"""FX-BRAND-02-STORAGE: frozen bytes from the pre-rename on-disk formats."""

import ast
import hashlib
import inspect
from uuid import UUID

from holm_download_worker.artifact_publishing import _artifact_id_for_task
from holm_security import backup, credentials


def test_BRAND_02_credential_storage_tag_is_frozen() -> None:
    assert credentials._AAD_PREFIX == b"privatews:model-source-credential:v1\x00"


def test_BRAND_02_backup_storage_tag_is_frozen() -> None:
    assert backup._AAD == b"privatews:backup-secrets:v1"


def test_BRAND_02_artifact_storage_tag_and_derived_id_are_frozen() -> None:
    tag = b"privatews:model-artifact:v1\0"
    literals = [
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(_artifact_id_for_task)))
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes)
    ]
    assert literals == [tag]
    task = UUID("01912345-6789-7abc-8def-0123456789ab")
    digest = hashlib.sha256(tag + task.bytes).digest()
    value = ((task.int >> 80) << 80) | (0x7 << 76)
    value |= int.from_bytes(digest[6:16], "big") & ((1 << 76) - 1)
    value = (value & ~(0b11 << 62)) | (0b10 << 62)
    assert _artifact_id_for_task(task) == UUID(int=value)
