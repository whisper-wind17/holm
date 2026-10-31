#!/usr/bin/env python3
"""Enforce the repository's apps-to-packages dependency direction."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DOMAIN_FORBIDDEN_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "alembic",
        "argon2",
        "cryptography",
        "docker",
        "fastapi",
        "hf_xet",
        "huggingface_hub",
        "modelscope",
        "modelscope_hub",
        "nvidia_ml_py",
        "nvidia_smi",
        "pynvml",
        "sglang",
        "sqlalchemy",
        "starlette",
        "vllm",
    }
)
DOMAIN_FORBIDDEN_WORKSPACE_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "holm_contracts",
        "holm_downloads",
        "holm_ledger",
        "holm_persistence",
        "holm_protocols",
        "holm_runtimes",
        "holm_security",
        "holm_telemetry",
    }
)
DOMAIN_FORBIDDEN_TYPESCRIPT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "@prisma",
        "dockerode",
        "fastify",
        "next",
        "react",
        "react-dom",
        "typeorm",
        "vite",
    }
)
SKIPPED_PARTS: Final[frozenset[str]] = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "dist", "node_modules"}
)
TYPESCRIPT_IMPORT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:import|export)\s+(?:type\s+)?(?:[^\"'`;]*?\s+from\s+)?[\"']([^\"']+)[\"']"
    r"|(?:import|require)\(\s*[\"']([^\"']+)[\"']\s*\)",
    re.MULTILINE,
)


@dataclass(frozen=True, order=True)
class Violation:
    path: Path
    line: int
    imported_name: str
    rule: str

    def render(self, root: Path) -> str:
        relative_path = self.path.relative_to(root)
        return f"{relative_path}:{self.line}: {self.rule}: forbidden import '{self.imported_name}'"


def _is_source_file(path: Path, suffixes: frozenset[str]) -> bool:
    return path.suffix in suffixes and not any(part in SKIPPED_PARTS for part in path.parts)


def _python_imports(path: Path) -> tuple[list[tuple[int, str]], list[Violation]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        line = error.lineno if isinstance(error, SyntaxError) and error.lineno else 1
        return [], [Violation(path, line, "<unparseable>", "DEP-PARSE")]

    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append((node.lineno, node.module))
            else:
                imports.extend((node.lineno, alias.name) for alias in node.names)
    return imports, []


def _workspace_python_roots(root: Path, top_level: str) -> set[str]:
    module_roots: set[str] = set()
    base = root / top_level
    if not base.is_dir():
        return module_roots

    for source_root in base.glob("**/src"):
        if any(part in SKIPPED_PARTS for part in source_root.parts):
            continue
        for candidate in source_root.iterdir():
            if candidate.is_dir() and (candidate / "__init__.py").is_file():
                module_roots.add(candidate.name)
    return module_roots


def _workspace_node_names(root: Path, top_level: str) -> set[str]:
    package_names: set[str] = set()
    base = root / top_level
    if not base.is_dir():
        return package_names

    for manifest in base.glob("**/package.json"):
        if any(part in SKIPPED_PARTS for part in manifest.parts):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("name")
        if isinstance(name, str):
            package_names.add(name)
    return package_names


def _root_name(imported_name: str) -> str:
    return imported_name.split(".", maxsplit=1)[0]


def _typescript_root_name(imported_name: str) -> str:
    if imported_name.startswith("@"):
        return imported_name.split("/", maxsplit=2)[0]
    return imported_name.split("/", maxsplit=1)[0]


def _targets_apps(imported_name: str, source_file: Path, root: Path) -> bool:
    if imported_name == "apps" or imported_name.startswith(("apps/", "apps.")):
        return True
    if not imported_name.startswith("."):
        return False

    resolved = (source_file.parent / imported_name).resolve()
    return resolved.is_relative_to((root / "apps").resolve())


def _is_inference_core(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return relative.is_relative_to(Path("packages/protocols/src/holm_protocols"))


def _targets_openai_adapter(imported_name: str) -> bool:
    return imported_name == "holm_gateway" or imported_name.startswith("holm_gateway.")


def _check_python(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    packages_root = root / "packages"
    domain_root = packages_root / "domain"
    app_module_roots = _workspace_python_roots(root, "apps")

    if not packages_root.is_dir():
        return [Violation(packages_root, 1, "<missing>", "DEP-CONFIG")]

    for path in packages_root.rglob("*.py"):
        if not _is_source_file(path, frozenset({".py"})):
            continue
        imports, parse_violations = _python_imports(path)
        violations.extend(parse_violations)
        for line, imported_name in imports:
            imported_root = _root_name(imported_name)
            if _is_inference_core(path, root) and _targets_openai_adapter(imported_name):
                violations.append(Violation(path, line, imported_name, "DEP-INFERENCE-CORE-OPENAI"))
            if imported_root in app_module_roots or imported_root == "apps":
                violations.append(Violation(path, line, imported_name, "DEP-PACKAGE-TO-APP"))
            if not path.is_relative_to(domain_root):
                continue
            if imported_root in DOMAIN_FORBIDDEN_ROOTS:
                violations.append(Violation(path, line, imported_name, "DEP-DOMAIN-EXTERNAL"))
            if imported_root in DOMAIN_FORBIDDEN_WORKSPACE_ROOTS:
                violations.append(Violation(path, line, imported_name, "DEP-DOMAIN-ADAPTER"))
    return violations


def _typescript_imports(path: Path) -> Iterable[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    for match in TYPESCRIPT_IMPORT_RE.finditer(text):
        imported_name = match.group(1) or match.group(2)
        line = text.count("\n", 0, match.start()) + 1
        yield line, imported_name


def _check_typescript(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    packages_root = root / "packages"
    domain_root = packages_root / "domain"
    app_package_names = _workspace_node_names(root, "apps")

    for path in packages_root.rglob("*"):
        if not _is_source_file(path, frozenset({".js", ".jsx", ".mjs", ".ts", ".tsx"})):
            continue
        try:
            imports = tuple(_typescript_imports(path))
        except OSError:
            violations.append(Violation(path, 1, "<unreadable>", "DEP-PARSE"))
            continue
        for line, imported_name in imports:
            if imported_name in app_package_names or _targets_apps(imported_name, path, root):
                violations.append(Violation(path, line, imported_name, "DEP-PACKAGE-TO-APP"))
            if path.is_relative_to(domain_root):
                imported_root = _typescript_root_name(imported_name)
                if imported_root in DOMAIN_FORBIDDEN_TYPESCRIPT_ROOTS:
                    violations.append(Violation(path, line, imported_name, "DEP-DOMAIN-EXTERNAL"))
    return violations


def check_dependencies(root: Path) -> list[Violation]:
    resolved_root = root.resolve()
    return sorted([*_check_python(resolved_root), *_check_typescript(resolved_root)])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="workspace root to inspect (defaults to this repository)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    if not (root / "apps").is_dir() or not (root / "packages").is_dir():
        print(
            f"ERROR: dependency guard root must contain apps/ and packages/: {root}",
            file=sys.stderr,
        )
        return 2

    violations = check_dependencies(root)
    if violations:
        for violation in violations:
            print(violation.render(root), file=sys.stderr)
        print(f"dependency guard failed: {len(violations)} violation(s)", file=sys.stderr)
        return 1

    print("dependency guard passed: apps -> packages; domain boundary is clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
