"""Isolation tests for the execution state machine."""

import pytest

from execution.states import (
    AbandonReason,
    IllegalStateTransition,
    PaymentState,
    validate_transition,
)


def test_full_happy_path_is_legal():
    validate_transition(None, PaymentState.AT_RISK)
    validate_transition(PaymentState.AT_RISK, PaymentState.DIAGNOSED)
    validate_transition(PaymentState.DIAGNOSED, PaymentState.SCHEDULED)
    validate_transition(PaymentState.SCHEDULED, PaymentState.EXECUTING)
    validate_transition(PaymentState.EXECUTING, PaymentState.AWAITING_CONFIRMATION)
    validate_transition(PaymentState.AWAITING_CONFIRMATION, PaymentState.RECOVERED)


def test_skipping_a_state_is_illegal():
    with pytest.raises(IllegalStateTransition):
        validate_transition(PaymentState.AT_RISK, PaymentState.RECOVERED)


def test_transition_from_terminal_state_is_illegal():
    with pytest.raises(IllegalStateTransition):
        validate_transition(PaymentState.RECOVERED, PaymentState.DIAGNOSED)
    with pytest.raises(IllegalStateTransition):
        validate_transition(PaymentState.ABANDONED, PaymentState.SCHEDULED)


def test_backwards_transition_is_illegal():
    with pytest.raises(IllegalStateTransition):
        validate_transition(PaymentState.SCHEDULED, PaymentState.DIAGNOSED)


def test_abandoned_requires_a_reason():
    with pytest.raises(IllegalStateTransition):
        validate_transition(PaymentState.DIAGNOSED, PaymentState.ABANDONED)


def test_abandoned_with_reason_is_legal_from_every_non_terminal_state():
    for from_state in (
        PaymentState.DIAGNOSED,
        PaymentState.SCHEDULED,
        PaymentState.EXECUTING,
        PaymentState.AWAITING_CONFIRMATION,
    ):
        validate_transition(from_state, PaymentState.ABANDONED, AbandonReason.POLICY_STOP)


def test_reason_only_valid_on_a_transition_to_abandoned():
    with pytest.raises(IllegalStateTransition):
        validate_transition(
            PaymentState.AT_RISK, PaymentState.DIAGNOSED, AbandonReason.POLICY_STOP
        )
