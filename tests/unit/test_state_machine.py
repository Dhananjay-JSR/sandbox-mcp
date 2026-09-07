from __future__ import annotations

import pytest

from sandbox_mcp.errors import InvalidStateTransitionError
from sandbox_mcp.experiments.state import (
    EXPERIMENT_TRANSITIONS,
    LIVE_STATES,
    assert_transition,
    can_transition,
)
from sandbox_mcp.models import ExperimentStatus as S


class TestTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            (S.CREATING, S.READY),
            (S.CREATING, S.FAILED),
            (S.READY, S.RUNNING),
            (S.RUNNING, S.COMPLETED),
            (S.RUNNING, S.FAILED),
            (S.RUNNING, S.TIMEOUT),
            (S.RUNNING, S.CANCELLED),
            (S.RUNNING, S.READY),
            (S.COMPLETED, S.RUNNING),
            (S.FAILED, S.RUNNING),
            (S.COMPLETED, S.DESTROYED),
            (S.FAILED, S.DESTROYED),
        ],
    )
    def test_legal_edges(self, current: S, target: S) -> None:
        assert can_transition(current, target)
        assert_transition("exp_1", current, target)

    @pytest.mark.parametrize(
        "current,target",
        [
            (S.CREATING, S.RUNNING),
            (S.CREATING, S.COMPLETED),
            (S.DESTROYED, S.RUNNING),
            (S.DESTROYED, S.READY),
            (S.DESTROYED, S.COMPLETED),
            (S.READY, S.TIMEOUT),
        ],
    )
    def test_illegal_edges_are_refused(self, current: S, target: S) -> None:
        assert not can_transition(current, target)
        with pytest.raises(InvalidStateTransitionError):
            assert_transition("exp_1", current, target)

    def test_self_transition_is_a_no_op(self) -> None:
        for status in S:
            assert_transition("exp_1", status, status)

    def test_destroyed_is_absorbing(self) -> None:
        assert EXPERIMENT_TRANSITIONS[S.DESTROYED] == frozenset()

    def test_every_live_state_can_reach_destroyed(self) -> None:
        """Otherwise a sandbox could become impossible to clean up."""
        for status in LIVE_STATES:
            assert can_transition(status, S.DESTROYED)

    def test_every_status_has_a_rule(self) -> None:
        assert set(EXPERIMENT_TRANSITIONS) == set(S)

    def test_the_error_names_the_legal_moves(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as info:
            assert_transition("exp_1", S.DESTROYED, S.RUNNING)
        assert info.value.details["allowed"] == []
        assert info.value.details["current"] == "DESTROYED"
