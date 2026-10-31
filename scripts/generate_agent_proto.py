"""Generate or byte-check the versioned Host Agent protobuf modules."""

from __future__ import annotations

import argparse
import importlib.metadata
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
SOURCE_ROOT: Final[Path] = ROOT / "packages" / "contracts" / "src"
PROTO: Final[Path] = SOURCE_ROOT / "holm_contracts" / "agent" / "v1" / "agent.proto"
COMMITTED_ROOT: Final[Path] = PROTO.parent
GENERATED_FILES: Final[tuple[str, ...]] = ("agent_pb2.py", "agent_pb2_grpc.py")


def _pinned_tool_version() -> str:
    root_manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements: object = root_manifest["dependency-groups"]["dev"]
    if not isinstance(requirements, list):
        raise RuntimeError("root dev dependency group is not a list")
    matches = [
        requirement.removeprefix("grpcio-tools==")
        for requirement in requirements
        if isinstance(requirement, str) and requirement.startswith("grpcio-tools==")
    ]
    if len(matches) != 1 or not matches[0] or any(token in matches[0] for token in "*<>,~!"):
        raise RuntimeError("grpcio-tools must have exactly one exact == pin")
    return matches[0]


def _verify_tool_version() -> None:
    expected = _pinned_tool_version()
    actual = importlib.metadata.version("grpcio-tools")
    if actual != expected:
        raise RuntimeError(f"grpcio-tools version mismatch: expected {expected}, found {actual}")


def _generate(output_root: Path) -> Path:
    _verify_tool_version()
    command = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{SOURCE_ROOT}",
        f"--python_out={output_root}",
        f"--grpc_python_out={output_root}",
        str(PROTO.relative_to(SOURCE_ROOT)),
    ]
    result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Agent protobuf generation failed ({result.returncode}): {result.stderr.strip()}"
        )
    return output_root / PROTO.parent.relative_to(SOURCE_ROOT)


def _mismatches(generated_root: Path, committed_root: Path) -> tuple[str, ...]:
    mismatches: list[str] = []
    for filename in GENERATED_FILES:
        generated = generated_root / filename
        committed = committed_root / filename
        if not committed.is_file() or committed.read_bytes() != generated.read_bytes():
            mismatches.append(filename)
    return tuple(mismatches)


def check_generated(committed_root: Path = COMMITTED_ROOT) -> None:
    with tempfile.TemporaryDirectory(prefix="holm-agent-proto-") as temporary:
        generated_root = _generate(Path(temporary))
        mismatches = _mismatches(generated_root, committed_root)
    if mismatches:
        files = ", ".join(mismatches)
        raise RuntimeError(f"generated Agent protobuf drift: {files}; run with --write")


def write_generated() -> None:
    with tempfile.TemporaryDirectory(prefix="holm-agent-proto-") as temporary:
        generated_root = _generate(Path(temporary))
        for filename in GENERATED_FILES:
            shutil.copyfile(generated_root / filename, COMMITTED_ROOT / filename)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true", help="replace committed generated files")
    action.add_argument(
        "--check-against",
        type=Path,
        metavar="DIR",
        help="compare with generated files in DIR instead of the committed directory",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.write:
        write_generated()
    else:
        check_generated(args.check_against or COMMITTED_ROOT)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
