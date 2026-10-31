"""FX-M7-02B: runtime-generated certificates; temporary trees and no host installation."""

from __future__ import annotations

import copy
import importlib.util
import ipaddress
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_M7_04_installer import ROOT, engine, fixture_installer, install, module

helper = module("image_helper")


def certificate_pair(tmp_path, *, host="holm.example.test", days=90, future=False, encrypted=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FX-M7-02B")])
    now = datetime.now(UTC)
    try:
        san = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        san = x509.DNSName(host)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=1) if future else now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "certificate.pem", tmp_path / "key.pem"
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"FX-M7-02B")
            if encrypted
            else serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


def configured(tmp_path, mode="internal"):
    subject = fixture_installer(tmp_path)
    subject.config["public_base_url"] = "https://holm.example.test:18443"
    subject.config["edge"]["tls"] = mode
    if mode == "acme":
        subject.config["edge"]["http_port"] = 80
    if mode == "provided":
        cert, key = certificate_pair(tmp_path)
        subject.config["edge"].update(certificate_file=str(cert), private_key_file=str(key))
    return subject


def load_config(tmp_path, subject):
    path = tmp_path / "install.json"
    path.write_text(json.dumps(subject.config))
    return install.load_configuration(path)


@pytest.mark.parametrize("mode", ["internal", "acme", "provided"])
def test_M7_02B_TC_DP_10_configuration_and_exact_render(tmp_path, mode):
    subject = configured(tmp_path, mode)
    load_config(tmp_path, subject)
    subject.details = {"platform": {"public_base_url": subject.config["public_base_url"]}}
    written = {}
    subject.directory = lambda *args: None
    subject.write = lambda path, content, *args: written.setdefault(path, content)
    subject.install_provided_tls = lambda: None
    subject.image_operation = lambda *args, **kwargs: {}
    subject.compose = lambda *args: json.dumps(subject.rendered).encode()
    subject.configuration()  # Includes the unchanged rendered Compose guard.
    text = written[subject.config_root / "Caddyfile"].decode()
    directive = {
        "internal": "tls internal",
        "acme": "",
        "provided": "tls /etc/caddy/tls/certificate.pem /etc/caddy/tls/private-key.pem",
    }[mode]
    expected = (ROOT / "deploy/caddy/Caddyfile.tmpl").read_text()
    for name, value in {
        "SITE_ADDRESS": "https://holm.example.test:8443",
        "TLS_DIRECTIVE": directive,
        "CONTROL_UPSTREAM": "control-api:8000",
        "GATEWAY_UPSTREAM": "gateway:8001",
        "WEB_ROOT": "/srv/holm",
        "LOG_DIR": "/data",
    }.items():
        expected = expected.replace("{{" + name + "}}", value)
    expected = (
        "{\n http_port 8080\n https_port 8443\n auto_https disable_redirects\n"
        " default_sni holm.example.test\n skip_install_trust\n}\n"
        + expected
        + "\nhttp://holm.example.test:8080 {\n redir https://holm.example.test:18443{uri} 308\n}\n"
    )
    assert text == expected
    mount = next(
        m
        for m in subject.rendered["services"]["edge"]["volumes"]
        if m["target"] == "/etc/caddy/tls"
    )
    assert mount["read_only"] is True
    changed = copy.deepcopy(subject.rendered)
    next(m for m in changed["services"]["edge"]["volumes"] if m["target"] == "/etc/caddy/tls")[
        "read_only"
    ] = False
    with pytest.raises(engine.InstallError, match="unsafe_container_mounts"):
        subject.verify_compose(changed)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"tls": "unknown"}, "invalid_edge_configuration"),
        ({"extra": "x"}, "invalid_edge_configuration"),
        ({"acme_email": "x"}, "invalid_edge_configuration"),
        ({"tls": "provided"}, "invalid_edge_configuration"),
        ({"tls": "acme"}, "acme_challenge_port_unavailable"),
        ({"tls": "acme", "http_port": 80, "acme_email": "x\n}"}, "invalid_acme_email"),
    ],
)
def test_M7_02B_invalid_configuration(tmp_path, change, code):
    subject = configured(tmp_path)
    subject.config["edge"].update(change)
    with pytest.raises(engine.InstallError, match=code):
        load_config(tmp_path, subject)


def test_M7_02B_acme_ip_and_provided_path_rejected(tmp_path):
    subject = configured(tmp_path, "acme")
    subject.config["public_base_url"] = "https://127.0.0.1:18443"
    with pytest.raises(engine.InstallError, match="acme_requires_domain"):
        load_config(tmp_path, subject)
    subject = configured(tmp_path, "provided")
    subject.config["edge"]["private_key_file"] = "relative.pem"
    with pytest.raises(engine.InstallError, match="invalid_path"):
        load_config(tmp_path, subject)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "key.pem")
    subject.config["edge"]["private_key_file"] = str(link)
    with pytest.raises(engine.InstallError, match="symlink_path"):
        load_config(tmp_path, subject)


@pytest.mark.parametrize(
    "host,requested,matched",
    [
        ("holm.example.test", "holm.example.test", True),
        ("*.example.test", "holm.example.test", True),
        ("*.example.test", "a.b.example.test", False),
        ("*.example.test", "example.test", False),
        ("127.0.0.1", "127.0.0.1", True),
        ("127.0.0.1", "127.0.0.2", False),
    ],
)
def test_M7_02B_TC_DP_10_san(tmp_path, host, requested, matched):
    paths = certificate_pair(tmp_path, host=host)
    result = helper.validate_tls({"hostname": requested}, *paths)
    assert ("error" not in result) is matched
    if not matched:
        assert result["error"] == "tls_hostname_mismatch"


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"days": -1}, "tls_not_valid"),
        ({"future": True}, "tls_not_valid"),
        ({"encrypted": True}, "tls_invalid_material"),
    ],
)
def test_M7_02B_invalid_certificate(tmp_path, kwargs, code):
    paths = certificate_pair(tmp_path, **kwargs)
    assert helper.validate_tls({"hostname": "holm.example.test"}, *paths) == {"error": code}


def test_M7_02B_mismatch_and_non_pem(tmp_path):
    cert, key = certificate_pair(tmp_path)
    old_cert = cert.read_bytes()
    certificate_pair(tmp_path)
    cert.write_bytes(old_cert)
    assert (
        helper.validate_tls({"hostname": "holm.example.test"}, cert, key)["error"]
        == "tls_key_mismatch"
    )
    cert.write_bytes(b"invalid")
    assert (
        helper.validate_tls({"hostname": "holm.example.test"}, cert, key)["error"]
        == "tls_invalid_material"
    )


def wired_subject(tmp_path):
    subject = configured(tmp_path, "provided")
    subject.data_root = tmp_path / "managed"
    (subject.data_root / "caddy/tls").mkdir(parents=True)
    subject.details = {"identities": {"edge": [os.getuid(), os.getgid()]}}
    calls = []

    class Executor:
        def run(self, argv, *, payload, timeout):
            calls.append((argv, payload))
            assert "--log-driver" in argv and "none" in argv and "--read-only" in argv
            mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
            assert all(m.endswith(",readonly") for m in mounts)
            assert json.loads(payload) == {"hostname": "holm.example.test"}
            return json.dumps(
                helper.validate_tls(
                    json.loads(payload),
                    Path(subject.config["edge"]["certificate_file"]),
                    Path(subject.config["edge"]["private_key_file"]),
                )
            ).encode()

    subject.executor = Executor()
    return subject, calls


def test_M7_02B_atomic_replacement_and_no_secret_output(tmp_path, capsys):
    subject, calls = wired_subject(tmp_path)
    subject.journal = engine.Journal(tmp_path / "journal.json", subject.config)
    subject.install_provided_tls()
    target = subject.data_root / "caddy/tls/private-key.pem"
    first = target.read_bytes()
    source_cert, source_key = certificate_pair(tmp_path, days=10)
    subject.install_provided_tls()
    assert target.read_bytes() == source_key.read_bytes() != first
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    assert target.stat().st_uid == subject.details["identities"]["edge"][0]
    assert (
        subject.data_root / "caddy/tls/certificate.pem"
    ).read_bytes() == source_cert.read_bytes()
    before = {p.name: p.read_bytes() for p in target.parent.iterdir()}
    certificate_pair(tmp_path, host="wrong.test")
    with pytest.raises(engine.InstallError, match="tls_hostname_mismatch"):
        subject.install_provided_tls()
    assert before == {p.name: p.read_bytes() for p in target.parent.iterdir()}
    subject.journal.save()
    captured = capsys.readouterr()
    observed = repr(calls) + captured.out + captured.err + subject.journal.path.read_text()
    assert "PRIVATE KEY" not in observed
    assert first.decode() not in observed
    assert "expires_within_30_days" in captured.out
    assert not list(target.parent.glob(".tls-*"))


@pytest.mark.parametrize(
    "fault,code",
    [
        ("permissions", "tls_private_key_permissions"),
        ("owner", "tls_private_key_permissions"),
        ("missing", "tls_validation_failed"),
        ("directory", "tls_invalid_file"),
        ("symlink", "symlink_path"),
    ],
)
def test_M7_02B_file_security(tmp_path, fault, code):
    subject, calls = wired_subject(tmp_path)
    key = Path(subject.config["edge"]["private_key_file"])
    if fault == "permissions":
        key.chmod(0o644)
    elif fault == "owner":
        os.chown(key, 12345, 12345)
    else:
        key.unlink()
        if fault == "directory":
            key.mkdir()
        elif fault == "symlink":
            key.symlink_to(tmp_path / "certificate.pem")
    with pytest.raises(engine.InstallError, match=code):
        subject.validate_provided_tls()
    assert calls == []


def test_M7_02B_production_mount_guard_negative():
    spec = importlib.util.spec_from_file_location(
        "m702b_compose", ROOT / "scripts/verify_production_compose.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    ProductionComposeValidationError = validator.ProductionComposeValidationError
    validate_compose_text = validator.validate_compose_text
    text = (ROOT / "deploy/compose/compose.prod.yaml").read_text()
    validate_compose_text(text)
    data = json.loads("\n".join(line for line in text.splitlines() if not line.startswith("#")))
    next(m for m in data["services"]["edge"]["volumes"] if m["target"] == "/etc/caddy/tls")[
        "read_only"
    ] = False
    with pytest.raises(ProductionComposeValidationError):
        validate_compose_text(json.dumps(data))


@pytest.mark.parametrize(
    "mode,wrong_peer", [("provided", False), ("provided", True), ("acme", False)]
)
def test_M7_02B_TC_DP_10_local_https_smoke(tmp_path, monkeypatch, mode, wrong_peer):
    import http.server
    import ssl
    import threading

    subject, _ = wired_subject(tmp_path)
    cert = Path(subject.config["edge"]["certificate_file"])
    key = Path(subject.config["edge"]["private_key_file"])
    if wrong_peer:
        other = tmp_path / "other"
        other.mkdir()
        cert, key = certificate_pair(other)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    names = []
    context.set_servername_callback(lambda connection, name, ctx: names.append(name))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    subject.config["edge"]["https_port"] = server.server_port
    subject.config["edge"]["tls"] = mode
    if mode == "acme":
        # Stand in for the host trust store with the runtime-generated trusted leaf.
        trusted = ssl.create_default_context(cafile=str(cert))
        monkeypatch.setattr(install.ssl, "create_default_context", lambda: trusted)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        if wrong_peer:
            with pytest.raises(engine.InstallError, match="provided_tls_smoke_failed"):
                subject.trusted_tls_smoke()
        else:
            subject.trusted_tls_smoke()
        assert names and set(names) == {"holm.example.test"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_M7_02B_acme_timeout_is_bounded_and_sanitized(tmp_path, monkeypatch):
    subject = configured(tmp_path, "acme")
    clock = [0.0]
    monkeypatch.setattr(install.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        install.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def unavailable(*args, **kwargs):
        raise OSError("sensitive upstream exception")

    monkeypatch.setattr(install.socket, "create_connection", unavailable)
    with pytest.raises(engine.InstallError, match="acme_certificate_timeout") as error:
        subject.trusted_tls_smoke()
    assert clock[0] == 180
    assert "sensitive" not in str(error.value)
    assert "DNS" in error.value.remedy and "80/443" in error.value.remedy


def test_M7_02B_no_persistent_writes_before_provided_validation(tmp_path):
    subject, calls = wired_subject(tmp_path)
    subject.config_root = tmp_path / "not-created"
    subject.preflight = subject.validate_provided_tls
    Path(subject.config["edge"]["private_key_file"]).chmod(0o644)
    with pytest.raises(engine.InstallError, match="tls_private_key_permissions"):
        subject.run()
    assert not subject.config_root.exists()
    assert not calls
