"""Tests for various Priors."""

import pytest

from dsr.test.test_core import model
from dsr.test.generate_test_data import CONFIG_TRAINING_OVERRIDE
from dsr.program import from_tokens, Program
from dsr.memory import Batch
from dsr.controller import parents_siblings

import numpy as np


BATCH_SIZE = 1000


def assert_invalid(model, cases):
    batch = make_batch(model, cases)
    logp = model.controller.compute_probs(batch, log=True)
    print(batch)
    assert all(np.isneginf(logp)), \
        "Found invalid case with probability > 0."


def assert_valid(model, cases):
    batch = make_batch(model, cases)
    logp = model.controller.compute_probs(batch, log=True)
    assert all(logp > -np.inf), \
        "Found valid case with probability 0."


def make_sequence(model, L):
    """Utility function to generate a sequence of length L"""
    X = Program.library.input_tokens[0]
    U = Program.library.unary_tokens[0]
    B = Program.library.binary_tokens[0]
    num_B = (L - 1) // 2
    num_U = int(L % 2 == 0)
    num_X = num_B + 1
    case = [B] * num_B + [U] * num_U + [X] * num_X
    assert len(case) == L
    case = case[:model.controller.max_length]
    return case


def make_batch(model, cases):
    """
    Utility function to generate a Batch from cases.

    This uses essentially the same logic as controller.py's loop_fn, except
    actions are prescribed instead of samples. Is there a way to refactor these
    with less code reuse?
    """

    batch_size = len(cases)
    L = model.controller.max_length

    # Pad actions to maximum length
    actions = np.array([a + [0] * (L - len(a)) for a in cases], dtype=np.int32)

    # Initialize obs
    prev_actions = np.zeros_like(actions)
    parents = np.zeros_like(actions)
    siblings = np.zeros_like(actions)

    arities = Program.library.arities
    parent_adjust = Program.library.parent_adjust

    # Set initial values
    empty_parent = np.max(parent_adjust) + 1
    empty_sibling = len(arities)
    action = empty_sibling
    parent, sibling = empty_parent, empty_sibling
    prior = np.array([model.prior.initial_prior()] * batch_size)

    priors = []
    lengths = np.zeros(batch_size, dtype=np.int32)
    finished = np.zeros(batch_size, dtype=np.bool_)
    dangling = np.ones(batch_size, dtype=np.int32)
    for i in range(L):
        partial_actions = actions[:, :(i + 1)]

        # Set prior and obs used to generate this action
        prev_actions[:, i] = action
        parents[:, i] = parent
        siblings[:, i] = sibling
        priors.append(prior)

        # Compute next obs and prior
        action = actions[:, i]
        parent, sibling = parents_siblings(tokens=partial_actions,
                                           arities=arities,
                                           parent_adjust=parent_adjust)
        dangling += arities[action] - 1
        prior = model.prior(partial_actions, parent, sibling, dangling)
        finished = np.where(np.logical_and(dangling == 0, lengths == 0),
                            True,
                            False)
        lengths = np.where(finished,
                           i + 1,
                           lengths)

    lengths = np.where(lengths == 0, L, lengths)
    obs = [prev_actions, parents, siblings]
    priors = np.array(priors).swapaxes(0, 1)
    rewards = np.zeros(batch_size, dtype=np.float32)
    batch = Batch(actions, obs, priors, lengths, rewards)
    return batch


def test_child(model):

    library = Program.library
    parents = library.actionize("log,exp,mul")
    children = library.actionize("exp,log,sin")

    model.config_prior = {} # Turn off all other Priors
    model.config_prior["child"] = {"children" : children, "parents" : parents}
    model.config_training.update(CONFIG_TRAINING_OVERRIDE)
    model.train()

    # For each parent-child pair, generate invalid cases where child is one of
    # parent's children.
    X = Program.library.input_tokens[0]
    assert X not in children, \
        "Error in test case specification. Do not include x1 in children."
    invalid_cases = []
    for p, c in zip(parents, children):
        arity = library.tokenize(p)[0].arity
        for i in range(arity):
            before = i
            after = arity - i - 1
            case = [p] + [X] * before + [c] + [X] * after
            invalid_cases.append(case)
    assert_invalid(model, invalid_cases)


def test_inverse(model):

    library = Program.library
    model.config_prior = {} # Turn off all other Priors
    model.config_prior["inverse"] = {}
    model.config_training.update(CONFIG_TRAINING_OVERRIDE)
    model.train()

    # Generate invalid cases for each inverse
    invalid_cases = []
    invalid_cases.append(library.actionize("mul,sin,x1,exp,log,x1"))
    for t1, t2 in library.inverse_tokens.items():
        invalid_cases.append([t1, t2])
        invalid_cases.append([t2, t1])
    assert_invalid(model, invalid_cases)


@pytest.mark.parametrize("minmax", [(10, 10), (4, 30), (None, 10), (10, None)])
def test_length(model, minmax):
    """Test length constraints."""

    min_, max_ = minmax
    model.config_prior = {} # Turn off all other Priors
    model.config_prior["length"] = {"min_" : min_, "max_" : max_}
    model.config_training.update(CONFIG_TRAINING_OVERRIDE)
    model.train()

    # First, check that randomly generated samples do not violate constraints
    actions, _, _ = model.controller.sample(BATCH_SIZE)
    programs = [from_tokens(a, optimize=True) for a in actions]
    lengths = [len(p.traversal) for p in programs]
    if min_ is not None:
        min_L = min(lengths)
        assert min_L >= min_, \
            "Found min length {} but constrained to {}.".format(min_L, min_)
    if max_ is not None:
        max_L = max(lengths)
        assert max_L <= max_, \
            "Found max length {} but constrained to {}.".format(max_L, max_)

    # Next, check valid and invalid test cases based on min_ and max_
    # Valid test cases should not be constrained
    # Invalid test cases should all be constrained
    valid_cases = []
    invalid_cases = []

    # Initial prior prevents length-1 tokens
    case = make_sequence(model, 1)
    invalid_cases.append(case)

    if min_ is not None:
        # Generate an invalid case that is one Token too short
        if min_ > 1:
            case = make_sequence(model, min_ - 1)
            invalid_cases.append(case)

        # Generate a valid case that is exactly the minimum length
        case = make_sequence(model, min_)
        valid_cases.append(case)

    if max_ is not None:
        # Generate an invalid case that is one Token too long (which will be
        # truncated to dangling == 1)
        case = make_sequence(model, max_ + 1)
        invalid_cases.append(case)

        # Generate a valid case that is exactly the maximum length
        case = make_sequence(model, max_)
        valid_cases.append(case)

    assert_valid(model, valid_cases)
    assert_invalid(model, invalid_cases)
