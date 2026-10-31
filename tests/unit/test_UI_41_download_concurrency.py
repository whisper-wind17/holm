"""UI-41: FX-UI-41 uses synthetic CLI arguments without reading secrets or networking."""

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from holm_download_worker import __main__ as entrypoint
from holm_download_worker.configuration import DownloadWorkerConfigurationError
from holm_download_worker.download_subprocess import ProductionDownloadProcess


@pytest.fixture
def worker_args() -> list[str]:
    return [
        "--database-url-file",
        "/unused/database",
        "--master-keyring-file",
        "/unused/keyring",
        "--instance-id",
        "FX-UI-41",
        "--build-version",
        "test",
    ]


@pytest.mark.parametrize(("extra", "expected"), [([], 16), (["--download-max-workers", "4"], 4)])
def test_UI_41_configuration_reaches_production_process(
    monkeypatch: pytest.MonkeyPatch, worker_args: list[str], extra: list[str], expected: int
) -> None:
    configuration = entrypoint._configuration(entrypoint._parse_args(worker_args + extra))
    assert configuration.download_max_workers == expected
    engine = Mock(dispose=AsyncMock())
    monkeypatch.setattr(entrypoint, "load_database_url", Mock())
    monkeypatch.setattr(entrypoint, "create_database_engine", Mock(return_value=engine))
    monkeypatch.setattr(entrypoint, "create_session_factory", Mock())
    for name in (
        "SubprocessModelResolver",
        "ProductionResolutionProcess",
        "DownloadResolutionJobHandler",
        "DownloadStagingLifecycle",
        "SubprocessModelDownloader",
        "DownloadArtifactPublisher",
        "DownloadWorker",
        "ArtifactVerificationHandler",
        "ArtifactDeletionHandler",
    ):
        monkeypatch.setattr(entrypoint, name, Mock())
    production = Mock(wraps=ProductionDownloadProcess)
    monkeypatch.setattr(entrypoint, "ProductionDownloadProcess", production)
    monkeypatch.setattr(entrypoint, "run_until_signalled", AsyncMock())
    asyncio.run(entrypoint._run(configuration))
    production.assert_called_once_with(
        database_url_file=configuration.database_url_file,
        master_keyring_file=configuration.master_keyring_file,
        max_workers=expected,
    )
    engine.dispose.assert_awaited_once()


def test_UI_41_direct_process_default_matches_configuration(worker_args: list[str]) -> None:
    configuration = entrypoint._configuration(entrypoint._parse_args(worker_args))
    process = ProductionDownloadProcess(Path("/unused/database"), Path("/unused/keyring"))
    assert process.max_workers == configuration.download_max_workers == 16


@pytest.mark.parametrize("value", [0, 65, 1.5, "4", True])
def test_UI_41_configuration_rejects_invalid_workers(worker_args: list[str], value: object) -> None:
    configuration = entrypoint._configuration(entrypoint._parse_args(worker_args))
    with pytest.raises(DownloadWorkerConfigurationError) as error:
        replace(configuration, download_max_workers=value)
    assert error.value.code == "download_max_workers_invalid"


@pytest.mark.parametrize("value", ["0", "65", "1.5", "invalid"])
def test_UI_41_invalid_cli_fails_before_startup(
    monkeypatch: pytest.MonkeyPatch, worker_args: list[str], value: str
) -> None:
    run = Mock()
    monkeypatch.setattr(entrypoint, "_run", run)
    with pytest.raises(SystemExit) as error:
        entrypoint.main(worker_args + ["--download-max-workers", value])
    assert error.value.code in (1, 2)
    run.assert_not_called()


@pytest.mark.parametrize("value", [1, 64])
def test_UI_41_valid_range_endpoints(worker_args: list[str], value: int) -> None:
    configuration = entrypoint._configuration(
        entrypoint._parse_args(worker_args + ["--download-max-workers", str(value)])
    )
    assert configuration.download_max_workers == value


@pytest.mark.parametrize("workers", [16, 4])
@pytest.mark.parametrize("source", ["hugging_face", "modelscope"])
def test_UI_41_sdk_receives_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workers: int, source: str
) -> None:
    from holm_download_worker import download_subprocess

    sdk = Mock()
    module = "huggingface_hub" if source == "hugging_face" else "modelscope"
    monkeypatch.setattr(f"{module}.snapshot_download", sdk)
    monkeypatch.setattr(
        download_subprocess, "install_staging_write_fence", Mock(return_value=Mock())
    )
    monkeypatch.setattr(
        download_subprocess, "_prepare_modelscope_staging_state", Mock(return_value=tmp_path)
    )
    request = Mock(
        repository_id="FX-UI-41/model",
        resolved_revision="fixed",
        staging_path=tmp_path,
        endpoint="https://source.invalid",
    )
    process = ProductionDownloadProcess(
        Path("/unused/database"), Path("/unused/keyring"), max_workers=workers
    )
    getattr(process, f"_download_{source}")(request, Mock(), None)
    sdk.assert_called_once()
    assert sdk.call_args.kwargs["max_workers"] == workers
