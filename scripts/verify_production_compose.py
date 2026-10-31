"""Fail-closed static validation for the production Compose."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

from holm_contracts.release import PRODUCTION_AGENT_SOCKET_GID, PRODUCTION_SERVICE_IDS

# Loaded by path from tests as well as run as a script: find the sibling build module either way.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_artifacts import ARTIFACT_PATHS  # noqa: E402

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
COMPOSE_PATH: Final[Path] = PROJECT_ROOT / "deploy" / "compose" / "compose.prod.yaml"
APPLICATION_DOCKERFILE: Final[Path] = PROJECT_ROOT / "deploy" / "images" / "application.Dockerfile"
EDGE_DOCKERFILE: Final[Path] = PROJECT_ROOT / "deploy" / "images" / "edge.Dockerfile"

EXPECTED_SERVICES: Final[frozenset[str]] = frozenset(
    {
        "edge",
        "postgres",
        "migrate",
        "control-api",
        "gateway",
        "download-worker",
        "instance-controller",
        "ledger-writer",
        "maintenance-worker",
    }
)
EXPECTED_NETWORKS: Final[frozenset[str]] = frozenset({"holm-app", "holm-runtime"})
EXPECTED_SECRETS: Final[frozenset[str]] = frozenset(
    {
        "postgres_password",
        "migrate_database_url",
        "control_database_url",
        "gateway_database_url",
        "download_database_url",
        "instance_database_url",
        "ledger_database_url",
        "maintenance_database_url",
        "master_keyring",
        "api_key_pepper_keyring",
        "gateway_api_key_pepper_keyring",
        "idempotency_hmac_secret",
        "model_source_credentials",
    }
)
APPLICATION_SERVICES: Final[frozenset[str]] = EXPECTED_SERVICES - {"edge", "postgres"}
RUNTIME_NETWORK_MEMBERS: Final[frozenset[str]] = frozenset(
    {"control-api", "gateway", "instance-controller"}
)
AGENT_SERVICES: Final[frozenset[str]] = frozenset(
    {"control-api", "download-worker", "instance-controller"}
)
AGENT_DIRECTORY: Final[str] = "/run/holm"
EXTERNAL_MODEL_ROOT: Final[str] = "${HOLM_EXTERNAL_MODEL_ROOT:-/var/lib/holm/external-models}"
EXTERNAL_MODEL_SERVICES: Final[frozenset[str]] = frozenset(
    {"control-api", "download-worker", "maintenance-worker"}
)
APPLICATION_IMAGE: Final[str] = "holm/application:0.1.0"
EXPECTED_WHEELS: Final[frozenset[str]] = frozenset(
    path.name for path in ARTIFACT_PATHS if path.suffix == ".whl"
)
REQUIRED_MOUNT_TARGETS: Final[Mapping[str, frozenset[str]]] = {
    "edge": frozenset({"/etc/caddy/Caddyfile", "/data", "/config", "/etc/caddy/tls"}),
    "postgres": frozenset({"/var/lib/postgresql/data"}),
    "migrate": frozenset(),
    "control-api": frozenset({"/etc/holm/platform.yaml", AGENT_DIRECTORY, EXTERNAL_MODEL_ROOT}),
    "gateway": frozenset(
        {
            "/var/lib/holm/spool",
            "/var/lib/holm/tokenizers",
            "/etc/holm/platform.yaml",
        }
    ),
    "download-worker": frozenset(
        {
            "/var/lib/holm/models",
            "/var/lib/holm/tokenizers",
            EXTERNAL_MODEL_ROOT,
            "/etc/holm/platform.yaml",
            AGENT_DIRECTORY,
        }
    ),
    "instance-controller": frozenset(
        {
            "/etc/holm/release-manifest.json",
            "/etc/holm/platform.yaml",
            AGENT_DIRECTORY,
        }
    ),
    "ledger-writer": frozenset(
        {
            "/var/lib/holm/spool",
            "/etc/holm/release-manifest.json",
        }
    ),
    "maintenance-worker": frozenset(
        {
            "/var/lib/holm/backups",
            "/var/lib/holm/models",
            EXTERNAL_MODEL_ROOT,
            "/etc/holm/platform.yaml",
        }
    ),
}
WRITABLE_MOUNT_TARGETS: Final[Mapping[str, frozenset[str]]] = {
    "edge": frozenset({"/data", "/config"}),
    "postgres": frozenset({"/var/lib/postgresql/data"}),
    "migrate": frozenset(),
    "control-api": frozenset(),
    "gateway": frozenset({"/var/lib/holm/spool"}),
    "download-worker": frozenset(
        {
            "/var/lib/holm/models",
            "/var/lib/holm/tokenizers",
        }
    ),
    "instance-controller": frozenset(),
    "ledger-writer": frozenset({"/var/lib/holm/spool"}),
    "maintenance-worker": frozenset({"/var/lib/holm/backups"}),
}
DOCKER_SOCKET_MARKERS: Final[tuple[str, ...]] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
)
_FROM_LINE: Final[re.Pattern[str]] = re.compile(
    r"^\s*FROM\s+(?P<image>[^\s]+)", re.IGNORECASE | re.MULTILINE
)
_WHEEL_COPY: Final[re.Pattern[str]] = re.compile(
    r"^COPY\s+build/m0-02/python/(?P<wheel>[^\s]+\.whl)\s+/wheelhouse/$",
    re.MULTILINE,
)
_SYNTAX_DIRECTIVE: Final[re.Pattern[str]] = re.compile(
    r"^\s*#\s*syntax\s*=", re.IGNORECASE | re.MULTILINE
)


class ProductionComposeValidationError(AssertionError):
    """The production deployment definition failed a reviewed invariant."""


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProductionComposeValidationError(f"{label} must be a mapping")
    return cast(Mapping[str, object], value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ProductionComposeValidationError(f"{label} must be a list")
    return cast(Sequence[object], value)


def _parse(text: str) -> Mapping[str, object]:
    if not text.strip():
        raise ProductionComposeValidationError("production Compose file is empty")
    payload = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).strip()
    try:
        parsed: object = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProductionComposeValidationError(
            "production Compose file could not be parsed"
        ) from error
    return _mapping(parsed, "production Compose document")


def _service(services: Mapping[str, object], service_name: str) -> Mapping[str, object]:
    return _mapping(services.get(service_name), f"services.{service_name}")


def _volumes(service_name: str, service: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw_volumes = _sequence(service.get("volumes", []), f"{service_name}.volumes")
    return tuple(
        _mapping(volume, f"{service_name}.volumes[{index}]")
        for index, volume in enumerate(raw_volumes)
    )


def _assert_exact_image(image: object, label: str) -> str:
    if not isinstance(image, str) or not image:
        raise ProductionComposeValidationError(f"{label} image must be a string")
    final_segment = image.rsplit("/", maxsplit=1)[-1]
    if "@sha256:" not in image and ":" not in final_segment:
        raise ProductionComposeValidationError(
            f"{label} image must use an exact version tag or digest"
        )
    tag = final_segment.split("@", maxsplit=1)[0].rsplit(":", maxsplit=1)[-1]
    if tag.lower() == "latest":
        raise ProductionComposeValidationError(f"{label} image must not use latest")
    return image


def _assert_hardening(service_name: str, service: Mapping[str, object]) -> None:
    user = service.get("user")
    if not isinstance(user, str) or re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", user) is None:
        raise ProductionComposeValidationError(f"{service_name} must use a fixed non-root UID:GID")
    if service.get("read_only") is not True:
        raise ProductionComposeValidationError(
            f"{service_name} must have a read-only root filesystem"
        )
    if service.get("cap_drop") != ["ALL"]:
        raise ProductionComposeValidationError(f"{service_name} must drop every capability")
    security_options = _sequence(service.get("security_opt"), f"{service_name}.security_opt")
    if "no-new-privileges:true" not in security_options:
        raise ProductionComposeValidationError(f"{service_name} must set no-new-privileges")
    tmpfs = _sequence(service.get("tmpfs"), f"{service_name}.tmpfs")
    targets = [
        item.split(":", maxsplit=1)[0]
        for item in tmpfs
        if isinstance(item, str) and item.startswith("/")
    ]
    if len(targets) != len(tmpfs) or targets.count("/tmp") != 1:
        raise ProductionComposeValidationError(
            f"{service_name} must have one independent /tmp tmpfs"
        )


def _assert_identities(services: Mapping[str, object]) -> None:
    protected = AGENT_SERVICES | {"runtime"}
    protected_uids = [PRODUCTION_SERVICE_IDS[name][0] for name in protected]
    if len(protected_uids) != len(set(protected_uids)) or any(
        PRODUCTION_SERVICE_IDS[name][0] in protected_uids
        for name in PRODUCTION_SERVICE_IDS.keys() - protected
    ):
        raise ProductionComposeValidationError("Host Agent caller or Runtime UID is reused")
    if PRODUCTION_SERVICE_IDS["gateway"] != PRODUCTION_SERVICE_IDS["ledger-writer"]:
        raise ProductionComposeValidationError(
            "Gateway and Ledger Writer must share spool identity"
        )
    for service_name in EXPECTED_SERVICES:
        service = _service(services, service_name)
        uid, gid = PRODUCTION_SERVICE_IDS[service_name]
        if service.get("user") != f"{uid}:{gid}":
            raise ProductionComposeValidationError(
                f"{service_name} fixed UID:GID differs from manifest identity"
            )
        if service.get("group_add", []) != (
            [str(PRODUCTION_AGENT_SOCKET_GID)] if service_name in AGENT_SERVICES else []
        ):
            raise ProductionComposeValidationError(f"{service_name} Agent group membership differs")
        tmpfs = _sequence(service.get("tmpfs"), f"{service_name}.tmpfs")
        if not any(
            isinstance(item, str) and item.startswith("/tmp:") and f",uid={uid},gid={gid}" in item
            for item in tmpfs
        ):
            raise ProductionComposeValidationError(f"{service_name} tmpfs identity differs")


def _assert_keyring_mounts(services: Mapping[str, object], secrets: Mapping[str, object]) -> None:
    control = _sequence(_service(services, "control-api").get("secrets"), "control-api.secrets")
    gateway = _sequence(_service(services, "gateway").get("secrets"), "gateway.secrets")
    download = _sequence(
        _service(services, "download-worker").get("secrets"), "download-worker.secrets"
    )
    if "master_keyring" not in control or "master_keyring" not in download:
        raise ProductionComposeValidationError("shared Master Keyring must reach both services")
    if (
        "api_key_pepper_keyring" not in control
        or {
            "source": "gateway_api_key_pepper_keyring",
            "target": "api_key_pepper_keyring",
        }
        not in gateway
    ):
        raise ProductionComposeValidationError(
            "Pepper Keyring must use per-service owner-only files"
        )
    control_keyring = _mapping(secrets.get("api_key_pepper_keyring"), "api_key_pepper_keyring")
    gateway_keyring = _mapping(
        secrets.get("gateway_api_key_pepper_keyring"), "gateway_api_key_pepper_keyring"
    )
    if control_keyring.get("file") == gateway_keyring.get("file"):
        raise ProductionComposeValidationError("Pepper Keyring sources must be distinct")


def _assert_images(services: Mapping[str, object]) -> None:
    for service_name in EXPECTED_SERVICES:
        image = _assert_exact_image(_service(services, service_name).get("image"), service_name)
        if service_name == "postgres" and image != "holm/postgres:16.15-alpine":
            raise ProductionComposeValidationError("postgres must use the local release image")
        if service_name in APPLICATION_SERVICES and image != APPLICATION_IMAGE:
            raise ProductionComposeValidationError(
                "all Python components must share the application image"
            )


def _assert_ports(services: Mapping[str, object]) -> None:
    for service_name in EXPECTED_SERVICES:
        service = _service(services, service_name)
        ports = _sequence(service.get("ports", []), f"{service_name}.ports")
        if service_name != "edge" and ports:
            raise ProductionComposeValidationError(
                f"non-Edge service {service_name} must not publish ports"
            )
    edge_ports = _sequence(_service(services, "edge").get("ports"), "edge.ports")
    host_ports = {
        port.split(":", maxsplit=1)[0]
        for port in edge_ports
        if isinstance(port, str) and ":" in port
    }
    if host_ports != {"80", "443"} or len(edge_ports) != 2:
        raise ProductionComposeValidationError(
            "Edge must be the only service publishing host ports 80 and 443"
        )


def _assert_networks(document: Mapping[str, object], services: Mapping[str, object]) -> None:
    networks = _mapping(document.get("networks"), "networks")
    if frozenset(networks) != EXPECTED_NETWORKS:
        raise ProductionComposeValidationError(
            "production Compose network set differs from the reviewed set"
        )
    runtime = _mapping(networks.get("holm-runtime"), "holm-runtime")
    if runtime.get("internal") is not True:
        raise ProductionComposeValidationError("Runtime network must set internal: true")

    for service_name in EXPECTED_SERVICES:
        service_networks = frozenset(
            _sequence(
                _service(services, service_name).get("networks"),
                f"{service_name}.networks",
            )
        )
        expected = {"holm-app"}
        if service_name in RUNTIME_NETWORK_MEMBERS:
            expected.add("holm-runtime")
        if service_networks != expected:
            if service_name == "edge" and "holm-runtime" in service_networks:
                raise ProductionComposeValidationError("Edge must not join the Runtime network")
            raise ProductionComposeValidationError(
                f"{service_name} network membership differs from the required network topology"
            )


def _assert_command_argument(
    service_name: str, service: Mapping[str, object], option: str, expected: str
) -> None:
    command = _sequence(service.get("command"), f"{service_name}.command")
    # Reject duplicate/inline overrides: argparse would otherwise use the last value.
    positions = [
        index
        for index, token in enumerate(command)
        if isinstance(token, str) and (token == option or token.startswith(f"{option}="))
    ]
    if (
        len(positions) != 1
        or command[positions[0]] != option
        or positions[0] + 1 >= len(command)
        or command[positions[0] + 1] != expected
    ):
        raise ProductionComposeValidationError(
            f"{service_name} {option} must appear once and match its mount"
        )


def _assert_external_model_mount(
    service_name: str, service: Mapping[str, object], mounts: Sequence[Mapping[str, object]]
) -> None:
    external_mounts = [
        mount
        for mount in mounts
        if EXTERNAL_MODEL_ROOT in (mount.get("source"), mount.get("target"))
    ]
    if len(external_mounts) != 1:
        raise ProductionComposeValidationError(
            f"{service_name} must have exactly one external model root mount"
        )
    mount = external_mounts[0]
    if mount.get("source") != EXTERNAL_MODEL_ROOT or mount.get("target") != EXTERNAL_MODEL_ROOT:
        raise ProductionComposeValidationError(
            f"{service_name} external model root source and target must use the same expression"
        )
    if mount.get("read_only") is not True:
        raise ProductionComposeValidationError(
            f"{service_name} external model root mount must be read-only"
        )
    if service_name in {"download-worker", "maintenance-worker"}:
        _assert_command_argument(
            service_name, service, "--external-model-root", EXTERNAL_MODEL_ROOT
        )
    if service_name == "download-worker":
        _assert_command_argument(
            service_name, service, "--agent-socket", f"{AGENT_DIRECTORY}/agent.sock"
        )


def _assert_mounts(services: Mapping[str, object], raw_text: str) -> None:
    if any(marker in raw_text for marker in DOCKER_SOCKET_MARKERS):
        raise ProductionComposeValidationError(
            "Docker Socket must not appear in production Compose"
        )
    agent_mount_services: set[str] = set()
    for service_name in EXPECTED_SERVICES:
        service = _service(services, service_name)
        mounts = _volumes(service_name, service)
        if service_name in EXTERNAL_MODEL_SERVICES:
            _assert_external_model_mount(service_name, service, mounts)
        targets: set[str] = set()
        writable_targets: set[str] = set()
        for mount in mounts:
            if mount.get("type") != "bind":
                raise ProductionComposeValidationError(
                    f"{service_name} mounts must be explicit bind mounts"
                )
            source = mount.get("source")
            target = mount.get("target")
            read_only = mount.get("read_only")
            if not isinstance(source, str) or not isinstance(target, str):
                raise ProductionComposeValidationError(
                    f"{service_name} mount source and target must be strings"
                )
            if target in targets:
                raise ProductionComposeValidationError(
                    f"{service_name} mount targets must be unique"
                )
            if not isinstance(read_only, bool):
                raise ProductionComposeValidationError(
                    f"{service_name} mounts must state read_only explicitly"
                )
            targets.add(target)
            if not read_only:
                writable_targets.add(target)
            if source == AGENT_DIRECTORY or target == AGENT_DIRECTORY:
                if source != AGENT_DIRECTORY or target != AGENT_DIRECTORY or not read_only:
                    raise ProductionComposeValidationError(
                        "Agent Socket directory mount must be directory-to-directory and read-only"
                    )
                if service_name not in AGENT_SERVICES:
                    raise ProductionComposeValidationError(
                        "Agent Socket directory may be mounted only by Control API "
                        "and Agent-calling workers"
                    )
                agent_mount_services.add(service_name)
        if targets != REQUIRED_MOUNT_TARGETS[service_name]:
            raise ProductionComposeValidationError(
                f"{service_name} mount set differs from the required mount set"
            )
        if writable_targets != WRITABLE_MOUNT_TARGETS[service_name]:
            raise ProductionComposeValidationError(
                f"{service_name} writable mount set differs from the reviewed set"
            )
    if agent_mount_services != set(AGENT_SERVICES):
        raise ProductionComposeValidationError(
            "Agent Socket directory may be mounted only by Control API and Agent-calling workers"
        )


def _assert_dependencies(services: Mapping[str, object]) -> None:
    migrate_dependencies = _mapping(
        _service(services, "migrate").get("depends_on"), "migrate.depends_on"
    )
    postgres = _mapping(migrate_dependencies.get("postgres"), "migrate.depends_on.postgres")
    if postgres.get("condition") != "service_healthy":
        raise ProductionComposeValidationError("migrate must wait for healthy PostgreSQL")

    expected = {
        "postgres": "service_healthy",
        "migrate": "service_completed_successfully",
    }
    for service_name in APPLICATION_SERVICES - {"migrate"}:
        dependencies = _mapping(
            _service(services, service_name).get("depends_on"),
            f"{service_name}.depends_on",
        )
        actual = {
            name: _mapping(value, f"{service_name}.depends_on.{name}").get("condition")
            for name, value in dependencies.items()
        }
        if actual != expected:
            raise ProductionComposeValidationError(
                f"{service_name} must wait for PostgreSQL then successful migration"
            )
    if "depends_on" in _service(services, "edge"):
        raise ProductionComposeValidationError(
            "Edge must remain independent from application readiness"
        )


def _assert_migrate_credential(services: Mapping[str, object]) -> None:
    for service_name in EXPECTED_SERVICES:
        secrets = _sequence(
            _service(services, service_name).get("secrets", []),
            f"{service_name}.secrets",
        )
        has_credential = any(
            secret == "migrate_database_url"
            or isinstance(secret, Mapping)
            and secret.get("source") == "migrate_database_url"
            for secret in secrets
        )
        if has_credential != (service_name == "migrate"):
            raise ProductionComposeValidationError("only migrate may mount migrate_database_url")


def _assert_health_and_restart(services: Mapping[str, object]) -> None:
    for service_name in EXPECTED_SERVICES:
        service = _service(services, service_name)
        expected_restart = "no" if service_name == "migrate" else "unless-stopped"
        if service.get("restart") != expected_restart:
            raise ProductionComposeValidationError(
                f"{service_name} restart policy must be {expected_restart}"
            )
        if service_name == "migrate":
            if "healthcheck" in service:
                raise ProductionComposeValidationError(
                    "migrate is a one-shot completion dependency, not a long-running healthcheck"
                )
            continue
        healthcheck = _mapping(service.get("healthcheck"), f"{service_name}.healthcheck")
        test = _sequence(healthcheck.get("test"), f"{service_name}.healthcheck.test")
        if not test or not all(isinstance(item, str) and item for item in test):
            raise ProductionComposeValidationError(
                f"{service_name} healthcheck command must be nonempty"
            )
        for required in ("interval", "timeout", "retries"):
            if required not in healthcheck:
                raise ProductionComposeValidationError(
                    f"{service_name} healthcheck must define {required}"
                )


def _assert_no_dynamic_runtimes(services: Mapping[str, object]) -> None:
    runtime_like = sorted(name for name in services if name.startswith("runtime-"))
    if runtime_like:
        raise ProductionComposeValidationError(
            "dynamic Runtime instances must not be declared in production Compose"
        )


def validate_compose_text(text: str) -> tuple[str, ...]:
    """Validate one in-memory production Compose copy."""
    document = _parse(text)
    if document.get("name") != "holm":
        raise ProductionComposeValidationError("production Compose Project Name must be holm")
    services = _mapping(document.get("services"), "services")
    secrets = _mapping(document.get("secrets"), "secrets")
    if frozenset(secrets) != EXPECTED_SECRETS:
        raise ProductionComposeValidationError(
            "production Compose secret set differs from the reviewed set"
        )
    service_names = tuple(sorted(services))
    if frozenset(service_names) != EXPECTED_SERVICES:
        raise ProductionComposeValidationError(
            "production Compose service set differs from the reviewed nine services"
        )
    for service_name in service_names:
        _assert_hardening(service_name, _service(services, service_name))
    _assert_identities(services)
    _assert_keyring_mounts(services, secrets)
    _assert_images(services)
    _assert_ports(services)
    _assert_networks(document, services)
    _assert_mounts(services, text)
    _assert_migrate_credential(services)
    _assert_dependencies(services)
    _assert_health_and_restart(services)
    _assert_no_dynamic_runtimes(services)
    return service_names


def _read(path: Path, label: str) -> str:
    if not path.is_file():
        raise ProductionComposeValidationError(f"{label} is missing: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ProductionComposeValidationError(f"{label} could not be read") from error


def _dockerfile_base_images(text: str, label: str) -> tuple[str, ...]:
    images = tuple(match.group("image") for match in _FROM_LINE.finditer(text))
    if not images:
        raise ProductionComposeValidationError(f"{label} has no FROM instruction")
    for image in images:
        _assert_exact_image(image, label)
        if (
            re.fullmatch(
                r"\$\{HOLM_BASE_REGISTRY\}/library/[a-z]+:[0-9][a-zA-Z0-9_.-]*@sha256:[0-9a-f]{64}",
                image,
            )
            is None
            or "ARG HOLM_BASE_REGISTRY=docker.io" not in text.splitlines()
        ):
            raise ProductionComposeValidationError(
                f"{label} must use HOLM_BASE_REGISTRY and a pinned SHA-256 digest"
            )
    return images


def validate_dockerfile_texts(application_text: str, edge_text: str) -> None:
    """Validate fixed bases and artifact-only image assembly."""
    for label, text in (("application", application_text), ("Edge", edge_text)):
        if _SYNTAX_DIRECTIVE.search(text):
            raise ProductionComposeValidationError(
                f"{label} Dockerfile must not use # syntax= (requires an external build frontend)"
            )
    _dockerfile_base_images(application_text, "application Dockerfile base")
    _dockerfile_base_images(edge_text, "Edge Dockerfile base")
    for name, (uid, gid) in PRODUCTION_SERVICE_IDS.items():
        if name not in APPLICATION_SERVICES:
            continue
        identity_name = "spool" if name in {"gateway", "ledger-writer"} else name
        if f"'{identity_name}:{uid}:{gid}'" not in application_text:
            raise ProductionComposeValidationError(
                f"application Dockerfile missing {name} identity"
            )
    if f"groupadd --gid {PRODUCTION_AGENT_SOCKET_GID} holm-agent" not in application_text:
        raise ProductionComposeValidationError("application Dockerfile missing Agent group")
    migrate_uid, migrate_gid = PRODUCTION_SERVICE_IDS["migrate"]
    if f"USER {migrate_uid}:{migrate_gid}" not in application_text:
        raise ProductionComposeValidationError("application Dockerfile default UID:GID differs")
    edge_uid, edge_gid = PRODUCTION_SERVICE_IDS["edge"]
    if f"USER {edge_uid}:{edge_gid}" not in edge_text:
        raise ProductionComposeValidationError("Edge Dockerfile UID:GID differs")
    wheels = frozenset(_WHEEL_COPY.findall(application_text))
    if wheels != EXPECTED_WHEELS:
        raise ProductionComposeValidationError(
            "application Dockerfile must copy the exact 16 build_artifacts.py wheels"
        )
    lowered_application = application_text.lower()
    if "uv sync" in lowered_application:
        raise ProductionComposeValidationError(
            "application image must install wheels, not run uv sync"
        )
    if "pip install" not in lowered_application or "/wheelhouse/*.whl" not in application_text:
        raise ProductionComposeValidationError(
            "application Dockerfile must install the reviewed wheel set"
        )
    lowered_edge = edge_text.lower()
    edge_lines = [line.strip() for line in edge_text.splitlines()]
    capability_removal = "RUN setcap -r /usr/bin/caddy"
    if capability_removal not in edge_lines or edge_lines.index(capability_removal) > next(
        index for index, line in enumerate(edge_lines) if line.startswith("USER ")
    ):
        raise ProductionComposeValidationError(
            "Edge Dockerfile must remove Caddy file capabilities before USER"
        )
    if "npm " in lowered_edge or "vite" in lowered_edge:
        raise ProductionComposeValidationError(
            "Edge image must copy Web artifacts, not compile the frontend"
        )
    if "COPY build/m0-02/web/ /srv/holm/" not in edge_text:
        raise ProductionComposeValidationError(
            "Edge Dockerfile must copy the built Web artifact directory"
        )
    if "COPY deploy/caddy/Caddyfile.tmpl" not in edge_text:
        raise ProductionComposeValidationError(
            "Edge Dockerfile must contain the reviewed Caddyfile template"
        )


def validate_production_files() -> tuple[str, ...]:
    compose_text = _read(COMPOSE_PATH, "production Compose file")
    services = validate_compose_text(compose_text)
    validate_dockerfile_texts(
        _read(APPLICATION_DOCKERFILE, "application Dockerfile"),
        _read(EDGE_DOCKERFILE, "Edge Dockerfile"),
    )
    return services


def _mutable_document() -> dict[str, object]:
    return copy.deepcopy(dict(_parse(_read(COMPOSE_PATH, "production Compose file"))))


def _mutable_service(document: dict[str, object], name: str) -> dict[str, object]:
    services = document["services"]
    assert isinstance(services, dict)
    service = services[name]
    assert isinstance(service, dict)
    return cast(dict[str, object], service)


def _mutate_non_edge_ports(document: dict[str, object]) -> None:
    _mutable_service(document, "postgres")["ports"] = ["5432:5432"]


def _mutate_docker_socket(document: dict[str, object]) -> None:
    volumes = _mutable_service(document, "gateway")["volumes"]
    assert isinstance(volumes, list)
    volumes.append(
        {
            "type": "bind",
            "source": "/var/run/docker.sock",
            "target": "/var/run/docker.sock",
            "read_only": False,
        }
    )


def _mutate_runtime_internal(document: dict[str, object]) -> None:
    networks = document["networks"]
    assert isinstance(networks, dict)
    runtime = networks["holm-runtime"]
    assert isinstance(runtime, dict)
    runtime.pop("internal")


def _mutate_edge_runtime(document: dict[str, object]) -> None:
    networks = _mutable_service(document, "edge")["networks"]
    assert isinstance(networks, list)
    networks.append("holm-runtime")


MUTATIONS: Final[Mapping[str, Callable[[dict[str, object]], None]]] = {
    "non-edge-ports": _mutate_non_edge_ports,
    "docker-socket": _mutate_docker_socket,
    "runtime-not-internal": _mutate_runtime_internal,
    "edge-runtime-network": _mutate_edge_runtime,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mutate",
        choices=tuple(MUTATIONS),
        help="apply one deliberate invalid mutation before validation",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mutate is None:
            services = validate_production_files()
        else:
            document = _mutable_document()
            MUTATIONS[args.mutate](document)
            services = validate_compose_text(json.dumps(document))
    except ProductionComposeValidationError as error:
        print(f"production Compose validation failed: {error}", file=sys.stderr)
        return 1
    print(f"validated {len(services)} production services: {', '.join(services)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
