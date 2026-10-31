"""Generic ordered state transition contract."""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType


class InvalidStateTransition(ValueError):
    """Raised when an event requests a transition outside the declared graph."""

    def __init__(self) -> None:
        super().__init__("state transition rejected")


@dataclass(frozen=True, slots=True)
class StateSnapshot[StateT: Hashable]:
    state: StateT
    version: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("state version must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class StateMachine[StateT: Hashable]:
    transitions: Mapping[StateT, Set[StateT]]
    terminal_states: Set[StateT]

    def __post_init__(self) -> None:
        frozen_transitions = {
            state: frozenset(targets) for state, targets in self.transitions.items()
        }
        frozen_terminals = frozenset(self.terminal_states)
        if any(frozen_transitions.get(state, frozenset()) for state in frozen_terminals):
            raise ValueError("terminal states cannot declare outgoing transitions")
        object.__setattr__(self, "transitions", MappingProxyType(frozen_transitions))
        object.__setattr__(self, "terminal_states", frozen_terminals)

    def advance(
        self, snapshot: StateSnapshot[StateT], target: StateT, *, event_version: int
    ) -> StateSnapshot[StateT]:
        if (
            isinstance(event_version, bool)
            or not isinstance(event_version, int)
            or event_version <= 0
        ):
            raise ValueError("event version must be a positive integer")
        if event_version <= snapshot.version or snapshot.state in self.terminal_states:
            return snapshot
        if target not in self.transitions.get(snapshot.state, frozenset()):
            raise InvalidStateTransition()
        return StateSnapshot(state=target, version=event_version)
