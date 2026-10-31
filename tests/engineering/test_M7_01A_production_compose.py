"""M7-01A fail-closed production Compose and image-definition checks."""

from __future__ import annotations

import copy
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from holm_contracts.release import PRODUCTION_AGENT_SOCKET_GID, PRODUCTION_SERVICE_IDS

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_validator() -> ModuleType:
    path = PROJECT_ROOT / "scripts" / "verify_production_compose.py"
    spec = importlib.util.spec_from_file_location("m7_01a_verify_production", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load scripts/verify_production_compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()
COMPOSE_PATH: Path = VALIDATOR.COMPOSE_PATH
APPLICATION_DOCKERFILE: Path = VALIDATOR.APPLICATION_DOCKERFILE
EDGE_DOCKERFILE: Path = VALIDATOR.EDGE_DOCKERFILE
ProductionComposeValidationError = VALIDATOR.ProductionComposeValidationError


def _document() -> dict[str, object]:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    payload = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return cast(dict[str, object], parsed)


def _service(document: dict[str, object], name: str) -> dict[str, object]:
    services = document["services"]
    assert isinstance(services, dict)
    service = services[name]
    assert isinstance(service, dict)
    return cast(dict[str, object], service)


def _mutated(mutation: Callable[[dict[str, object]], None]) -> str:
    document = copy.deepcopy(_document())
    mutation(document)
    return json.dumps(document)


def _remove_runtime_internal(document: dict[str, object]) -> None:
    networks = document["networks"]
    assert isinstance(networks, dict)
    runtime = networks["holm-runtime"]
    assert isinstance(runtime, dict)
    runtime.pop("internal")


def _put_edge_on_runtime(document: dict[str, object]) -> None:
    networks = _service(document, "edge")["networks"]
    assert isinstance(networks, list)
    networks.append("holm-runtime")


def _mount_docker_socket(document: dict[str, object]) -> None:
    volumes = _service(document, "gateway")["volumes"]
    assert isinstance(volumes, list)
    volumes.append(
        {
            "type": "bind",
            "source": "/var/run/docker.sock",
            "target": "/var/run/docker.sock",
            "read_only": False,
        }
    )


def _remove_cap_drop(document: dict[str, object]) -> None:
    _service(document, "control-api").pop("cap_drop")


def _remove_no_new_privileges(document: dict[str, object]) -> None:
    _service(document, "gateway")["security_opt"] = []


def _remove_tmp(document: dict[str, object]) -> None:
    tmpfs = _service(document, "postgres")["tmpfs"]
    assert isinstance(tmpfs, list)
    _service(document, "postgres")["tmpfs"] = [
        item for item in tmpfs if not str(item).startswith("/tmp:")
    ]


def _remove_healthcheck(document: dict[str, object]) -> None:
    _service(document, "ledger-writer").pop("healthcheck")


def _break_dependency(document: dict[str, object]) -> None:
    dependencies = _service(document, "download-worker")["depends_on"]
    assert isinstance(dependencies, dict)
    migrate = dependencies["migrate"]
    assert isinstance(migrate, dict)
    migrate["condition"] = "service_started"


def _mount_agent_on_gateway(document: dict[str, object]) -> None:
    volumes = _service(document, "gateway")["volumes"]
    assert isinstance(volumes, list)
    volumes.append(
        {
            "type": "bind",
            "source": "/run/holm",
            "target": "/run/holm",
            "read_only": True,
        }
    )


def _remove_control_runtime_network(document: dict[str, object]) -> None:
    _service(document, "control-api")["networks"] = ["holm-app"]


def _restore_telemetry_worker(document: dict[str, object]) -> None:
    services = document["services"]
    assert isinstance(services, dict)
    services["telemetry-worker"] = copy.deepcopy(_service(document, "control-api"))


def _restore_telemetry_secret(document: dict[str, object]) -> None:
    secrets = document["secrets"]
    assert isinstance(secrets, dict)
    secrets["telemetry_database_url"] = {"file": "./secrets/telemetry_database_url"}


def test_M7_01A_production_definition_scans_exact_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert VALIDATOR.validate_production_files() == (
        "control-api",
        "download-worker",
        "edge",
        "gateway",
        "instance-controller",
        "ledger-writer",
        "maintenance-worker",
        "migrate",
        "postgres",
    )
    assert VALIDATOR.main([]) == 0
    assert capsys.readouterr().out.startswith("validated 9 production services:")


@pytest.mark.parametrize(
    ("service_name", "replacement", "message"),
    (
        ("download-worker", "10003:10003", "fixed UID:GID differs"),
        ("gateway", "10003:10003", "fixed UID:GID differs"),
        ("ledger-writer", "10005:10005", "fixed UID:GID differs"),
    ),
)
def test_FIX_12_uid_guard_rejects_reuse_and_spool_divergence(
    service_name: str, replacement: str, message: str
) -> None:
    with pytest.raises(ProductionComposeValidationError, match=message):
        VALIDATOR.validate_compose_text(
            _mutated(
                lambda document: _service(document, service_name).update({"user": replacement})
            )
        )


@pytest.mark.parametrize("service_name", ("control-api", "download-worker", "instance-controller"))
def test_FIX_12_agent_group_is_required_only_for_callers(service_name: str) -> None:
    with pytest.raises(ProductionComposeValidationError, match="Agent group membership differs"):
        VALIDATOR.validate_compose_text(
            _mutated(lambda document: _service(document, service_name).pop("group_add"))
        )


@pytest.mark.parametrize("service_name", ("gateway", "ledger-writer", "edge", "maintenance-worker"))
def test_FIX_12_agent_group_rejects_noncallers(service_name: str) -> None:
    with pytest.raises(ProductionComposeValidationError, match="Agent group membership differs"):
        VALIDATOR.validate_compose_text(
            _mutated(
                lambda document: _service(document, service_name).update(
                    {"group_add": [str(PRODUCTION_AGENT_SOCKET_GID)]}
                )
            )
        )


def test_FIX_12_identity_layout_source_rejects_caller_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for service_name in ("control-api", "gateway", "runtime"):
        altered = dict(PRODUCTION_SERVICE_IDS)
        altered[service_name] = PRODUCTION_SERVICE_IDS["download-worker"]
        monkeypatch.setattr(VALIDATOR, "PRODUCTION_SERVICE_IDS", altered)
        with pytest.raises(ProductionComposeValidationError, match="UID is reused"):
            VALIDATOR._assert_identities({})


def test_FIX_12_spool_source_requires_identical_uid_and_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    altered = dict(PRODUCTION_SERVICE_IDS)
    altered["ledger-writer"] = (10004, 10005)
    monkeypatch.setattr(VALIDATOR, "PRODUCTION_SERVICE_IDS", altered)
    with pytest.raises(ProductionComposeValidationError, match="share spool identity"):
        VALIDATOR._assert_identities({})


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda document: _service(document, "gateway").update(
                {"secrets": ["gateway_database_url", "api_key_pepper_keyring"]}
            ),
            "per-service owner-only files",
        ),
        (
            lambda document: document["secrets"]["gateway_api_key_pepper_keyring"].update(
                {"file": document["secrets"]["api_key_pepper_keyring"]["file"]}
            ),
            "sources must be distinct",
        ),
        (
            lambda document: _service(document, "download-worker").update(
                {"secrets": ["download_database_url", "model_source_credentials"]}
            ),
            "shared Master Keyring",
        ),
    ),
)
def test_FIX_12_shared_secret_guards_fail_loudly(
    mutation: Callable[[dict[str, object]], None], message: str
) -> None:
    with pytest.raises(ProductionComposeValidationError, match=message):
        VALIDATOR.validate_compose_text(_mutated(mutation))


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda document: _service(document, "postgres").update({"ports": ["5432:5432"]}),
            "non-Edge service postgres must not publish ports",
        ),
        (_mount_docker_socket, "Docker Socket must not appear"),
        (_remove_runtime_internal, "Runtime network must set internal: true"),
        (_put_edge_on_runtime, "Edge must not join the Runtime network"),
        (_remove_control_runtime_network, "control-api network membership differs"),
    ),
)
def test_M7_01A_core_network_and_socket_guards_fail_loudly(
    mutation: Callable[[dict[str, object]], None], message: str
) -> None:
    with pytest.raises(ProductionComposeValidationError, match=message):
        VALIDATOR.validate_compose_text(_mutated(mutation))


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (_restore_telemetry_worker, "service set differs"),
        (_restore_telemetry_secret, "secret set differs"),
    ),
)
def test_FIX_08_removed_deployment_units_fail_loudly(
    mutation: Callable[[dict[str, object]], None], message: str
) -> None:
    with pytest.raises(ProductionComposeValidationError, match=message):
        VALIDATOR.validate_compose_text(_mutated(mutation))


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda document: _service(document, "edge").pop("read_only"),
            "read-only root filesystem",
        ),
        (_remove_cap_drop, "drop every capability"),
        (_remove_no_new_privileges, "set no-new-privileges"),
        (
            lambda document: _service(document, "control-api").update({"user": "0:0"}),
            "fixed non-root UID:GID",
        ),
        (_remove_tmp, "independent /tmp tmpfs"),
    ),
)
def test_M7_01A_each_container_hardening_guard_has_a_positive_control(
    mutation: Callable[[dict[str, object]], None], message: str
) -> None:
    with pytest.raises(ProductionComposeValidationError, match=message):
        VALIDATOR.validate_compose_text(_mutated(mutation))


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda document: _service(document, "gateway").update(
                {"image": "holm/application:latest"}
            ),
            "must not use latest",
        ),
        (
            lambda document: _service(document, "ledger-writer").update(
                {"image": "holm/ledger-writer:0.1.0"}
            ),
            "share the application image",
        ),
        (_mount_agent_on_gateway, "Agent Socket directory may be mounted only"),
        (_break_dependency, "successful migration"),
        (_remove_healthcheck, "ledger-writer.healthcheck must be a mapping"),
        (
            lambda document: _service(document, "maintenance-worker").update({"restart": "no"}),
            "restart policy must be unless-stopped",
        ),
    ),
)
def test_M7_01A_service_contract_guards_have_positive_controls(
    mutation: Callable[[dict[str, object]], None], message: str
) -> None:
    with pytest.raises(ProductionComposeValidationError, match=message):
        VALIDATOR.validate_compose_text(_mutated(mutation))


def test_M7_01A_base_image_latest_guard_has_a_positive_control() -> None:
    application = APPLICATION_DOCKERFILE.read_text(encoding="utf-8")
    edge = EDGE_DOCKERFILE.read_text(encoding="utf-8")
    VALIDATOR.validate_dockerfile_texts(application, edge)

    with pytest.raises(ProductionComposeValidationError, match="must not use latest"):
        VALIDATOR.validate_dockerfile_texts(
            application.replace(
                "FROM ${HOLM_BASE_REGISTRY}/library/python:3.12.11-slim-bookworm@sha256:"
                "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7",
                "FROM ${HOLM_BASE_REGISTRY}/library/python:latest",
                1,
            ),
            edge,
        )


@pytest.mark.parametrize("mutation", ("missing", "commented", "after-user"))
def test_M7_04_caddy_file_capability_guard_has_positive_controls(mutation: str) -> None:
    """FX-M7-04-edge-capability: production Dockerfile with removal disabled or misplaced."""
    application = APPLICATION_DOCKERFILE.read_text(encoding="utf-8")
    edge = EDGE_DOCKERFILE.read_text(encoding="utf-8")
    VALIDATOR.validate_dockerfile_texts(application, edge)
    instruction = "RUN setcap -r /usr/bin/caddy\n"
    assert instruction in edge
    replacement = "# " + instruction if mutation == "commented" else ""
    altered = edge.replace(instruction, replacement, 1)
    if mutation == "after-user":
        altered += "\n" + instruction
    with pytest.raises(ProductionComposeValidationError, match="remove Caddy file capabilities"):
        VALIDATOR.validate_dockerfile_texts(application, altered)


@pytest.mark.parametrize(
    ("application_mutation", "edge_mutation", "message"),
    (
        (
            lambda text: text.replace(
                "COPY build/m0-02/python/holm_domain-0.1.0rc1-py3-none-any.whl /wheelhouse/\n",
                "",
                1,
            ),
            lambda text: text,
            "exact 16",
        ),
        (
            lambda text: text.replace("pip install", "uv sync", 1),
            lambda text: text,
            "not run uv sync",
        ),
        (
            lambda text: text,
            lambda text: text.replace("COPY build/m0-02/web/ /srv/holm/", "RUN npm run build", 1),
            "not compile the frontend",
        ),
        (
            lambda text: text,
            lambda text: text.replace(
                "COPY deploy/caddy/Caddyfile.tmpl /etc/holm/Caddyfile.tmpl",
                "",
                1,
            ),
            "Caddyfile template",
        ),
    ),
)
def test_M7_01A_artifact_only_build_guards_have_positive_controls(
    application_mutation: Callable[[str], str],
    edge_mutation: Callable[[str], str],
    message: str,
) -> None:
    application = APPLICATION_DOCKERFILE.read_text(encoding="utf-8")
    edge = EDGE_DOCKERFILE.read_text(encoding="utf-8")
    with pytest.raises(ProductionComposeValidationError, match=message):
        VALIDATOR.validate_dockerfile_texts(application_mutation(application), edge_mutation(edge))


def test_M7_01A_four_cli_positive_controls_emit_specific_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = {
        "non-edge-ports": "non-Edge service postgres must not publish ports",
        "docker-socket": "Docker Socket must not appear",
        "runtime-not-internal": "Runtime network must set internal: true",
        "edge-runtime-network": "Edge must not join the Runtime network",
    }
    for mutation, message in expected.items():
        assert VALIDATOR.main(["--mutate", mutation]) == 1
        captured = capsys.readouterr()
        assert message in captured.err


@pytest.mark.parametrize("service_name", ("control-api", "download-worker", "maintenance-worker"))
@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing", "exactly one external model root mount"),
        ("duplicate", "exactly one external model root mount"),
        ("writable", "external model root mount must be read-only"),
        ("implicit-read-only", "external model root mount must be read-only"),
        ("wrong-source", "source and target must use the same expression"),
        ("wrong-target", "source and target must use the same expression"),
        ("fixed-root", "exactly one external model root mount"),
        ("extra-root", "mount set differs"),
    ),
)
def test_M7_04_TC_DP_07_external_root_mount_guards_have_positive_controls(
    service_name: str, mutation: str, message: str
) -> None:
    """FX-M7-04-compose-guard: production template with one deliberate contract violation."""
    document = _document()
    volumes = _service(document, service_name)["volumes"]
    assert isinstance(volumes, list)
    root = "${HOLM_EXTERNAL_MODEL_ROOT:-/var/lib/holm/external-models}"
    mount = next(item for item in volumes if item["target"] == root)
    if mutation == "missing":
        volumes.remove(mount)
    elif mutation == "duplicate":
        volumes.append(copy.deepcopy(mount))
    elif mutation == "writable":
        mount["read_only"] = False
    elif mutation == "implicit-read-only":
        mount.pop("read_only")
    elif mutation == "wrong-source":
        mount["source"] = "/srv/other-models"
    elif mutation == "wrong-target":
        mount["target"] = "/var/lib/holm/external-models"
    elif mutation == "fixed-root":
        mount.update(source="/srv/models", target="/srv/models")
    elif mutation == "extra-root":
        volumes.append(
            {"type": "bind", "source": "/srv/extra", "target": "/srv/extra", "read_only": True}
        )
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    with pytest.raises(ProductionComposeValidationError, match=message):
        VALIDATOR.validate_compose_text(json.dumps(document))


@pytest.mark.parametrize(
    ("service_name", "option"),
    (
        ("download-worker", "--external-model-root"),
        ("maintenance-worker", "--external-model-root"),
        ("download-worker", "--agent-socket"),
    ),
)
@pytest.mark.parametrize("mutation", ("missing", "missing-value", "wrong", "duplicate", "inline"))
def test_M7_04_TC_DP_07_worker_mount_argument_guards_have_positive_controls(
    service_name: str, option: str, mutation: str
) -> None:
    document = _document()
    command = _service(document, service_name)["command"]
    assert isinstance(command, list)
    index = command.index(option)
    if mutation == "missing":
        del command[index : index + 2]
    elif mutation == "missing-value":
        del command[index : index + 2]
        command.append(option)
    elif mutation == "wrong":
        command[index + 1] = "/srv/wrong"
    elif mutation == "duplicate":
        command.extend([option, "/srv/wrong"])
    elif mutation == "inline":
        command.append(f"{option}=/srv/wrong")
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    with pytest.raises(
        ProductionComposeValidationError, match=f"{option} must appear once and match its mount"
    ):
        VALIDATOR.validate_compose_text(json.dumps(document))


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing", "mount set differs"),
        ("writable", "Agent Socket directory mount must be directory-to-directory and read-only"),
        (
            "wrong-source",
            "Agent Socket directory mount must be directory-to-directory and read-only",
        ),
        (
            "wrong-target",
            "Agent Socket directory mount must be directory-to-directory and read-only",
        ),
    ),
)
def test_M7_04_TC_DP_07_download_agent_mount_guards_have_positive_controls(
    mutation: str, message: str
) -> None:
    document = _document()
    volumes = _service(document, "download-worker")["volumes"]
    assert isinstance(volumes, list)
    mount = next(item for item in volumes if item["target"] == "/run/holm")
    if mutation == "missing":
        volumes.remove(mount)
    elif mutation == "writable":
        mount["read_only"] = False
    elif mutation == "wrong-source":
        mount["source"] = "/run/other-agent"
    elif mutation == "wrong-target":
        mount["target"] = "/run/other-agent"
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    with pytest.raises(ProductionComposeValidationError, match=message):
        VALIDATOR.validate_compose_text(json.dumps(document))
