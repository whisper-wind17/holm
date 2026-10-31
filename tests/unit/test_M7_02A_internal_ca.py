"""FX-M7-02A: runtime-generated public CA; fake stages never install on a host."""

from __future__ import annotations

import builtins
import io
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_M7_04_installer import engine, install


@pytest.fixture
def root_certificate(tmp_path: Path) -> tuple[Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "FX-M7-02A")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "data/caddy/data/caddy/pki/authorities/local/root.crt"
    path.parent.mkdir(parents=True)
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path, cert.fingerprint(hashes.SHA256()).hex(":").upper()


def test_M7_02A_fingerprint_matches_openssl(root_certificate: tuple[Path, str]) -> None:
    path, expected = root_certificate
    if shutil.which("openssl"):
        result = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout", "-fingerprint", "-sha256"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip().split("=", 1)[1] == expected
    assert install.certificate_fingerprint(path) == expected


def fake_installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    subject = object.__new__(install.Installer)
    subject.config_root = tmp_path / "config"
    subject.state_path = subject.config_root / "install-state.json"
    subject.identity_config = {"fixture": "FX-M7-02A"}
    subject.data_root = tmp_path / "data"
    subject.config = {"edge": {"tls": "internal"}, "public_base_url": "https://fixture.invalid"}
    subject.project = "fixture"
    subject.base = {"networks": {"fixture": {}}}
    subject.details = {"bootstrap_path": "bootstrap-token"}
    subject.secrets_root = tmp_path / "secrets"
    subject.secrets_root.mkdir()
    (subject.secrets_root / "bootstrap-token").write_text("FX-M7-02A-SECRET-SENTINEL")
    calls = []
    for stage in engine.STAGES:
        monkeypatch.setattr(subject, stage, lambda stage=stage: calls.append(stage))
    monkeypatch.setattr(subject, "secret_payload", lambda: {})
    monkeypatch.setattr(subject, "image_operation", lambda *a, **kw: {"bootstrap": "valid"})
    return subject, calls


@pytest.mark.parametrize("terminal_available", [True, False])
def test_M7_02A_success_and_completed_journal_repeat_output_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    root_certificate: tuple[Path, str],
    terminal_available: bool,
) -> None:
    subject, calls = fake_installer(tmp_path, monkeypatch)
    delivered = []
    real_open = builtins.open

    class Terminal(io.StringIO):
        def close(self) -> None:
            delivered.append(self.getvalue())
            super().close()

    def open_terminal(path, *args, **kwargs):
        if path == "/dev/tty":
            if not terminal_available:
                raise OSError("no terminal")
            return Terminal()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_terminal)
    path, fingerprint = root_certificate
    for _ in range(2):
        assert subject.run() == 0
        captured = capsys.readouterr()
        assert f"tls=internal ca_certificate={path} sha256={fingerprint}" in captured.out
        assert (
            "warning=Internal certificates are only for testing and isolated networks"
            in captured.out
        )
        assert (
            "Node/Electron" in captured.out
            and "Production deployments require trusted" in captured.out
        )
        assert "FX-M7-02A-SECRET-SENTINEL" not in captured.out + captured.err
    assert calls == ["preflight", *engine.STAGES[1:], "preflight"]
    assert bool(delivered) == terminal_available
    if delivered:
        assert all("#token=FX-M7-02A-SECRET-SENTINEL" in value for value in delivered)
    assert "FX-M7-02A-SECRET-SENTINEL" not in subject.state_path.read_text()


@pytest.mark.parametrize(
    "content",
    [None, "not a certificate", "-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----\n"],
)
def test_M7_02A_missing_or_invalid_root_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
) -> None:
    subject, _ = fake_installer(tmp_path, monkeypatch)
    path = subject.data_root / "caddy/data/caddy/pki/authorities/local/root.crt"
    if content is not None:
        path.parent.mkdir(parents=True)
        path.write_text(content)
    # Exercise the actual CLI error boundary without any host installation stages.
    monkeypatch.setattr(install, "load_configuration", lambda _: {})
    monkeypatch.setattr(install, "Installer", lambda _: subject)
    monkeypatch.setattr(install.signal, "signal", lambda *args: None)
    monkeypatch.setattr(install.sys, "argv", ["holm", "install", "--config", "fixture.json"])
    assert install.main() == 20
    captured = capsys.readouterr()
    assert "code=internal_ca_unavailable" in captured.err
    assert "tls=internal" not in captured.out
    assert "FX-M7-02A-SECRET-SENTINEL" not in captured.out + captured.err
