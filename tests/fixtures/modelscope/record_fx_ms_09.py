"""Record real modelscope.cn responses for FX-MS-09.

Runs on the developer workstation only: the development container cannot be
assumed to reach modelscope.cn, so the recording happens here and the replay
happens in the container (same topology M2-04A established for FX-MS-08).

No credential is used.  Every request is anonymous or carries a deliberately
invalid, made-up token, so nothing secret can enter the recordings.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

OUT = Path("tests/fixtures/modelscope/recorded")
BASE = "https://modelscope.cn/api/v1"
PUBLIC = f"{BASE}/models/Qwen/Qwen2.5-7B-Instruct"
SMALL = f"{BASE}/models/iic/nlp_structbert_word-segmentation_chinese-base"
MISSING = f"{BASE}/models/Qwen/Qwen2.5-7B-Instruct-no-such-repo-abc123"

# A made-up token in ModelScope's shape.  It is not, and never was, a real
# credential; it exists to record what an invalid one looks like.
BAD_TOKEN = "ms-00000000-0000-0000-0000-000000000000"

# One file's last-commit sha observed in the master listing, truncated the way
# a careless caller would truncate it.
PARTIAL_SHA = "3fe289ea2828843e2d"
UNKNOWN_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

Case = tuple[str, str, list[str]]

CASES: list[Case] = [
    ("public-repo-metadata", PUBLIC, []),
    ("public-repo-revisions", f"{PUBLIC}/revisions", []),
    ("public-repo-files-master", f"{PUBLIC}/repo/files?Revision=master&Recursive=True", []),
    (
        "public-repo-files-by-partial-sha",
        f"{PUBLIC}/repo/files?Revision={PARTIAL_SHA}&Recursive=True",
        [],
    ),
    (
        "public-repo-files-unknown-sha",
        f"{PUBLIC}/repo/files?Revision={UNKNOWN_SHA}&Recursive=True",
        [],
    ),
    (
        "public-repo-files-unknown-branch",
        f"{PUBLIC}/repo/files?Revision=nonexistent-branch-xyz&Recursive=True",
        [],
    ),
    ("public-repo-files-empty-revision", f"{PUBLIC}/repo/files?Revision=&Recursive=True", []),
    ("public-repo-config", f"{PUBLIC}/repo?Revision=master&FilePath=config.json", []),
    ("small-repo-files-master", f"{SMALL}/repo/files?Revision=master&Recursive=True", []),
    ("missing-repo-metadata-anonymous", MISSING, []),
    ("missing-repo-files-anonymous", f"{MISSING}/repo/files?Revision=master&Recursive=True", []),
    ("anonymous-users-info", f"{BASE}/users/info", []),
    (
        "invalid-credential-public-repo",
        PUBLIC,
        ["-H", f"Authorization: Bearer {BAD_TOKEN}"],
    ),
    (
        "invalid-credential-missing-repo",
        MISSING,
        ["-H", f"Authorization: Bearer {BAD_TOKEN}"],
    ),
    (
        "invalid-credential-users-info",
        f"{BASE}/users/info",
        ["-H", f"Authorization: Bearer {BAD_TOKEN}"],
    ),
    (
        "public-repo-lfs-redirect",
        f"{PUBLIC}/repo?Revision=master&FilePath=model-00001-of-00004.safetensors",
        ["-r", "0-0"],
    ),
]

# The workstation reaches the internet through a local HTTP proxy; CONNECT
# handshakes are not part of the source's answer.
_PROXY_LINE = re.compile(r"^HTTP/1\.[01] 200 Connection established\s*$")
# LFS redirects carry a signed, time-limited CDN query.  The signature is not a
# judgement input and it expires, so it is replaced rather than recorded.  It
# appears in the Location header AND in the HTML body of the 302, so the same
# substitution runs over both.
_SIGNED_QUERY = re.compile(r"(auth_key=)[^&\s\"<]+")
# Anonymous session and CSRF cookies.  Nothing in this project judges on them
# and they are per-session values, so only the cookie name is kept.
_COOKIE_VALUE = re.compile(r"^(Set-Cookie:\s*)([A-Za-z0-9_\-]+)=[^;]*", re.MULTILINE)


def redact(text: str) -> str:
    """Remove per-session and signed values that carry no judgement meaning."""
    return _SIGNED_QUERY.sub(r"\1<signed-value-redacted>", text)


def sanitize_headers(raw: str) -> str:
    lines = [line for line in raw.splitlines() if not _PROXY_LINE.match(line)]
    while lines and not lines[0].strip():
        lines.pop(0)
    text = "\n".join(lines).rstrip() + "\n"
    return _COOKIE_VALUE.sub(r"\1\2=<session-value-redacted>", redact(text))


def sanitize_body(path: Path) -> None:
    """Redact signed values inside textual bodies; binary bodies are left alone."""
    try:
        original = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    cleaned = redact(original)
    if cleaned != original:
        path.write_text(cleaned, encoding="utf-8")


def record(name: str, url: str, extra: list[str]) -> int:
    body = OUT / f"{name}.body"
    headers = OUT / f"{name}.headers"
    proc = subprocess.run(
        ["curl", "-sS", "--max-time", "40", "-D", "-", "-o", str(body), *extra, url],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(f"FAIL {name}: curl exit {proc.returncode}: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    headers.write_text(sanitize_headers(proc.stdout), encoding="utf-8")
    sanitize_body(body)
    status = proc.stdout.split("\n")[0].strip()
    for line in proc.stdout.splitlines():
        if line.startswith("HTTP/") and "Connection established" not in line:
            status = line.strip()
            break
    print(f"{name:38s} {status:20s} {body.stat().st_size:>9d}B")
    return 0


def main() -> int:
    # Relative output path: running from anywhere else silently creates a nested
    # copy instead of updating the recordings.  That happened once; fail loudly.
    if not Path("pyproject.toml").is_file() or not Path("tests/fixtures").is_dir():
        print("run this from the repository root", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    failures = sum(record(*case) for case in CASES)
    if failures:
        print(f"\n{failures} recording(s) failed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
