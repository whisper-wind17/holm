"""Fail-loud literal-color source gate for M1-11A."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_SOURCE_ROOT = PROJECT_ROOT / "apps" / "web" / "src"
EXCLUDED_TOKEN_FILES = (
    Path("styles/tokens.css"),
    Path("styles/icons.css"),
)
SCANNED_SUFFIXES = frozenset(
    {".css", ".scss", ".sass", ".less", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html"}
)
KNOWN_UNCOVERED = (
    "oklch()/lab()/color() functional colors",
    "CSS named colors",
    ".svg resources (masks use currentColor and brand marks may carry color)",
)

HEX_COLOR = re.compile(
    r"(?<![\w-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9a-fA-F])"
)
COLOR_FUNCTION = re.compile(r"\b(?:rgb|rgba|hsl|hsla)\s*\(", re.IGNORECASE)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class LiteralColorViolation:
    path: Path
    line: int
    literal: str


@dataclass(frozen=True)
class LiteralColorReport:
    scanned_files: tuple[Path, ...]
    excluded_files: tuple[Path, ...]


def _strip_code_comments(source: str) -> str:
    """Remove //, /* */, and HTML comments while retaining strings and line numbers."""

    source = HTML_COMMENT.sub(lambda match: "\n" * match.group(0).count("\n"), source)
    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(source):
        current = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if quote is not None:
            output.append(current)
            if current == "\\" and following:
                output.append(following)
                index += 2
                continue
            if current == quote:
                quote = None
            index += 1
            continue
        if current in {'"', "'", "`"}:
            quote = current
            output.append(current)
            index += 1
            continue
        if current == "/" and following == "/":
            while index < len(source) and source[index] != "\n":
                output.append(" ")
                index += 1
            continue
        if current == "/" and following == "*":
            output.extend((" ", " "))
            index += 2
            while index < len(source):
                if source[index : index + 2] == "*/":
                    output.extend((" ", " "))
                    index += 2
                    break
                output.append("\n" if source[index] == "\n" else " ")
                index += 1
            continue
        output.append(current)
        index += 1
    return "".join(output)


def _discover_sources(source_root: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    assert source_root.is_dir(), f"web source root is missing: {source_root}"
    excluded = tuple(source_root / relative for relative in EXCLUDED_TOKEN_FILES)
    missing_exclusions = [path for path in excluded if not path.is_file()]
    assert not missing_exclusions, (
        "literal-color exclusions must exist rather than silently widening/narrowing the gate: "
        f"{missing_exclusions}"
    )
    excluded_set = set(excluded)
    scanned = tuple(
        path
        for path in sorted(source_root.rglob("*"))
        if path.is_file() and path.suffix.lower() in SCANNED_SUFFIXES and path not in excluded_set
    )
    assert scanned, "literal-color gate discovered zero business source files"
    return scanned, excluded


def _violations(source_root: Path, scanned: tuple[Path, ...]) -> tuple[LiteralColorViolation, ...]:
    violations: list[LiteralColorViolation] = []
    for path in scanned:
        content = _strip_code_comments(path.read_text(encoding="utf-8"))
        for pattern in (HEX_COLOR, COLOR_FUNCTION):
            for match in pattern.finditer(content):
                violations.append(
                    LiteralColorViolation(
                        path=path.relative_to(source_root),
                        line=content.count("\n", 0, match.start()) + 1,
                        literal=match.group(0),
                    )
                )
    return tuple(sorted(violations, key=lambda item: (str(item.path), item.line, item.literal)))


def _run_literal_color_gate(source_root: Path, *, emit_report: bool = False) -> LiteralColorReport:
    scanned, excluded = _discover_sources(source_root)
    report = LiteralColorReport(
        scanned_files=tuple(path.relative_to(source_root) for path in scanned),
        excluded_files=tuple(path.relative_to(source_root) for path in excluded),
    )
    if emit_report:
        print(f"M1-11A literal-color gate: scanned {len(scanned)} file(s)")
        print(
            f"M1-11A literal-color gate: excluded {[str(path) for path in report.excluded_files]}"
        )
        print(f"M1-11A literal-color gate: KNOWN NOT COVERED — {'; '.join(KNOWN_UNCOVERED)}")
    violations = _violations(source_root, scanned)
    assert not violations, f"business source contains literal colors: {violations}"
    return report


def _write_minimal_token_layer(source_root: Path) -> None:
    for relative in EXCLUDED_TOKEN_FILES:
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(":root {}\n", encoding="utf-8")


def test_M1_11A_business_source_has_no_literal_colors(capsys: pytest.CaptureFixture[str]) -> None:
    with capsys.disabled():
        report = _run_literal_color_gate(WEB_SOURCE_ROOT, emit_report=True)
    assert report.excluded_files == EXCLUDED_TOKEN_FILES


@pytest.mark.parametrize(
    "literal",
    (
        "#abc",
        "#abcd",
        "#aabbcc",
        "#aabbccdd",
        "rgb(1, 2, 3)",
        "rgba(1, 2, 3, .4)",
        "hsl(10, 20%, 30%)",
        "hsla(10, 20%, 30%, .4)",
    ),
)
def test_M1_11A_literal_color_spellings_fail(tmp_path: Path, literal: str) -> None:
    source_root = tmp_path / "src"
    _write_minimal_token_layer(source_root)
    (source_root / "injected.ts").write_text(
        f'export const injected = "{literal}";\n', encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="literal colors"):
        _run_literal_color_gate(source_root)


def test_M1_11A_comments_class_names_and_color_words_do_not_false_positive(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_minimal_token_layer(source_root)
    (source_root / "safe.css").write_text(
        """/* #0B5CFF rgb(1, 2, 3) hsl(1, 2%, 3%) */
// #abc rgba(1, 2, 3, .4)
.red { color: var(--pw-text-primary); }
.颜色词::after { content: "红色、蓝色、绿色"; }
""",
        encoding="utf-8",
    )
    _run_literal_color_gate(source_root)


def test_M1_11A_zero_discovered_business_files_fails(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_minimal_token_layer(source_root)
    with pytest.raises(AssertionError, match="zero business source files"):
        _run_literal_color_gate(source_root)


def test_M1_11A_missing_excluded_token_file_fails(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    (source_root / "styles").mkdir(parents=True)
    (source_root / "styles" / "tokens.css").write_text(":root {}\n", encoding="utf-8")
    (source_root / "workspace.ts").write_text("export {};\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="exclusions must exist"):
        _run_literal_color_gate(source_root)
