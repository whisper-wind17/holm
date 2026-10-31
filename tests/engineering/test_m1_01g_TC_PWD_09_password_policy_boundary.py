"""Fail-loud traversal guard for user-selected password persistence."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (PROJECT_ROOT / "apps", PROJECT_ROOT / "packages")
TRUSTED_RAW_HASH_MODULES = frozenset(
    {
        Path("packages/security/src/holm_security/passwords.py"),
        Path("packages/security/src/holm_security/temporary_passwords.py"),
    }
)


@dataclass(frozen=True, slots=True)
class RawPasswordHashAccess:
    path: Path
    line: int


def _raw_password_hash_accesses(
    project_root: Path,
    production_roots: tuple[Path, ...],
) -> tuple[RawPasswordHashAccess, ...]:
    accesses: list[RawPasswordHashAccess] = []
    discovered = 0
    for root in production_roots:
        assert root.is_dir(), f"password policy traversal root is missing: {root}"
        for path in sorted(root.rglob("*.py")):
            discovered += 1
            relative = path.relative_to(project_root)
            if relative in TRUSTED_RAW_HASH_MODULES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "hash":
                    accesses.append(RawPasswordHashAccess(relative, node.lineno))
    assert discovered > 0, "password policy traversal discovered zero production Python files"
    return tuple(accesses)


def _run_boundary_gate(
    project_root: Path,
    production_roots: tuple[Path, ...],
) -> None:
    accesses = _raw_password_hash_accesses(project_root, production_roots)
    assert not accesses, (
        "production code accessed a raw password hash primitive; "
        f"user-selected passwords must call hash_user_password: {accesses}"
    )


def test_TC_PWD_09_all_production_password_hashing_uses_the_policy_boundary() -> None:
    _run_boundary_gate(PROJECT_ROOT, PRODUCTION_ROOTS)


def test_TC_PWD_09_new_direct_hash_and_persistence_bypass_fails_loudly(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    app_root = project_root / "apps/control_api"
    app_root.mkdir(parents=True)
    (app_root / "bypass.py").write_text(
        """async def bypass(users, hasher, user_id, password, now):
    password_hash = hasher.hash(password)
    await users.set_password(user_id, password_hash=password_hash, now=now)
""",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="raw password hash primitive"):
        _run_boundary_gate(project_root, (project_root / "apps",))
