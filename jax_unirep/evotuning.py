"""API for evolutionary tuning."""
import logging
from functools import partial
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as onp
import optuna
from jax import grad, jit, lax
from jax import numpy as np
from jax import random, vmap
from jax.experimental.optimizers import adam
from jax.experimental.stax import Dense, Softmax, serial
from sklearn.model_selection import KFold, train_test_split

from jax_unirep.losses import _neg_cross_entropy_loss

from .layers import mLSTM1900, mLSTM1900_AvgHidden, mLSTM1900_HiddenStates
from .optimizers import adamW
from .params import add_dense_params
from .utils import (
    aa_seq_to_int,
    batch_sequences,
    dump_params,
    get_batch_len,
    get_embeddings,
    load_embedding_1900,
    load_params,
    one_hots,
    validate_mLSTM1900_params,
)

# setup logger
logger = logging.getLogger("evotuning")
logger.setLevel(logging.INFO)
fh = logging.FileHandler("evotuning.log")
fh.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s :: %(levelname)s :: %(message)s")
fh.setFormatter(formatter)
logger.addHandler(fh)

# setup model
model_layers = (mLSTM1900(), mLSTM1900_HiddenStates(), Dense(25), Softmax)
init_fun, predict = serial(*model_layers)


@jit
def evotune_loss(params, inputs, targets):
    predictions = vmap(partial(predict, params))(inputs)

    return _neg_cross_entropy_loss(targets, predictions)


def avg_loss(sequences, params):
    """
    Return average loss of a set of parameters,
    on a set of sequences.

    :param sequences: sequences (in standard AA format)
    :param params: parameters (i.e. from training)
    """
    xs, ys = length_batch_input_outputs(sequences)

    sum_loss = 0
    for x, y in zip(xs, ys):
        sum_loss += evotune_loss(params, inputs=x, targets=y) * len(x)

    return sum_loss / len(sequences)


def evotuning_pairs(s: str) -> Tuple[np.ndarray, np.ndarray]:
    """
    Given a sequence, return input-output pairs for evotuning.

    The goal of evotuning is to get the RNN to accurately predict
    the next character in a sequence.
    This convenience function exists to prep a single sequence
    into its corresponding input-output tensor pairs.

    Given a 1D sequence of length `k`,
    it gets represented as a 2D array of shape (k, 10),
    where 10 is the size of the embedding of each amino acid,
    and k-1 ranges from the zeroth a.a. to the nth a.a.
    This is the first element in the returned tuple.

    Given the same 1D sequence,
    the output is defined as a 2D array of shape (k-1, 25),
    where 25 is number of indices available to us
    in ``aa_to_int``,
    and k-1 corresponds to the first a.a. to the nth a.a.
    This is the second element in the returned tuple.

    :param s: The protein sequence to featurize.
    :returns: Two 2D NumPy arrays,
        the first corresponding to the input to evotuning with shape (n_letters, 10),
        and the second corresponding to the output amino acid to predict with shape (n_letters, 25).
    """
    seq_int = aa_seq_to_int(s[:-1])
    next_letters_int = aa_seq_to_int(s[1:])
    embeddings = load_embedding_1900()
    x = np.stack([embeddings[i] for i in seq_int])
    y = np.stack([one_hots[i] for i in next_letters_int])
    return x, y


def input_output_pairs(sequences: List[str]) -> Tuple[np.ndarray, np.ndarray]:
    """
    Generate input-output tensor pairs for evo-tuning.

    We check that lengths of sequences are identical,
    as this is necessary to ensure stacking of tensors happens correctly.

    :param sequences: A list of sequences
        to generate input-output tensor pairs.
    :returns: Two NumPy arrays,
        the first corresponding to the input to evotuning
        with shape (n_sequences, n_letters+1, 10),
        and the second corresponding to the output amino acids to predict
        with shape (n_sequences, n_letters+1, 25).
        Both will have an additional "sample" dimension as the first dim.
    """
    seqlengths = set(map(len, sequences))
    if not len(seqlengths) == 1:
        raise ValueError(
            """
Sequences should be of uniform length, but are not.
Please ensure that they are all of the same length before passing them in.
"""
        )

    xs = []
    ys = []
    for s in sequences:
        x, y = evotuning_pairs(s)
        xs.append(x)
        ys.append(y)
    return np.stack(xs), np.stack(ys)


def length_batch_input_outputs(
    sequences: List[str],
) -> Tuple[List[np.ndarray], List[np.ndarray]]:
    """
    Return lists of x and y tensors for evotuning, batched by their length.

    This function exists because we need a way of
    batching sequences by size conveniently.

    :param sequences: A list of sequences to evotune on.
    :returns: Two lists of NumPy arrays, one for xs and the other for ys.
    """
    idxs_batched = batch_sequences(sequences)

    xs = []
    ys = []
    for idxs in idxs_batched:
        seqs = [sequences[i] for i in idxs]
        x, y = input_output_pairs(seqs)
        xs.append(x)
        ys.append(y)
    return xs, ys


def fit(
    params: Dict,
    sequences: List[str],
    n: int,
    step_size: float = 0.001,
    holdout_seqs: Optional[List[str]] = None,
    proj_name: Optional[str] = "temp",
    steps_per_print: Optional[int] = 200,
) -> Dict:
    """
    Return weights fitted to predict the next letter in each sequence.

    The training loop is as follows.
    Per step in the training loop,
    we loop over each "length batch" of sequences and tune weights
    in order of the length of each sequence.
    For example, if we have sequences of length 302, 305, and 309,
    over K training epochs,
    we will perform 3xK updates,
    one step of updates for each length.

    To get batching of sequences by length done,
    we call on ``batch_sequences`` from our ``utils.py`` module,
    which returns a list of sub-lists,
    in which each sub-list contains the indices
    in the original list of sequences
    that are of a particular length.

    To learn more about the passing of ``params``,
    have a look at the ``evotune`` function docstring.

    You can optionally dump parameters 
    and print weights every `steps_per_print` steps
    to monitor training progress.
    Set this to ``None`` to avoid parameter dumping.

    :param params: mLSTM1900 and Dense parameters.
    :param sequences: List of sequences to evotune on.
    :param n: The number of iterations to evotune on.
    :param step_size: The learning rate
    :param holdout_seqs: Holdout set, an optional input.
    :param proj_name: The directory path for weights to be output to.
    :param steps_per_print: Number of steps per printing and dumping
        of weights.
    """

    # Load and check that params have correct keys and shapes
    if params is None:
        params = load_params()

    # Defensive programming checks
    if len(params) != len(model_layers):
        raise ValueError(
            "The number of parameters specified must match the number of stax.serial layers"
        )
    validate_mLSTM1900_params(params[0])

    # batch sequences by length
    xs, ys = length_batch_input_outputs(sequences)

    avg_len, batch_lens = get_batch_len(xs)

    logger.info(
        f"Number of batches: {len(xs)}, "
        + f"Average batch length: {avg_len}, "
        + f"Batch lengths: {batch_lens}, "
    )

    init, update, get_params = adamW(step_size=step_size)

    @jit
    def step(i, state):
        """
        Perform one step of evolutionary updating.

        This function is closed inside `fit` because we need access
        to the variables in its scope,
        particularly the update and get_params functions.

        By structuring the function this way, we can JIT-compile it,
        and thus gain a massive speed-up!

        :param i: The current iteration of the training loop.
        :param state: Current state of parameters from jax.
        """
        params = get_params(state)
        g = grad(evotune_loss)(params, x, y)
        state = update(i, g, state)

        return state

    state = init(params)

    for i in range(n):

        logger.info(f"Starting epoch {i + 1}")

        for x, y in zip(xs, ys):
            state = step(i, state)

            # change logger level and uncomment to display debug prints
            # commenting this out as it causes memory issues at high epochs
            # logger.debug(f"Shape of y: {(len(y), len(y[0]), len(y[0][0]))}")
            # logger.debug(vmap(partial(predict, get_params(state)))(x))
        if steps_per_print:
            if (i + 1) % steps_per_print == 0:

                logger.info(
                    f"Epoch {i + 1}: "
                    + f"train-loss={avg_loss(sequences, get_params(state))}, "
                )

                if holdout_seqs is not None:

                    # calculate and print loss for out-domain holdout set
                    logger.info(
                        f"Epoch {i + 1}: "
                        + f"holdout-loss={avg_loss(holdout_seqs, get_params(state))}, "
                    )

                # dump current params in case run crashes or loss increases
                # steps printed are 1-indexed i.e. starts at epoch 1 not 0.
                dump_params(get_params(state), proj_name, (i + 1))

    return get_params(state)


# def evotune_step(
#     i: int,
#     state,
#     optimizer_funcs: Tuple[Callable, Callable],
#     loss_func: Callable,
#     x: np.ndarray,
#     y: np.ndarray,
# ):
#     """
#     Perform one step of evolutionary updating.

#     ;param i: The current iteration of the training loop.
#     :param state: Current state of parameters from jax.
#     :param optimizer_funcs: The (update, get_params) functions
#         from jax's optimizers.
#     :param loss_func: The loss function.
#     :return state: Updated state of parameters from jax.
#     """
#     # Unpack optimizer funcs
#     update, get_params = optimizer_funcs
#     params = get_params(state)

#     # Unpack loss funcs
#     dloss = grad(loss_func)

#     l = loss_func(params, x, y)

#     # Conditional check
# #     pred = np.isnan(l)
# #     def true_fun(x):
# #         return optuna.exceptions.TrialPruned()
# #     true_operand = None
# #     def false_fun(x):
# #         pass
# #     false_operand = None

# #     lax.cond(pred, true_operand, true_fun, false_operand, false_fun)

# #     Rewrite the following using lax.cond
# #     if np.isnan(l):
# #         l = np.inf
# #         print("NaN occured in optimization. Skipping trial.")
# #         raise optuna.exceptions.TrialPruned()
# #     print(f"Iteration: {i}, Loss: {l:.4f}")

#     g = dloss(params, x, y)

#     state = update(i, g, state)
#     return state


def objective(
    trial,
    sequences: List[str],
    params: Optional[Dict] = None,
    n_epochs_config: Dict = None,
    learning_rate_config: Dict = None,
    n_splits: Optional[int] = 5,
) -> float:
    """
    Objective function for an Optuna trial.

    The goal with the objective function is
    to automatically find the number of epochs to train
    that minimizes the average of 5-fold test loss.
    Doing so allows us to avoid babysitting the model manually.

    :param trial: An Optuna trial object.
    :param sequences: A list of strings corresponding to the sequences
        that we want to evotune against.
    :param params: A dictionary of parameters.
        Should have the keys ``mLSTM1900`` and ``dense``,
        which correspond to the mLSTM weights and dense layer weights
        (output dimensions = 25)
        to predict the next character in the sequence.
    :param n_epochs_config: A dictionary of kwargs
        to ``trial.suggest_discrete_uniform``,
        which are: ``name``, ``low``, ``high``, ``q``.
        This controls how many epochs to have Optuna test.
        See source code for default configuration,
        at the definition of ``n_epochs_kwargs``.
    :param n_splits: The number of folds of cross-validation to do.

    :returns: Average of 5-fold test loss.
    """
    # Default settings for n_epochs_kwargs
    n_epochs_kwargs = {
        "name": "n_epochs",
        "low": 1,
        "high": len(sequences) * 3,
        "q": 1,
    }

    # Default settings for learning_rate_kwargs
    learning_rate_kwargs = {
        "name": "learning_rate",
        "low": 0.00001,
        "high": 0.01,
    }

    if n_epochs_config is not None:
        n_epochs_kwargs.update(n_epochs_config)
    if learning_rate_config is not None:
        learning_rate_kwargs.update(learning_rate_config)

    n_epochs = trial.suggest_discrete_uniform(**n_epochs_kwargs)
    learning_rate = trial.suggest_loguniform(**learning_rate_kwargs)
    logger.info(
        f"Trying out {n_epochs} epochs with learning rate {learning_rate}."
    )

    kf = KFold(n_splits=n_splits, shuffle=True)
    sequences = onp.array(sequences)

    avg_test_losses = []
    for i, (train_index, test_index) in enumerate(kf.split(sequences)):
        logger.info(f"Split #{i}")
        train_sequences, test_sequences = (
            sequences[train_index],
            sequences[test_index],
        )

        evotuned_params = fit(
            params, train_sequences, n=int(n_epochs), step_size=learning_rate
        )

        avg_test_losses.append(avg_loss(test_sequences, evotuned_params))

    return sum(avg_test_losses) / len(avg_test_losses)


def evotune(
    sequences: List[str],
    params: Optional[Dict] = None,
    proj_name: Optional[str] = "temp",
    out_dom_seqs: Optional[List[str]] = None,
    n_trials: Optional[int] = 20,
    n_epochs_config: Dict = None,
    learning_rate_config: Dict = None,
    n_splits: Optional[int] = 5,
    steps_per_print: Optional[int] = 200,
) -> Dict:
    """
    Evolutionarily tune the model to a set of sequences.

    Evotuning is described in the original UniRep and eUniRep papers.
    This reimplementation of evotune provides a nicer API
    that automatically handles multiple sequences of variable lengths.

    Evotuning always needs a starter set of weights.
    By default, the pre-trained weights from the Nature Methods paper are used.
    However, other pre-trained weights are legitimate.

    We first use optuna to figure out how many epochs to fit
    before overfitting happens.
    To save on computation time, the number of trials run
    defaults to 20, but can be configured.

    By default, mLSTM1900 and Dense weights from the paper are used 
    by passing in `params=None`,
    but if you want to use randomly intialized weights:

        from jax_unirep.evotuning import init_fun
        from jax.random import PRNGKey
        _, params = init_fun(PRNGKey(0), input_shape=(-1, 10))

    or dumped weights:

        from jax_unirep.utils import load_params
        params = load_params(folderpath="path/to/params/folder")

    This function is intended as an automagic way of identifying
    the best model and training routine hyperparameters.
    If you want more control over how fitting happens,
    please use the `fit()` function directly.
    There is an example in the `examples/` directory
    that shows how to use it.

    :param sequences: Sequences to evotune against.
    :param params: Parameters to be passed into `mLSTM1900` and `Dense`.
        Optional; if None, will default to weights from paper,
        or you can pass in your own set of parameters,
        as long as they are stax-compatible.
    :param proj_name: Name of the project,
        used to name created output directory.
    :param out_dom_seqs: Out-domain holdout set of sequences,
        to check for loss on to prevent overfitting.
    :param n_trials: The number of trials Optuna should attempt.
    :param n_epochs_config: A dictionary of kwargs
        to ``trial.suggest_discrete_uniform``,
        which are: ``name``, ``low``, ``high``, ``q``.
        This controls how many epochs to have Optuna test.
        See source code for default configuration,
        at the definition of ``n_epochs_kwargs``.
    :param learning_rate_config: A dictionary of kwargs
        to ``trial.suggest_loguniform``,
        which are: ``name``, ``low``, ``high``.
        This controls the learning rate of the model.
        See source code for default configuration,
        at the definition of ``learning_rate_kwargs``.
    :param n_splits: The number of folds of cross-validation to do.
    :param steps_per_print: The number of steps between each
        printing and dumping of weights in the final
        evotuning step using the optimized hyperparameters.

    :returns:
        - study - The optuna study object, containing information
        about all evotuning trials.
        - evotuned_params - A dictionary of optimized weights
    """

    study = optuna.create_study()

    objective_func = lambda x: objective(
        x,
        params=params,
        sequences=sequences,
        n_epochs_config=n_epochs_config,
        learning_rate_config=learning_rate_config,
        n_splits=n_splits,
    )
    study.optimize(objective_func, n_trials=n_trials)
    num_epochs = int(study.best_params["n_epochs"])
    learning_rate = float(study.best_params["learning_rate"])

    logger.info(
        f"Optuna done, starting tuning with learning rate={learning_rate}, "
    )

    evotuned_params = fit(
        params=params,
        sequences=sequences,
        n=num_epochs,
        step_size=learning_rate,
        holdout_seqs=out_dom_seqs,
        proj_name=proj_name,
        steps_per_print=steps_per_print,
    )

    return study, evotuned_params
