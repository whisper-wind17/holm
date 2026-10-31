"""Fail-closed static validation for the development Compose."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
COMPOSE_PATH: Final[Path] = PROJECT_ROOT / "deploy" / "compose" / "compose.yaml"
EXPECTED_SERVICES: Final[frozenset[str]] = frozenset({"postgres", "migrate", "control-api"})
EXPECTED_NETWORKS: Final[frozenset[str]] = frozenset({"holm-app", "holm-runtime"})
AGENT_DIRECTORY: Final[str] = "/run/holm"
DOCKER_SOCKET_MARKERS: Final[tuple[str, ...]] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
)
_LATEST_TAG: Final[re.Pattern[str]] = re.compile(r"(?:^|:)latest(?:@|$)", re.IGNORECASE)
_TMPFS_COMMENT: Final[re.Pattern[str]] = re.compile(
    r"^\s*# writable-tmpfs (?P<service>[a-z0-9-]+) "
    r"(?P<target>/[^:\s]+):\s+\S.*$",
    re.MULTILINE,
)


class ComposeValidationError(AssertionError):
    """The development Compose failed a reviewed invariant."""


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ComposeValidationError(f"{label} must be a mapping")
    return cast(Mapping[str, object], value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ComposeValidationError(f"{label} must be a list")
    return cast(Sequence[object], value)


def _parse(text: str) -> Mapping[str, object]:
    if not text.strip():
        raise ComposeValidationError("compose file is empty")
    payload = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).strip()
    try:
        parsed: object = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ComposeValidationError("compose file could not be parsed") from error
    return _mapping(parsed, "compose document")


def _volume_mappings(
    service: Mapping[str, object], service_name: str
) -> tuple[Mapping[str, object], ...]:
    volumes = _sequence(service.get("volumes", []), f"{service_name}.volumes")
    return tuple(
        _mapping(volume, f"{service_name}.volumes[{index}]") for index, volume in enumerate(volumes)
    )


def _assert_hardening(service_name: str, service: Mapping[str, object], raw_text: str) -> None:
    user = service.get("user")
    if not isinstance(user, str) or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", user):
        raise ComposeValidationError(f"{service_name} must use a fixed non-root UID:GID")
    if service.get("read_only") is not True:
        raise ComposeValidationError(f"{service_name} must have a read-only root filesystem")
    if _sequence(service.get("cap_drop"), f"{service_name}.cap_drop") != ["ALL"]:
        raise ComposeValidationError(f"{service_name} must drop every capability")
    security_options = _sequence(service.get("security_opt"), f"{service_name}.security_opt")
    if "no-new-privileges:true" not in security_options:
        raise ComposeValidationError(f"{service_name} must set no-new-privileges")
    tmpfs = _sequence(service.get("tmpfs"), f"{service_name}.tmpfs")
    targets = tuple(
        item.split(":", maxsplit=1)[0]
        for item in tmpfs
        if isinstance(item, str) and item.startswith("/")
    )
    if len(targets) != len(tmpfs) or len(set(targets)) != len(targets):
        raise ComposeValidationError(f"{service_name} tmpfs targets must be unique absolute paths")
    if targets.count("/tmp") != 1:
        raise ComposeValidationError(f"{service_name} must have one independent /tmp tmpfs")
    documented_targets = tuple(
        match.group("target")
        for match in _TMPFS_COMMENT.finditer(raw_text)
        if match.group("service") == service_name
    )
    if sorted(documented_targets) != sorted(targets):
        raise ComposeValidationError(
            f"{service_name} must document the purpose of every writable tmpfs path"
        )


def _assert_image(service_name: str, service: Mapping[str, object]) -> None:
    image = service.get("image")
    if not isinstance(image, str) or ":" not in image or image.endswith(":"):
        raise ComposeValidationError(f"{service_name} image must have an exact version tag")
    if _LATEST_TAG.search(image):
        raise ComposeValidationError(f"{service_name} image must not use latest")


def _assert_no_plaintext_password(service_name: str, service: Mapping[str, object]) -> None:
    environment = _mapping(service.get("environment", {}), f"{service_name}.environment")
    forbidden = [name for name in environment if "PASSWORD" in name and not name.endswith("_FILE")]
    if forbidden:
        raise ComposeValidationError(
            f"{service_name} exposes a database password through environment: {sorted(forbidden)}"
        )


def _assert_mounts(services: Mapping[str, object], raw_text: str) -> None:
    if any(marker in raw_text for marker in DOCKER_SOCKET_MARKERS):
        raise ComposeValidationError("Docker Socket must not appear anywhere in Compose")
    found: list[tuple[str, Mapping[str, object]]] = []
    for service_name, raw_service in services.items():
        service = _mapping(raw_service, f"services.{service_name}")
        for volume in _volume_mappings(service, service_name):
            source = volume.get("source")
            target = volume.get("target")
            if (
                source == AGENT_DIRECTORY
                or target == AGENT_DIRECTORY
                or (isinstance(source, str) and "holm" in source)
            ):
                found.append((service_name, volume))
    if len(found) != 1:
        raise ComposeValidationError("Agent Socket directory must be mounted exactly once")
    service_name, mount = found[0]
    if service_name != "control-api":
        raise ComposeValidationError("Agent Socket directory belongs only to control-api")
    if mount.get("source") != AGENT_DIRECTORY or mount.get("target") != AGENT_DIRECTORY:
        raise ComposeValidationError(
            "Agent mount must expose the socket directory, not the socket file"
        )
    if mount.get("read_only") is not True:
        raise ComposeValidationError("Agent Socket directory mount must be read-only")


def _assert_networks(document: Mapping[str, object], services: Mapping[str, object]) -> None:
    networks = _mapping(document.get("networks"), "networks")
    if frozenset(networks) != EXPECTED_NETWORKS:
        raise ComposeValidationError("Compose network set differs from the reviewed set")
    runtime = _mapping(networks["holm-runtime"], "networks.holm-runtime")
    if runtime.get("internal") is not True:
        raise ComposeValidationError("Runtime network must be internal")
    postgres = _mapping(services["postgres"], "services.postgres")
    if _sequence(postgres.get("networks"), "postgres.networks") != ["holm-app"]:
        raise ComposeValidationError("PostgreSQL must join only the App network")


def _assert_ports(services: Mapping[str, object]) -> None:
    publishers: list[str] = []
    for name, raw_service in services.items():
        service = _mapping(raw_service, f"services.{name}")
        ports = _sequence(service.get("ports", []), f"{name}.ports")
        if ports:
            publishers.append(name)
        for port in ports:
            if not isinstance(port, str) or not port.startswith("127.0.0.1:"):
                raise ComposeValidationError(f"{name} ports must bind to 127.0.0.1")
    if publishers != ["control-api"]:
        raise ComposeValidationError("control-api must be the only port-publishing service")


def _assert_dependencies(services: Mapping[str, object]) -> None:
    migrate = _mapping(services["migrate"], "services.migrate")
    migrate_dependencies = _mapping(migrate.get("depends_on"), "migrate.depends_on")
    postgres_dependency = _mapping(
        migrate_dependencies.get("postgres"), "migrate.depends_on.postgres"
    )
    if postgres_dependency.get("condition") != "service_healthy":
        raise ComposeValidationError("migrate must wait for healthy PostgreSQL")
    control = _mapping(services["control-api"], "services.control-api")
    control_dependencies = _mapping(control.get("depends_on"), "control-api.depends_on")
    expected = {
        "postgres": "service_healthy",
        "migrate": "service_completed_successfully",
    }
    actual = {
        name: _mapping(value, f"control-api.depends_on.{name}").get("condition")
        for name, value in control_dependencies.items()
    }
    if actual != expected:
        raise ComposeValidationError("control-api dependency chain must be PostgreSQL then migrate")


def validate_compose_text(text: str) -> tuple[str, ...]:
    """Validate one in-memory Compose copy and return the scanned service names."""
    document = _parse(text)
    if document.get("name") != "holm":
        raise ComposeValidationError("Compose Project Name must be holm")
    services = _mapping(document.get("services"), "services")
    if not services:
        raise ComposeValidationError("compose service set must not be empty")
    service_names = tuple(sorted(services))
    if frozenset(service_names) != EXPECTED_SERVICES:
        raise ComposeValidationError("Compose service set differs from the reviewed set")
    for service_name, raw_service in services.items():
        service = _mapping(raw_service, f"services.{service_name}")
        _assert_hardening(service_name, service, text)
        _assert_image(service_name, service)
        _assert_no_plaintext_password(service_name, service)
    _assert_mounts(services, text)
    _assert_networks(document, services)
    _assert_ports(services)
    _assert_dependencies(services)
    return service_names


def validate_compose_file(path: Path = COMPOSE_PATH) -> tuple[str, ...]:
    if not path.is_file():
        raise ComposeValidationError(f"compose file is missing: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ComposeValidationError("compose file could not be read") from error
    return validate_compose_text(text)


def main() -> int:
    try:
        services = validate_compose_file()
    except ComposeValidationError as error:
        print(f"development Compose validation failed: {error}", file=sys.stderr)
        return 1
    print(f"validated {len(services)} services: {', '.join(services)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
