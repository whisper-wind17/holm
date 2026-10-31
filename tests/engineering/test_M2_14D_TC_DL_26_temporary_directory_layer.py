"""M2-14D: the staging temporary-directory layer is deleted, on evidence.

``_prepare_staging_temporary_directory`` pointed TMPDIR/TEMP/TMP and
``tempfile.tempdir`` at the staging root.  Nothing pinned it (M2-14C's suite stays
green without it) so an unpinned global override must be removed.

It is deleted because the locked SDK's model download path never lets
``tempfile`` choose a directory: every temporary file it creates passes ``dir=``.
That was checked by opening the source on 2026-09-14 -- the earlier claim "the
SDK does not use tempfile" was wrong -- and it is a property of one SDK version,
so it is asserted here against the installed source.  A version bump that adds a
dir-less temporary file on the download path fails this test instead of silently
depending on a layer that no longer exists (M2-14C's write fence would refuse the
resulting write outside staging, loudly, but only at run time).
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from importlib.util import find_spec
from pathlib import Path

import holm_download_worker.download_subprocess as download_subprocess

_CREATORS = frozenset(
    {
        "NamedTemporaryFile",
        "TemporaryFile",
        "SpooledTemporaryFile",
        "TemporaryDirectory",
        "mkstemp",
        "mkdtemp",
        "mktemp",
    }
)
# Reading the default directory is as much a dependency on it as creating in it.
_DEFAULT_DIRECTORY_READERS = frozenset({"gettempdir", "gettempdirb", "tempdir"})

# Exact set, compared with ==: an exemption that stops matching fails too, so the
# list can only change deliberately.
EXEMPT = frozenset(
    {
        # repo_type "skill"/"skills" only (``_download.py:699``); models never reach it.
        ("modelscope_hub/_download.py", "_download_archive"),
        # Upload, not download.
        ("modelscope/hub/push_to_hub.py", "_push_files_to_hub"),
    }
)


def _sdk_sources() -> dict[str, str]:
    roots: list[tuple[str, Path]] = []
    for package, subdirectory in (("modelscope_hub", ""), ("modelscope", "hub")):
        spec = find_spec(package)
        assert spec is not None and spec.origin, f"{package} is not installed"
        roots.append((package, Path(spec.origin).parent / subdirectory))
    sources: dict[str, str] = {}
    for package, root in roots:
        base = root.parents[0] if package == "modelscope_hub" else root.parents[1]
        for path in sorted(root.rglob("*.py")):
            sources[path.relative_to(base).as_posix()] = path.read_text(encoding="utf-8")
    return sources


def _has_dir(call: ast.Call) -> bool:
    return any(
        keyword.arg == "dir"
        and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
        for keyword in call.keywords
    )


def _enclosing(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> str:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return "<module>"


def dirless_tempfile_uses(sources: Mapping[str, str]) -> set[tuple[str, str]]:
    """(file, enclosing function) of every tempfile use that does not name a directory."""

    found: set[tuple[str, str]] = set()
    for relative, source in sources.items():
        tree = ast.parse(source, filename=relative)
        aliases: dict[str, str] = {}
        modules = {"tempfile"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "tempfile":
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "tempfile":
                        modules.add(alias.asname or alias.name)
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in modules
            ):
                name = node.attr
            elif isinstance(node, ast.Name) and node.id in aliases:
                name = aliases[node.id]
            else:
                continue
            if name in _DEFAULT_DIRECTORY_READERS:
                found.add((relative, _enclosing(node, parents)))
                continue
            if name not in _CREATORS:
                continue
            parent = parents.get(node)
            # tempfile.X(...), or functools.partial(tempfile.X, ..., dir=...)
            if isinstance(parent, ast.Call) and (parent.func is node or node in parent.args):
                if not _has_dir(parent):
                    found.add((relative, _enclosing(node, parents)))
            elif not (isinstance(parent, ast.ImportFrom)):
                found.add((relative, _enclosing(node, parents)))  # unrecognised use: fail closed
    return found


def _mutate(sources: dict[str, str], relative: str, anchor: str, replacement: str) -> None:
    assert sources[relative].count(anchor) == 1, f"anchor must hit exactly once: {anchor!r}"
    sources[relative] = sources[relative].replace(anchor, replacement)


def test_M2_14D_TC_DL_26_sdk_download_path_always_names_the_temporary_directory() -> None:
    sources = _sdk_sources()
    # Seeing the known dir-passing sites is what makes "nothing found" mean something.
    assert (
        "tempfile.NamedTemporaryFile, mode='wb', dir=local_dir"
        in sources["modelscope/hub/file_download.py"]
    )
    assert "dir=self.cache_root_location" in sources["modelscope/hub/utils/caching.py"]

    assert dirless_tempfile_uses(sources) == EXEMPT


def test_M2_14D_TC_DL_26_positive_control_a_dirless_call_in_the_real_sdk_is_found() -> None:
    sources = _sdk_sources()
    _mutate(
        sources,
        "modelscope/hub/utils/caching.py",
        "suffix='.tmp', dir=self.cache_root_location)",
        "suffix='.tmp')",
    )

    assert ("modelscope/hub/utils/caching.py", "_save_cached_files_unlocked") in (
        dirless_tempfile_uses(sources) - EXEMPT
    )


def test_M2_14D_TC_DL_26_positive_control_a_dirless_partial_in_the_real_sdk_is_found() -> None:
    sources = _sdk_sources()
    _mutate(
        sources,
        "modelscope/hub/file_download.py",
        "mode='wb', dir=local_dir, delete=False)",
        "mode='wb', delete=False)",
    )

    assert ("modelscope/hub/file_download.py", "http_get_file") in (
        dirless_tempfile_uses(sources) - EXEMPT
    )


def test_M2_14D_TC_DL_26_positive_control_aliases_and_default_readers_are_found() -> None:
    sources = {
        "alias.py": "from tempfile import mkstemp as make\n\ndef f():\n    make()\n",
        "reader.py": "import tempfile as t\n\ndef g():\n    return t.gettempdir()\n",
    }

    assert dirless_tempfile_uses(sources) == {("alias.py", "f"), ("reader.py", "g")}


def test_M2_14D_TC_DL_26_download_child_no_longer_redirects_the_temporary_directory() -> None:
    source = Path(download_subprocess.__file__).read_text(encoding="utf-8")

    assert "_prepare_staging_temporary_directory" not in source
    assert "tempfile.tempdir" not in source
    assert '"TMPDIR"' not in source
