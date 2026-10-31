"""RELEASE-02: lockfile-scoped runtime license inventory (standard library only)."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from dataclasses import dataclass
from email.message import Message
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERMISSIVE = {
    "MIT",
    "MIT-0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache-2.0",
    "ISC",
    "PSF-2.0",
    "OFL-1.1",
    "0BSD",
    "Unlicense",
    "Zlib",
    "Python-2.0",
    "CC0-1.0",
    "CC-BY-4.0",
    "BlueOak-1.0.0",
}
REVIEW = {"MPL-2.0", "EPL-1.0", "EPL-2.0", "CDDL-1.0", "CDDL-1.1"} | {
    f"{family}-{version}{suffix}"
    for family, versions in (("LGPL", ("2.0", "2.1", "3.0")),)
    for version in versions
    for suffix in ("", "-only", "-or-later", "+")
}
INCOMPATIBLE = {"SSPL-1.0", "Proprietary", "UNLICENSED"} | {
    f"{family}-{version}{suffix}"
    for family in ("GPL", "AGPL")
    for version in ("1.0", "2.0", "3.0")
    for suffix in ("", "-only", "-or-later", "+")
}
ALIASES = {
    "MIT License": "MIT",
    "3-Clause BSD License": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "Apache 2.0": "Apache-2.0",
    "Apache License, Version 2.0": "Apache-2.0",
    "BSD-3-Clause License": "BSD-3-Clause",
}
CLASSIFIERS = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}
# Only reviewed, version-pinned exceptions belong here.
# Key: (ecosystem, name, version); value: (SPDX expression, evidence).
# Package metadata says only "BSD"; the license text identifies the variant.
OVERRIDES: dict[tuple[str, str, str], tuple[str, str]] = {
    ("Python", "colorama", "0.4.6"): (
        "BSD-3-Clause",
        "https://raw.githubusercontent.com/tartley/colorama/0.4.6/LICENSE.txt",
    ),
    ("Python", "nvidia-ml-py", "13.595.45"): (
        "BSD-3-Clause",
        "pynvml.py header in the locked wheel nvidia_ml_py-13.595.45-py3-none-any.whl",
    ),
}
ASSETS = (
    ("Inter", "5.3.0", "OFL-1.1", "apps/web/preview/public/licenses/inter-OFL.txt"),
    ("Noto Sans SC", "5.3.0", "OFL-1.1", "apps/web/preview/public/licenses/noto-sans-sc-OFL.txt"),
    (
        "common-passwords",
        "2026.1 (190c6f7bd58c847ceadfe57d9853592737f059e8)",
        "MIT",
        "packages/security/src/holm_security/resources/common-passwords.LICENSE",
    ),
)


@dataclass(frozen=True)
class Component:
    ecosystem: str
    name: str
    version: str
    license: str
    source: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.ecosystem, self.name, self.version


def classify(expression: str) -> str:
    """Preserve AND/OR expressions and conservatively use their strictest term."""
    if not expression.strip():
        return "不兼容"
    tokens = re.findall(r"[A-Za-z0-9.+-]+|[()]", expression)
    if "".join(tokens) != re.sub(r"\s", "", expression) or not tokens:
        return "识别不出"
    expect_license = True
    depth = 0
    grades: list[str] = []
    for token in tokens:
        if token == "(" and expect_license:
            depth += 1
        elif token == ")" and not expect_license and depth:
            depth -= 1
        elif token in {"AND", "OR"} and not expect_license:
            expect_license = True
        elif expect_license and token not in {"(", ")", "AND", "OR"}:
            grades.append(
                "宽松"
                if token in PERMISSIVE
                else "需人工审"
                if token in REVIEW
                else "不兼容"
                if token in INCOMPATIBLE
                else "识别不出"
            )
            expect_license = False
        else:
            return "识别不出"
    if expect_license or depth:
        return "识别不出"
    for grade in ("不兼容", "识别不出", "需人工审"):
        if grade in grades:
            return grade
    return "宽松"


def python_metadata(root: Path, name: str, version: str) -> Message:
    try:
        dist = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        # uv exports all platform markers. Inspect a hash-locked wheel for packages
        # absent on this platform, without installing them or running package code.
        lock = tomllib.loads((root / "uv.lock").read_text())
        matches = [p for p in lock["package"] if p["name"] == name and p["version"] == version]
        if len(matches) != 1 or not matches[0].get("wheels"):
            raise ValueError(f"{name}: no unique locked wheel for metadata") from None
        wheel = min(matches[0]["wheels"], key=lambda w: w["size"])
        digest = wheel["hash"].removeprefix("sha256:")
        cache = Path(tempfile.gettempdir()) / "holm-license-wheels" / digest
        if cache.is_file():
            data = cache.read_bytes()
        else:
            with urllib.request.urlopen(wheel["url"], timeout=30) as response:
                data = response.read(wheel["size"] + 1)
        if len(data) != wheel["size"] or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"{name}: locked wheel hash/size mismatch") from None
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            paths = [p for p in archive.namelist() if p.endswith(".dist-info/METADATA")]
            if len(paths) != 1:
                raise ValueError(f"{name}: wheel has no unique METADATA") from None
            metadata = Parser().parsestr(archive.read(paths[0]).decode())
    else:
        metadata = Parser().parsestr(dist.read_text("METADATA") or "")
    if metadata.get("Version") != version:
        raise ValueError(f"{name}: metadata version differs from uv.lock ({version})")
    return metadata


def python_components(root: Path) -> list[Component]:
    result = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--all-packages",
            "--no-hashes",
            "--no-emit-workspace",
            "--format",
            "requirements-txt",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    components = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ;]+)(?:\s*;.*)?", line)
        if match is None:
            raise ValueError("Unsupported uv export requirement: " + line)
        name, version = match.groups()
        metadata = python_metadata(root, name, version)
        expression = ""
        source = "missing license metadata"
        for field in ("License-Expression", "License"):
            value = metadata.get(field, "").strip()
            value = ALIASES.get(value, value)
            if value and (field == "License-Expression" or classify(value) != "识别不出"):
                expression, source = value, field
                break
        if not expression:
            values = sorted(
                {CLASSIFIERS[c] for c in metadata.get_all("Classifier", []) if c in CLASSIFIERS}
            )
            if values:
                expression, source = " AND ".join(values), "Trove classifier"
            elif metadata.get("License", "").strip():
                expression, source = "UNKNOWN", "License (unrecognized)"
            elif any(c.startswith("License ::") for c in metadata.get_all("Classifier", [])):
                expression, source = "UNKNOWN", "Trove classifier (unrecognized)"
        components.append(Component("Python", name, version, expression, source))
    if not components:
        raise ValueError("Python runtime dependency set is empty")
    return components


def frontend_components(root: Path) -> list[Component]:
    lock = json.loads((root / "package-lock.json").read_text())
    if lock.get("lockfileVersion") != 3:
        raise ValueError("Unsupported package-lock.json version")
    components = []
    for path, package in lock["packages"].items():
        if "node_modules/" not in path or package.get("link") or package.get("dev"):
            continue
        name = path.rsplit("node_modules/", 1)[1]
        version = package["version"]
        expression = package.get("license", "")
        source = "package-lock.json license"
        if not expression:
            installed = json.loads((root / path / "package.json").read_text())
            if installed["version"] != version:
                raise ValueError(f"{name}: installed version differs from package-lock.json")
            expression = installed.get("license", "")
            source = "package.json license"
        if not isinstance(expression, str):
            raise ValueError(f"{name}: unsupported license metadata")
        components.append(Component("Frontend", name, version, expression, source))
    if not components:
        raise ValueError("Frontend runtime dependency set is empty")
    return components


def asset_components(root: Path) -> list[Component]:
    result = []
    for name, version, expression, path in ASSETS:
        if not (root / path).is_file() or not (root / path).read_text().strip():
            raise ValueError(f"{name}: missing or empty asset license: {path}")
        result.append(Component("Asset", name, version, expression, f"bundled license: {path}"))
    return result


def render(
    components: list[Component],
    overrides: dict[tuple[str, str, str], tuple[str, str]],
) -> tuple[str, list[str]]:
    errors = []
    if not components:
        errors.append("Runtime dependency set is empty")
    keys = {component.key for component in components}
    for key in sorted(overrides):
        if key not in keys:
            errors.append(f"Stale override: {' / '.join(key)}")
    rows: list[tuple[Component, str]] = []
    for component in sorted(
        set(components), key=lambda c: (c.name.lower(), c.version, c.ecosystem)
    ):
        if component.key in overrides:
            expression, evidence = overrides[component.key]
            if not evidence.strip():
                errors.append(f"{component.name}: override has no evidence")
            component = Component(
                component.ecosystem,
                component.name,
                component.version,
                expression,
                f"override: {evidence}",
            )
        grade = classify(component.license)
        if grade in {"不兼容", "识别不出"}:
            errors.append(f"{component.name} {component.version}: {grade}")
        rows.append((component, grade))
    lines = [
        "# Third-party licenses",
        "",
        "运行时依赖来自 uv.lock（uv export --no-dev --all-packages）与 package-lock.json；"
        "开发依赖不在清单内。AND／OR 表达式保留原文，按最严的一项分级。",
        "",
    ]
    for grade in ("需人工审", "不兼容", "识别不出"):
        lines.extend([f"## {grade}", ""])
        selected = [c for c, g in rows if g == grade]
        lines.extend(
            [f"- {c.name} {c.version}: {c.license or '无许可证'} ({c.ecosystem})" for c in selected]
            or ["无。"]
        )
        lines.append("")
    lines.extend(
        [
            "## 清单",
            "",
            "| 类别 | 名称 | 版本 | SPDX | 分级 | 来源字段 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for component, grade in rows:
        cells = (
            component.ecosystem,
            component.name,
            component.version,
            component.license or "UNKNOWN",
            grade,
            component.source,
        )
        lines.append(
            "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |"
        )
    lines.extend(
        ["", "## 范围说明", "", "独立 venv 中的 vLLM／SGLang Runtime 环境不在本清单内。", ""]
    )
    return "\n".join(lines), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        components = python_components(ROOT) + frontend_components(ROOT) + asset_components(ROOT)
        output, errors = render(components, OVERRIDES)
        if not args.check:
            print(output, end="")
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        if args.check and (ROOT / "THIRD_PARTY_LICENSES.md").read_text() != output:
            print(
                "Stale THIRD_PARTY_LICENSES.md; regenerate: "
                "uv run --frozen python scripts/third_party_licenses.py "
                "> THIRD_PARTY_LICENSES.md",
                file=sys.stderr,
            )
            return 1
        return 0
    except (
        ValueError,
        OSError,
        KeyError,
        importlib.metadata.PackageNotFoundError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"License inventory failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
