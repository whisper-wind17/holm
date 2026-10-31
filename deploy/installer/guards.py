"""Validate the effective deployment, including installer overrides."""

from __future__ import annotations

import configparser
import copy
from pathlib import Path
from typing import Any

from engine import InstallError

# Docker builds read this literal via AST without importing installer dependencies.
RUNTIME_EXECUTABLE_DIRECTORY = "/opt/holm/runtime/bin"

HARDENING = {
    "NoNewPrivileges": "true",
    "PrivateTmp": "true",
    "ProtectSystem": "strict",
    "RestrictAddressFamilies": "AF_UNIX",
    "LimitNOFILE": "65536",
    "TasksMax": "256",
    "UMask": "0007",
}


def unit_values(text: str) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text)
        return dict(parser.items("Service"))
    except (configparser.Error, ValueError):
        raise InstallError("invalid_unit") from None


def effective_agent(
    base: str,
    dropin: str,
    *,
    run_root: Path,
    cache_root: Path,
    command: str,
    groups: tuple[int, ...],
    agent_name: str,
) -> dict[str, str]:
    effective = unit_values(base)
    if any(effective.get(key) != expected for key, expected in HARDENING.items()):
        raise InstallError("unsafe_agent_base_unit")
    if (
        effective.get("User") != agent_name
        or effective.get("Group") != agent_name
        or agent_name == "root"
    ):
        raise InstallError("unsafe_agent_identity")
    if effective.get("ReadWritePaths") != str(run_root):
        raise InstallError("unsafe_agent_base_paths")
    # Parse the exact systemd list/reset semantics used by our small drop-in.
    lines = [
        line.strip()
        for line in dropin.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    expected = [
        "[Service]",
        "ExecStart=",
        f"ExecStart={command}",
        f"ReadWritePaths={cache_root}",
        "SupplementaryGroups=" + " ".join(map(str, groups)),
    ]
    if lines != expected or cache_root == run_root or not cache_root.is_absolute():
        raise InstallError("unsafe_agent_dropin")
    effective["ExecStart"] = command
    effective["ReadWritePaths"] = f"{run_root} {cache_root}"
    effective["SupplementaryGroups"] = " ".join(map(str, groups))
    return effective


def verify_rendered_compose(
    base: dict[str, Any],
    rendered: dict[str, Any],
    *,
    config_root: Path,
    data_root: Path,
    external_model_root: Path,
    secrets_root: Path,
    project: str,
    images: dict[str, str],
    version: str,
    edge: dict[str, Any],
) -> None:
    """Check the Docker-normalized artifact against a closed mutation allowlist."""
    if rendered.get("name") != project or set(rendered.get("services", {})) != set(
        base["services"]
    ):
        raise InstallError("unexpected_compose_services")
    variables = {
        "HOLM_CONFIG_ROOT": str(config_root),
        "HOLM_DATA_ROOT": str(data_root),
        "HOLM_SECRETS_ROOT": str(secrets_root),
        "HOLM_EXTERNAL_MODEL_ROOT": str(external_model_root),
    }
    import re

    def expand(value: str) -> str:
        for key, replacement in variables.items():
            value = re.sub(r"\$\{" + key + r":-[^}]+\}", lambda _: replacement, value)
        return value

    def names(value: Any) -> set[str]:
        return (
            set(value)
            if isinstance(value, dict)
            else {item if isinstance(item, str) else item["source"] for item in value}
        )

    for name, service in rendered["services"].items():
        original = base["services"][name]
        allowed_keys = {
            "image",
            "user",
            "read_only",
            "cap_drop",
            "security_opt",
            "tmpfs",
            "command",
            "entrypoint",
            "secrets",
            "volumes",
            "networks",
            "depends_on",
            "healthcheck",
            "restart",
            "group_add",
            "ports",
            "environment",
        }
        if set(service) - allowed_keys:
            raise InstallError("unexpected_service_configuration")
        if service.get("tmpfs", []) != original.get("tmpfs", []):
            raise InstallError("unsafe_container_tmpfs")
        if isinstance(service.get("networks"), dict) and any(
            value not in (None, {}) for value in service["networks"].values()
        ):
            raise InstallError("unsafe_network_attachment")

        def secret_targets(items: list[Any]) -> dict[str, str]:
            targets = {}
            for item in items:
                if isinstance(item, str):
                    targets[item] = "/run/secrets/" + item
                else:
                    if set(item) - {"source", "target"}:
                        raise InstallError("unsafe_secret_mount")
                    targets[item["source"]] = item.get("target", "/run/secrets/" + item["source"])
            return targets

        if secret_targets(service.get("secrets", [])) != secret_targets(
            original.get("secrets", [])
        ):
            raise InstallError("unsafe_secret_mount")
        for mount in service.get("volumes", []):
            if set(mount) - {"type", "source", "target", "read_only", "bind"} or set(
                mount.get("bind", {})
            ) - {"create_host_path"}:
                raise InstallError("unsafe_bind_options")
        for key in ("user", "cap_drop", "security_opt", "read_only", "restart"):
            if service.get(key) != original.get(key):
                raise InstallError("unsafe_container_" + key)
        if (
            service.get("image") != images[name]
            or service.get("entrypoint") is not None
            or service.get("privileged")
            or service.get("cap_add")
            or service.get("network_mode")
        ):
            raise InstallError("unsafe_container_execution")
        if set(service.get("group_add", [])) != set(original.get("group_add", [])):
            raise InstallError("unsafe_container_groups")

        def healthcheck(value: dict[str, Any] | None) -> dict[str, Any]:
            from decimal import Decimal

            result = dict(value or {})
            factors = {
                "ns": 1,
                "us": 1000,
                "µs": 1000,
                "ms": 10**6,
                "s": 10**9,
                "m": 60 * 10**9,
                "h": 3600 * 10**9,
            }
            for key in ("interval", "timeout", "start_period", "start_interval"):
                if key in result:
                    raw = result[key]
                    parts = re.findall(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)", raw)
                    if not parts or "".join(number + unit for number, unit in parts) != raw:
                        raise InstallError("invalid_healthcheck_duration")
                    result[key] = sum(Decimal(number) * factors[unit] for number, unit in parts)
            return result

        if healthcheck(service.get("healthcheck")) != healthcheck(original.get("healthcheck")):
            raise InstallError("unsafe_container_healthcheck")
        expected_command = [expand(arg) for arg in original.get("command", [])]
        if "--build-version" in expected_command:
            expected_command[expected_command.index("--build-version") + 1] = version
        if (service.get("command") or []) != expected_command:
            raise InstallError("unsafe_container_command")
        if (
            service.get("environment", {}) != original.get("environment", {})
            or names(service.get("secrets", [])) != names(original.get("secrets", []))
            or names(service.get("networks", [])) != names(original.get("networks", []))
        ):
            raise InstallError("unsafe_container_access")
        expected_mounts = {
            (expand(mount["source"]), expand(mount["target"]), mount.get("read_only", False))
            for mount in original.get("volumes", [])
        }
        if name in {"control-api", "gateway", "instance-controller"}:
            expected_mounts.add(
                (
                    str(config_root / "runtime-compat.yaml"),
                    "/opt/holm/runtime-compat.yaml",
                    True,
                )
            )
        observed_mounts = {
            (mount["source"], mount["target"], mount.get("read_only", False))
            for mount in service.get("volumes", [])
        }
        if (
            observed_mounts != expected_mounts
            or len(observed_mounts) != len(service.get("volumes", []))
            or any(mount["type"] != "bind" for mount in service.get("volumes", []))
        ):
            raise InstallError("unsafe_container_mounts")
        ports = service.get("ports", [])
        if name != "edge" and ports:
            raise InstallError("unexpected_host_port")
        if name == "edge":
            observed_ports = set()
            for port in ports:
                if isinstance(port, str):
                    address, published, target = port.split(":")
                    observed_ports.add((address, int(published), int(target)))
                else:
                    if port.get("protocol", "tcp") != "tcp":
                        raise InstallError("unexpected_port_protocol")
                    observed_ports.add((port["host_ip"], int(port["published"]), port["target"]))
            if observed_ports != {
                (edge["address"], edge["http_port"], 8080),
                (edge["address"], edge["https_port"], 8443),
            }:
                raise InstallError("unsafe_edge_bind")
        dependencies = {
            key: value["condition"] for key, value in service.get("depends_on", {}).items()
        }
        if dependencies != {
            key: value["condition"] for key, value in original.get("depends_on", {}).items()
        }:
            raise InstallError("unexpected_startup_dependencies")
    if set(rendered.get("networks", {})) != set(base["networks"]):
        raise InstallError("unexpected_networks")
    for name, network in rendered["networks"].items():
        if (
            set(network) - {"name", "driver", "internal", "ipam"}
            or network.get("name", project + "_" + name) != project + "_" + name
        ):
            raise InstallError("unsafe_network_options")
        if network.get("ipam") not in (None, {}):
            raise InstallError("unsafe_network_ipam")
        if network.get("driver") != base["networks"][name]["driver"] or network.get(
            "internal", False
        ) != base["networks"][name].get("internal", False):
            raise InstallError("unsafe_network")
    if set(rendered.get("secrets", {})) != set(base["secrets"]):
        raise InstallError("unexpected_secret_set")
    for name, secret in rendered["secrets"].items():
        if (
            set(secret) - {"name", "file"}
            or secret.get("name", project + "_" + name) != project + "_" + name
        ):
            raise InstallError("unsafe_secret_options")
        if secret.get("file") != expand(base["secrets"][name]["file"]) or secret.get("external"):
            raise InstallError("unsafe_secret_source")


def production_catalog(
    source: dict[str, Any], digests: dict[str, str], cpu_count: int, gpu_count: int = 1
) -> dict[str, Any]:
    if cpu_count < 1 or gpu_count < 1:
        raise InstallError("invalid_cpu_count")
    catalog = copy.deepcopy(source)
    for runtime in catalog["runtimes"]:
        image = digests.get("runtime-" + runtime["name"])
        if image is None:
            raise InstallError("runtime_digest_missing")
        runtime["executable"] = f"{RUNTIME_EXECUTABLE_DIRECTORY}/{runtime['name']}"
        runtime["launcher"] = "container"
        runtime["image_digest"] = image
        # Libraries size thread pools from host topology even when quota is low.
        # Reserve eight threads per host CPU plus 512 service threads; quota is
        # a per-GPU upper bound capped by the node rather than a fixed two cores.
        runtime["container_resources"] = {
            "port": 8000,
            "shm_bytes_per_gpu": 2 * 1024**3,
            "tmpfs_bytes": 1024**3,
            "memory_bytes_per_gpu": 16 * 1024**3,
            "nano_cpus_per_gpu": max(1, cpu_count * 10**9 // gpu_count),
            "pids_limit": 512 + 8 * cpu_count,
        }
    return catalog


def effective_platform(
    base: str, dropin: str, *, original_project: str, project: str, compose_path: Path
) -> dict[str, str]:
    values = unit_values(base)
    original = f"/usr/bin/docker compose --project-name {original_project} --file {compose_path} up --detach"
    expected = f"/usr/bin/docker compose --project-name {project} --file {compose_path} up --detach"
    if (
        values.get("ExecStart") != original
        or values.get("Type") != "oneshot"
        or values.get("RemainAfterExit") != "yes"
    ):
        raise InstallError("unsafe_platform_base_unit")
    lines = [line.strip() for line in dropin.splitlines() if line.strip()]
    if lines != ["[Service]", "ExecStart=", "ExecStart=" + expected]:
        raise InstallError("unsafe_platform_dropin")
    values["ExecStart"] = expected
    return values
