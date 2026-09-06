"""The experiment state machine.

    CREATING ──► READY ──► RUNNING ──┬─► COMPLETED ──► DESTROYED
        │          │         │       ├─► FAILED    ──► DESTROYED
        │          │         │       ├─► TIMEOUT   ──► DESTROYED
        │          │         │       └─► CANCELLED ──► DESTROYED
        └──► FAILED          └──► READY   (more commands may follow a run)

Transitions are checked, not assumed. An illegal move is a bug in the caller
and is surfaced as :class:`InvalidStateTransitionError` rather than silently
corrupting the record -- which matters because ``DESTROYED`` is what tells the
server a container no longer exists.
"""

from __future__ import annotations

from ..errors import InvalidStateTransitionError
from ..models import ExperimentStatus as S

EXPERIMENT_TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATING: frozenset({S.READY, S.FAILED, S.DESTROYED}),
    # READY -> COMPLETED lets an experiment be closed out without running anything.
    S.READY: frozenset({S.RUNNING, S.COMPLETED, S.FAILED, S.CANCELLED, S.DESTROYED}),
    S.RUNNING: frozenset({S.READY, S.COMPLETED, S.FAILED, S.TIMEOUT, S.CANCELLED, S.DESTROYED}),
    # Terminal-but-alive states: the sandbox is still there to be inspected.
    S.COMPLETED: frozenset({S.RUNNING, S.DESTROYED}),
    S.FAILED: frozenset({S.RUNNING, S.DESTROYED}),
    S.TIMEOUT: frozenset({S.RUNNING, S.DESTROYED}),
    S.CANCELLED: frozenset({S.RUNNING, S.DESTROYED}),
    # Truly terminal.
    S.DESTROYED: frozenset(),
}

TERMINAL_STATES: frozenset[S] = frozenset({S.DESTROYED})

# States in which the sandbox exists and can accept work.
LIVE_STATES: frozenset[S] = frozenset(
    {S.READY, S.RUNNING, S.COMPLETED, S.FAILED, S.TIMEOUT, S.CANCELLED}
)


def can_transition(current: S, target: S) -> bool:
    return target in EXPERIMENT_TRANSITIONS.get(current, frozenset())


def assert_transition(experiment_id: str, current: S, target: S) -> None:
    """Raise unless ``current -> target`` is a legal edge."""
    if current == target:
        return
    if not can_transition(current, target):
        allowed = sorted(s.value for s in EXPERIMENT_TRANSITIONS.get(current, frozenset()))
        raise InvalidStateTransitionError(
            f"Experiment {experiment_id} cannot move from {current.value} to {target.value}.",
            experiment_id=experiment_id,
            current=current.value,
            requested=target.value,
            allowed=allowed,
        )
