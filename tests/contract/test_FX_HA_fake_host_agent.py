"""Contract checks for all Fake Host Agent capabilities in TST section 3.3."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fixtures.fake_host_agent import (
    DOWNLOAD_WORKER_UID,
    FIXTURE_REVIEW_STATUS,
    INSTANCE_CONTROLLER_UID,
    PLATFORM_LABEL,
    RPC_ALLOWLIST,
    TELEMETRY_WORKER_UID,
    ArbitraryMount,
    AuditDenialReason,
    ContainerStatus,
    FakeHostAgent,
    FixtureId,
    LaunchRejected,
    LaunchRejectionReason,
    LaunchSpec,
    LifecycleAction,
    PeerKind,
    PermissionDenied,
    RemovalRejected,
    RemovalRejectionReason,
    RpcMethod,
)

_ALLOWED_IMAGE_DIGEST = f"sha256:{'a' * 64}"
_OTHER_IMAGE_DIGEST = f"sha256:{'b' * 64}"


def _agent(*, max_log_line_characters: int = 4_096) -> FakeHostAgent:
    agent = FakeHostAgent(
        allowed_image_digests=(_ALLOWED_IMAGE_DIGEST,),
        max_log_line_characters=max_log_line_characters,
    )
    assert agent.review_status == FIXTURE_REVIEW_STATUS == "待真机复核"
    assert agent.fixture_ids == tuple(FixtureId)
    return agent


def _spec(runtime_id: str = "fixture-runtime", **changes: object) -> LaunchSpec:
    spec = LaunchSpec(
        runtime_id=runtime_id,
        image_digest=_ALLOWED_IMAGE_DIGEST,
        gpu_uuids=("GPU-fixture-01",),
    )
    return replace(spec, **changes)


def test_FX_HA_01_fixed_UIDs_have_only_their_explicit_method_allowlists() -> None:
    agent = _agent()
    expected = {
        DOWNLOAD_WORKER_UID: frozenset({RpcMethod.GET_HOST_INFO, RpcMethod.VALIDATE_PATH}),
        TELEMETRY_WORKER_UID: frozenset(
            {RpcMethod.GET_HOST_INFO, RpcMethod.LIST_GPUS, RpcMethod.INSPECT_RUNTIME}
        ),
        INSTANCE_CONTROLLER_UID: frozenset(
            {
                RpcMethod.ENSURE_RUNTIME,
                RpcMethod.STOP_RUNTIME,
                RpcMethod.REMOVE_RUNTIME,
                RpcMethod.INSPECT_RUNTIME,
                RpcMethod.LIST_RUNTIMES,
                RpcMethod.STREAM_LOGS,
            }
        ),
    }

    assert {
        PeerKind.DOWNLOAD_WORKER: expected[DOWNLOAD_WORKER_UID],
        PeerKind.TELEMETRY_WORKER: expected[TELEMETRY_WORKER_UID],
        PeerKind.INSTANCE_CONTROLLER: expected[INSTANCE_CONTROLLER_UID],
    } == RPC_ALLOWLIST
    for uid, methods in expected.items():
        assert {agent.require_allowed(uid, method.value) for method in methods} == methods
    assert agent.audit_records == ()


@pytest.mark.parametrize(
    ("peer_uid", "method", "expected_peer", "expected_reason"),
    (
        (99_999, RpcMethod.GET_HOST_INFO, PeerKind.UNKNOWN, AuditDenialReason.UNKNOWN_UID),
        (
            0,
            RpcMethod.ENSURE_RUNTIME,
            PeerKind.CONTAINER_ROOT,
            AuditDenialReason.CONTAINER_ROOT_FORBIDDEN,
        ),
        (
            DOWNLOAD_WORKER_UID,
            RpcMethod.ENSURE_RUNTIME,
            PeerKind.DOWNLOAD_WORKER,
            AuditDenialReason.METHOD_NOT_ALLOWED,
        ),
    ),
    ids=("unknown-uid", "container-root", "mismatched-method"),
)
def test_FX_HA_01_unknown_root_and_mismatch_are_each_denied_and_audited(
    peer_uid: int,
    method: RpcMethod,
    expected_peer: PeerKind,
    expected_reason: AuditDenialReason,
) -> None:
    agent = _agent()

    with pytest.raises(PermissionDenied) as raised:
        agent.require_allowed(peer_uid, method.value)

    assert raised.value.reason is expected_reason
    assert len(agent.audit_records) == 1
    record = agent.audit_records[0]
    assert record.event == "host_agent_rpc_denied"
    assert record.peer_kind is expected_peer
    assert record.reason is expected_reason
    assert record.rpc_method is method


def test_FX_HA_01_unlisted_method_is_default_denied_with_sanitized_audit() -> None:
    agent = _agent()
    untrusted_method = "FutureMethod:/internal/path?secret=fixture-secret"

    with pytest.raises(PermissionDenied) as raised:
        agent.require_allowed(INSTANCE_CONTROLLER_UID, untrusted_method)

    assert raised.value.reason is AuditDenialReason.METHOD_NOT_ALLOWED
    assert len(agent.audit_records) == 1
    record = agent.audit_records[0]
    assert record.rpc_method is None
    serialized_record = repr(record)
    assert untrusted_method not in serialized_record
    assert "fixture-secret" not in serialized_record
    assert "/internal/path" not in serialized_record


def test_FX_HA_02_EnsureRuntime_is_idempotent() -> None:
    agent = _agent()
    spec = _spec()

    first = agent.ensure_runtime(spec)
    second = agent.ensure_runtime(spec)

    assert first == second
    assert agent.runtime_count == 1
    assert [event.action for event in agent.lifecycle_events] == [LifecycleAction.CREATE]


def test_FX_HA_02_StopRuntime_graceful_stop_times_out_then_kills() -> None:
    agent = _agent()
    spec = _spec()
    agent.ensure_runtime(spec)
    agent.inject_graceful_stop_timeout(spec.runtime_id)

    inspection = agent.stop_runtime(spec.runtime_id, timeout_seconds=8.0)

    assert inspection.status is ContainerStatus.STOPPED
    assert inspection.exit_code == 137
    assert [event.action for event in agent.lifecycle_events] == [
        LifecycleAction.CREATE,
        LifecycleAction.GRACEFUL_STOP,
        LifecycleAction.KILL_AFTER_TIMEOUT,
    ]
    assert [event.timeout_seconds for event in agent.lifecycle_events[1:]] == [8.0, 8.0]


def test_FX_HA_02_RemoveRuntime_only_removes_platform_labeled_stopped_container() -> None:
    agent = _agent()
    foreign = _spec("foreign-stopped")
    running = _spec("platform-running")
    removable = _spec("platform-stopped")
    agent.inject_container(
        foreign,
        status=ContainerStatus.STOPPED,
        platform_label="another-platform",
    )
    agent.inject_container(
        running,
        status=ContainerStatus.RUNNING,
        platform_label=PLATFORM_LABEL,
    )
    agent.inject_container(
        removable,
        status=ContainerStatus.STOPPED,
        platform_label=PLATFORM_LABEL,
    )

    with pytest.raises(RemovalRejected) as foreign_rejection:
        agent.remove_runtime(foreign.runtime_id)
    with pytest.raises(RemovalRejected) as running_rejection:
        agent.remove_runtime(running.runtime_id)
    agent.remove_runtime(removable.runtime_id)

    assert foreign_rejection.value.reason is RemovalRejectionReason.PLATFORM_LABEL_REQUIRED
    assert running_rejection.value.reason is RemovalRejectionReason.CONTAINER_MUST_BE_STOPPED
    assert agent.runtime_count == 2
    assert agent.inspect_runtime(foreign.runtime_id).status is ContainerStatus.STOPPED
    assert agent.inspect_runtime(running.runtime_id).status is ContainerStatus.RUNNING
    with pytest.raises(KeyError, match="not present"):
        agent.inspect_runtime(removable.runtime_id)


def test_FX_HA_02_InspectRuntime_returns_status_exit_OOM_and_internal_address() -> None:
    agent = _agent()
    spec = _spec()
    agent.ensure_runtime(spec)
    agent.inject_exit(spec.runtime_id, exit_code=23, oom_killed=True)

    inspection = agent.inspect_runtime(spec.runtime_id)

    assert inspection.status is ContainerStatus.STOPPED
    assert inspection.exit_code == 23
    assert inspection.oom_killed is True
    assert inspection.internal_address == "runtime-fixture-runtime.holm-runtime"


def test_FX_HA_03_rejects_image_digest_not_in_allowlist() -> None:
    agent = _agent()

    with pytest.raises(LaunchRejected) as raised:
        agent.ensure_runtime(_spec(image_digest=_OTHER_IMAGE_DIGEST))

    assert raised.value.reason is LaunchRejectionReason.IMAGE_DIGEST_NOT_ALLOWED
    assert agent.runtime_count == 0


def test_FX_HA_03_rejects_GPU_occupied_by_other_platform_label_container() -> None:
    agent = _agent()
    spec = _spec()
    agent.inject_gpu_occupied_by_other_platform(spec.gpu_uuids[0])

    with pytest.raises(LaunchRejected) as raised:
        agent.ensure_runtime(spec)

    assert raised.value.reason is LaunchRejectionReason.GPU_OCCUPIED_BY_OTHER_PLATFORM
    assert agent.runtime_count == 0


def test_FX_HA_03_rejects_injected_mount_validation_failure() -> None:
    agent = _agent()
    spec = _spec()
    agent.inject_mount_validation_failure(spec.runtime_id)

    with pytest.raises(LaunchRejected) as raised:
        agent.ensure_runtime(spec)

    assert raised.value.reason is LaunchRejectionReason.MOUNT_VALIDATION_FAILED
    assert agent.runtime_count == 0


def test_FX_HA_04_exit_code_and_OOMKilled_are_independently_configurable() -> None:
    agent = _agent()
    oom_spec = _spec("oom-runtime")
    crash_spec = _spec("crash-runtime")
    agent.ensure_runtime(oom_spec)
    agent.ensure_runtime(crash_spec)

    agent.inject_exit(oom_spec.runtime_id, exit_code=137, oom_killed=True)
    agent.inject_exit(crash_spec.runtime_id, exit_code=17, oom_killed=False)

    oom = agent.inspect_runtime(oom_spec.runtime_id)
    crash = agent.inspect_runtime(crash_spec.runtime_id)
    assert (oom.exit_code, oom.oom_killed) == (137, True)
    assert (crash.exit_code, crash.oom_killed) == (17, False)


def test_FX_HA_05_StreamLogs_cursor_pages_have_no_duplicates() -> None:
    agent = _agent()
    runtime_id = "logs-runtime"
    agent.inject_logs(runtime_id, (f"line-{index}" for index in range(7)))

    observed_sequences: list[int] = []
    observed_text: list[str] = []
    cursor = None
    while True:
        page = agent.stream_logs(runtime_id, cursor=cursor, page_size=3)
        observed_sequences.extend(entry.sequence for entry in page.entries)
        observed_text.extend(entry.text for entry in page.entries)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert observed_sequences == list(range(7))
    assert len(observed_sequences) == len(set(observed_sequences))
    assert observed_text == [f"line-{index}" for index in range(7)]


def test_FX_HA_05_long_line_is_bounded_and_ANSI_sequence_remains_plain_text() -> None:
    line_limit = 128
    agent = _agent(max_log_line_characters=line_limit)
    ansi_line = "\x1b[31mred text\x1b[0m"
    long_line = "x" * 1_000_000
    agent.inject_logs("logs-runtime", (ansi_line, long_line))

    page = agent.stream_logs("logs-runtime", page_size=2)

    assert page.entries[0].text == ansi_line
    assert "\x1b[31m" in page.entries[0].text
    assert not page.entries[0].truncated
    assert page.entries[1].text == "x" * line_limit
    assert page.entries[1].truncated
    assert page.entries[1].original_character_count == 1_000_000
    assert agent.retained_log_characters == len(ansi_line) + line_limit
    assert max(len(entry.text) for entry in page.entries) <= agent.max_log_line_characters


def test_FX_HA_06_rejects_Shell_independently() -> None:
    agent = _agent()

    with pytest.raises(LaunchRejected) as raised:
        agent.ensure_runtime(_spec(shell="/bin/sh"))

    assert raised.value.reason is LaunchRejectionReason.SHELL_NOT_ALLOWED


def test_FX_HA_06_rejects_arbitrary_Docker_parameters_independently() -> None:
    agent = _agent()

    with pytest.raises(LaunchRejected) as raised:
        agent.ensure_runtime(_spec(docker_parameters={"privileged": "true"}))

    assert raised.value.reason is LaunchRejectionReason.DOCKER_PARAMETERS_NOT_ALLOWED


def test_FX_HA_06_rejects_arbitrary_Mount_independently() -> None:
    agent = _agent()
    arbitrary_mount = ArbitraryMount(source="/host", target="/runtime", read_only=False)

    with pytest.raises(LaunchRejected) as raised:
        agent.ensure_runtime(_spec(mounts=(arbitrary_mount,)))

    assert raised.value.reason is LaunchRejectionReason.ARBITRARY_MOUNT_NOT_ALLOWED


def test_FX_HA_06_rejects_arbitrary_image_and_latest_is_never_allowlisted() -> None:
    agent = _agent()

    with pytest.raises(LaunchRejected) as raised:
        agent.ensure_runtime(_spec(image_digest="runtime-image:latest"))
    with pytest.raises(ValueError, match="immutable sha256 digests"):
        FakeHostAgent(allowed_image_digests=("runtime-image:latest",))

    assert raised.value.reason is LaunchRejectionReason.IMAGE_DIGEST_NOT_ALLOWED
