"""TC-INV-13 deterministic generated state-machine properties."""

from __future__ import annotations

import random
from enum import StrEnum

import pytest
from holm_domain import ModelInstanceStatus
from holm_domain.state_machine import (
    InvalidStateTransition,
    StateMachine,
    StateSnapshot,
)

PROPERTY_SEEDS = tuple(range(64))


class ExampleState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


MACHINE = StateMachine[ExampleState](
    transitions={
        ExampleState.QUEUED: frozenset({ExampleState.RUNNING, ExampleState.FAILED}),
        ExampleState.RUNNING: frozenset({ExampleState.COMPLETED, ExampleState.FAILED}),
    },
    terminal_states=frozenset({ExampleState.COMPLETED, ExampleState.FAILED}),
)

MODEL_INSTANCE_MACHINE = StateMachine[ModelInstanceStatus](
    transitions={
        ModelInstanceStatus.WAITING_FOR_RESOURCE: frozenset(
            {ModelInstanceStatus.STARTING, ModelInstanceStatus.FAILED}
        ),
        ModelInstanceStatus.STARTING: frozenset(
            {ModelInstanceStatus.RUNNING, ModelInstanceStatus.FAILED}
        ),
        ModelInstanceStatus.RUNNING: frozenset(
            {
                ModelInstanceStatus.STOPPING,
                ModelInstanceStatus.RESTARTING,
                ModelInstanceStatus.UNHEALTHY,
                ModelInstanceStatus.FAILED,
            }
        ),
        ModelInstanceStatus.UNHEALTHY: frozenset(
            {ModelInstanceStatus.RESTARTING, ModelInstanceStatus.FAILED}
        ),
        ModelInstanceStatus.RESTARTING: frozenset(
            {ModelInstanceStatus.RUNNING, ModelInstanceStatus.FAILED}
        ),
        ModelInstanceStatus.STOPPING: frozenset(
            {ModelInstanceStatus.STOPPED, ModelInstanceStatus.FAILED}
        ),
    },
    terminal_states=frozenset({ModelInstanceStatus.STOPPED, ModelInstanceStatus.FAILED}),
)


@pytest.mark.parametrize("seed", PROPERTY_SEEDS)
def test_TC_INV_13_terminal_state_never_rolls_back_for_generated_sequences(seed: int) -> None:
    generator = random.Random(seed)
    generated = [
        (generator.randint(1, 30), generator.choice(tuple(ExampleState))) for _ in range(50)
    ]
    generated[24] = (31, ExampleState.RUNNING)
    generated[25] = (32, ExampleState.COMPLETED)
    snapshot = StateSnapshot(state=ExampleState.QUEUED)
    terminal_snapshot: StateSnapshot[ExampleState] | None = None

    for event_version, target in generated:
        previous = snapshot
        try:
            snapshot = MACHINE.advance(snapshot, target, event_version=event_version)
        except InvalidStateTransition:
            assert snapshot == previous
        if terminal_snapshot is None and snapshot.state in MACHINE.terminal_states:
            terminal_snapshot = snapshot
        if terminal_snapshot is not None:
            assert snapshot == terminal_snapshot

    assert terminal_snapshot is not None


def test_TC_INV_13_lower_and_same_versions_are_ignored_and_illegal_transition_rejected() -> None:
    running = StateSnapshot(state=ExampleState.RUNNING, version=7)

    assert MACHINE.advance(running, ExampleState.COMPLETED, event_version=7) == running
    assert MACHINE.advance(running, ExampleState.COMPLETED, event_version=6) == running
    with pytest.raises(InvalidStateTransition):
        MACHINE.advance(running, ExampleState.QUEUED, event_version=8)


@pytest.mark.parametrize("seed", PROPERTY_SEEDS)
def test_M3_01_TC_INV_13_model_instance_terminal_epoch_never_rolls_back(seed: int) -> None:
    generator = random.Random(seed)
    generated = [
        (generator.randint(1, 30), generator.choice(tuple(ModelInstanceStatus))) for _ in range(50)
    ]
    generated[24] = (31, ModelInstanceStatus.STARTING)
    generated[25] = (32, ModelInstanceStatus.FAILED)
    snapshot = StateSnapshot(state=ModelInstanceStatus.WAITING_FOR_RESOURCE)
    terminal_snapshot: StateSnapshot[ModelInstanceStatus] | None = None

    for event_version, target in generated:
        previous = snapshot
        try:
            snapshot = MODEL_INSTANCE_MACHINE.advance(
                snapshot,
                target,
                event_version=event_version,
            )
        except InvalidStateTransition:
            assert snapshot == previous
        if terminal_snapshot is None and snapshot.state in MODEL_INSTANCE_MACHINE.terminal_states:
            terminal_snapshot = snapshot
        if terminal_snapshot is not None:
            assert snapshot == terminal_snapshot

    assert terminal_snapshot is not None


def test_M3_01_TC_INV_13_model_instance_terminal_rejects_newer_async_transition() -> None:
    failed = StateSnapshot(state=ModelInstanceStatus.FAILED, version=7)

    assert (
        MODEL_INSTANCE_MACHINE.advance(
            failed,
            ModelInstanceStatus.RUNNING,
            event_version=7,
        )
        == failed
    )
    assert (
        MODEL_INSTANCE_MACHINE.advance(
            failed,
            ModelInstanceStatus.RUNNING,
            event_version=8,
        )
        == failed
    )
