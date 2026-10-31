"""Image-side operations. Never print Secret material or raw failures."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import tarfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4


def describe(payload: dict) -> dict:
    from holm_common.config import PlatformConfig
    from holm_contracts import ReleaseManifest
    from holm_contracts.release import PRODUCTION_AGENT_SOCKET_GID, PRODUCTION_SERVICE_IDS
    from holm_control_api.bootstrap_token import DEFAULT_BOOTSTRAP_TOKEN_PATH
    from holm_host_agent.server import AGENT_SOCKET_PATH
    from holm_runtimes import load_runtime_compatibility

    manifest = ReleaseManifest.model_validate(payload["manifest"])
    platform = PlatformConfig.model_validate(payload["platform"])
    if "catalog" in payload:
        import tempfile

        content = (json.dumps(payload["catalog"], indent=2) + "\n").encode()
        if hashlib.sha256(content).hexdigest() != manifest.runtime_compat_hash:
            raise RuntimeError("derived_catalog_hash_mismatch")
        with tempfile.NamedTemporaryFile() as temporary:
            temporary.write(content)
            temporary.flush()
            load_runtime_compatibility(Path(temporary.name))
    return {
        "identities": dict(PRODUCTION_SERVICE_IDS),
        "agent_gid": PRODUCTION_AGENT_SOCKET_GID,
        "bootstrap_path": str(DEFAULT_BOOTSTRAP_TOKEN_PATH),
        "agent_socket": str(AGENT_SOCKET_PATH),
        "platform": platform.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
    }


def export_agent() -> None:
    # pip is part of the immutable application build image. Its requirement
    # parser is used only here, never installed into the host Agent environment.
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name

    pending = ["holm-host-agent"]
    visited = set()
    members = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in visited:
            continue
        visited.add(name)
        distribution = importlib.metadata.distribution(name)
        for raw in distribution.requires or ():
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
        for relative in distribution.files or ():
            if ".." in relative.parts or relative.is_absolute() or "__pycache__" in relative.parts:
                continue
            source = Path(distribution.locate_file(relative))
            if source.is_symlink() or not source.is_file():
                raise RuntimeError("invalid_agent_distribution")
            members[str(relative)] = source
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for name, source in sorted(members.items()):
            archive.add(source, arcname=name, recursive=False)


def validate_secrets(payload: dict) -> None:
    from holm_security import load_master_keyring

    root = Path(payload["root"])
    identities = payload["identities"]
    load_master_keyring(root / "master_keyring", expected_gid=identities["control-api"][1])


def database(payload: dict) -> dict:
    import sqlalchemy as sa
    from holm_persistence.bootstrap import BOOTSTRAP_TOKENS, SYSTEM_INITIALIZATION
    from holm_persistence.install_roles import LOGIN_GROUPS, provision_login_roles
    from holm_security import hash_token
    from sqlalchemy.engine import make_url

    root = Path(payload["root"])
    engine = sa.create_engine(
        make_url((root / "migrate_database_url").read_text().strip()), hide_parameters=True
    )
    passwords = {name: make_url((root / name).read_text()).password for name in LOGIN_GROUPS}
    with engine.begin() as connection:
        provision_login_roles(connection, passwords)
    # Exercise the actual credentials and INHERIT, rather than SET ROLE as owner.
    for name, group in LOGIN_GROUPS.items():
        login = sa.create_engine(make_url((root / name).read_text()), hide_parameters=True)
        try:
            with login.connect() as connection:
                if connection.scalar(sa.text("SELECT current_user")) != group + "_login":
                    raise RuntimeError("login_identity_mismatch")
                if not connection.scalar(
                    sa.text("SELECT pg_has_role(current_user, :parent, 'USAGE')"), {"parent": group}
                ):
                    raise RuntimeError("login_grants_unavailable")
        finally:
            login.dispose()
    token_status = "initialized"
    with engine.begin() as connection:
        if connection.execute(sa.select(SYSTEM_INITIALIZATION.c.id)).first() is None:
            token = (root / payload["bootstrap_name"]).read_text().strip()
            digest = hash_token(token)
            row = (
                connection.execute(
                    sa.select(BOOTSTRAP_TOKENS).where(BOOTSTRAP_TOKENS.c.token_hash == digest)
                )
                .mappings()
                .first()
            )
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            if row is None:
                connection.execute(
                    sa.insert(BOOTSTRAP_TOKENS).values(
                        id=uuid4(),
                        token_hash=digest,
                        created_at=now,
                        expires_at=now + timedelta(minutes=30),
                    )
                )
                token_status = "valid"
            else:
                token_status = (
                    "valid"
                    if row["consumed_at"] is None and row["expires_at"] > now
                    else "expired_or_used"
                )
    engine.dispose()
    return {"bootstrap": token_status}


def smoke(payload: dict) -> None:
    import grpc
    from holm_contracts.agent.v1 import agent_pb2, agent_pb2_grpc

    with grpc.insecure_channel("unix:" + payload["socket"]) as channel:
        response = agent_pb2_grpc.HostAgentStub(channel).ListGPUs(
            agent_pb2.ListGPUsRequest(), timeout=10
        )
        if response.discovery_state != agent_pb2.GPU_DISCOVERY_STATE_READY or not response.devices:
            raise RuntimeError("gpu_inventory_empty")


def validate_tls(
    payload: dict,
    certificate: Path = Path("/run/tls/certificate"),
    private_key: Path = Path("/run/tls/private-key"),
) -> dict:
    """Return public leaf and digests only; never serialize key material or parser errors."""
    import ipaddress
    from datetime import UTC, datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        cert_bytes = certificate.read_bytes()
        key_bytes = private_key.read_bytes()
        leaf = x509.load_pem_x509_certificates(cert_bytes)[0]
        try:
            if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                return {"error": "tls_invalid_material"}
        except x509.ExtensionNotFound:
            pass
        key = serialization.load_pem_private_key(key_bytes, password=None)
        encoding = serialization.Encoding.DER
        form = serialization.PublicFormat.SubjectPublicKeyInfo
        if leaf.public_key().public_bytes(encoding, form) != key.public_key().public_bytes(
            encoding, form
        ):
            return {"error": "tls_key_mismatch"}
        now = datetime.now(UTC)
        if not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc:
            return {"error": "tls_not_valid"}
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        host = payload["hostname"].lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:

            def matches(name: str) -> bool:
                name = name.lower()
                return name == host or (
                    name.startswith("*.")
                    and host.count(".") == name.count(".")
                    and host.endswith(name[1:])
                )

            matched = any(matches(name) for name in san.get_values_for_type(x509.DNSName))
        else:
            matched = address in san.get_values_for_type(x509.IPAddress)
        if not matched:
            return {"error": "tls_hostname_mismatch"}
        return {
            "leaf_der": leaf.public_bytes(encoding).hex(),
            "certificate_sha256": hashlib.sha256(cert_bytes).hexdigest(),
            "private_key_sha256": hashlib.sha256(key_bytes).hexdigest(),
            "expires_soon": leaf.not_valid_after_utc - now < timedelta(days=30),
        }
    except Exception:
        return {"error": "tls_invalid_material"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "describe",
            "export-agent",
            "secrets",
            "database",
            "smoke",
            "validate-pepper",
            "validate-tls",
        ),
    )
    args = parser.parse_args()
    try:
        if args.action == "export-agent":
            export_agent()
            return 0
        payload = json.load(sys.stdin)
        result = {}
        if args.action == "describe":
            result = describe(payload)
        elif args.action == "secrets":
            from holm_persistence.install_secrets import provision_secrets

            created = provision_secrets(
                Path(payload["root"]),
                payload["identities"],
                database=payload["database"],
                bootstrap_user=payload["bootstrap_user"],
                bootstrap_name=payload["bootstrap_name"],
            )
            validate_secrets(payload)
            result = {"created": created}
        elif args.action == "validate-pepper":
            from holm_security import load_api_key_pepper_keyring

            load_api_key_pepper_keyring(Path("/run/secrets/validate"))
        elif args.action == "validate-tls":
            result = validate_tls(payload)
        elif args.action == "database":
            result = database(payload)
        elif args.action == "smoke":
            smoke(payload)
        print(json.dumps(result))
        return 0
    except Exception:
        print("Image operation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
