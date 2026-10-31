"""UI-18: inspect real preview output, never accept a missing build as a clean scan."""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "apps/web"
TEXT_SUFFIXES = {".css", ".html", ".js", ".ts", ".tsx"}
# Remote CSS imports may conceal font-face rules; reject them as well as font URLs.
EXTERNAL_FONT = re.compile(
    r"fonts\.(?:googleapis|gstatic)\.com"
    r"|(?:https?:)?//[^\s\"'<>)]*\.(?:woff2?|ttf|otf|eot)(?:[?#][^\s\"'<>)]*)?"
    r"|@import\s+(?:url\(\s*)?[\"']?(?:https?:)?//"
    r"|url\(\s*[\"']?(?:https?:)?//"
    r"|<link\b[^>]*\bhref\s*=\s*[\"'](?:https?:)?//",
    re.IGNORECASE,
)


def _assert_no_external_fonts(paths: list[Path]) -> None:
    assert paths, "UI-18: no files to inspect"
    for path in paths:
        assert not EXTERNAL_FONT.search(path.read_text()), f"external font URL: {path}"


@pytest.fixture(scope="module")
def preview_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("UI-18-preview")
    result = subprocess.run(
        [
            "npm",
            "run",
            "build",
            "--workspace",
            "@holm/web",
            "--",
            "--config",
            "preview/vite.config.ts",
            "--outDir",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (output / "index.html").is_file(), "UI-18: preview HTML missing"
    return output


def test_UI_18_source_and_preview_have_only_local_fonts(preview_build: Path) -> None:
    for directory in (WEB / "src", WEB / "preview", preview_build):
        paths = [
            path
            for path in directory.rglob("*")
            if path.suffix in TEXT_SUFFIXES
            and "dist" not in path.relative_to(directory).parts
            and not path.name.endswith((".test.ts", ".test.tsx"))
        ]
        _assert_no_external_fonts(paths)
    css_files = list(preview_build.rglob("*.css"))
    assert css_files, "UI-18: preview CSS missing"
    css = "\n".join(path.read_text() for path in css_files)
    faces = re.findall(r"@font-face\s*\{([^}]+)\}", css)
    for family, prefix in (("Inter", "inter-"), ("Noto Sans SC", "noto-sans-sc-")):
        family_faces = [
            face for face in faces if re.search(rf"font-family:\s*[\"']?{family}[\"']?\s*;", face)
        ]
        assert family_faces, f"UI-18: missing family {family}"
        assert {
            int(match.group(1))
            for face in family_faces
            if (match := re.search(r"font-weight:\s*(\d+)\s*;", face))
        } == {400, 500, 600, 700}
        for face in family_faces:
            assert "unicode-range:" in face and "font-display:swap" in face
            urls = re.findall(r"url\([\"']?([^\"')]+)[\"']?\)", face)
            assert urls, f"UI-18: no font URL for {family}"
            for url in urls:
                # Vite embeds small subsets in the same-origin stylesheet.
                if embedded := re.fullmatch(r"data:font/(woff2?);base64,(.+)", url):
                    magic = b"wOF2" if embedded[1] == "woff2" else b"wOFF"
                    assert base64.b64decode(embedded[2], validate=True).startswith(magic)
                    continue
                asset = preview_build / url.lstrip("/")
                assert asset.is_file(), f"UI-18: missing font {url}"
        fonts = list(preview_build.rglob(f"{prefix}*.woff2"))
        assert fonts, f"UI-18: no woff2 for {family}"
        assert all(font.read_bytes().startswith(b"wOF2") for font in fonts)
    fonts = [path for path in preview_build.rglob("*") if path.suffix in {".woff", ".woff2"}]
    print(f"UI-18 preview fonts: {len(fonts)} files, {sum(p.stat().st_size for p in fonts)} bytes")
    for extension in (".woff2", ".woff"):
        selected = [path for path in fonts if path.suffix == extension]
        size = sum(path.stat().st_size for path in selected)
        print(f"UI-18 {extension}: {len(selected)} files, {size} bytes")
    for name in ("inter", "noto-sans-sc"):
        assert (preview_build / "licenses" / f"{name}-OFL.txt").read_bytes() == (
            ROOT / "node_modules/@fontsource" / name / "LICENSE"
        ).read_bytes()


@pytest.mark.parametrize(
    "content",
    [
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">',
        '@font-face { src: url("//fonts.gstatic.com/inter.woff2"); }',
        '@font-face { src: url("https://cdn.example.test/type.woff2"); }',
        '@import "https://cdn.example.test/type.css";',
    ],
)
def test_UI_18_external_font_positive_control(tmp_path: Path, content: str) -> None:
    # FX-UI-18: synthetic prohibited references, never loaded by the browser.
    injected = tmp_path / "injected.css"
    injected.write_text(content)
    with pytest.raises(AssertionError, match="external font URL"):
        _assert_no_external_fonts([injected])


def test_UI_18_empty_scan_fails() -> None:
    with pytest.raises(AssertionError, match="no files to inspect"):
        _assert_no_external_fonts([])
