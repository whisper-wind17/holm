"""FX-M7-06B-PTY: real controlling terminal; only confirm(), never uninstall."""

from __future__ import annotations

import errno
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "deploy/installer"
CONFIRM = """
import fcntl
import sys
import termios

sys.path.insert(0, sys.argv[1])
from uninstall import Uninstaller

if sys.argv[2] == "tty":
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)

class UnusedRunner:
    def run(self, *args, **kwargs):
        raise AssertionError("confirm must not execute commands")

sys.exit(0 if Uninstaller(UnusedRunner()).confirm() else 1)
"""


@pytest.mark.parametrize(("answer", "expected"), [(b"purge\n", 0), (b"no\n", 1)])
def test_M7_06B_TC_DP_06_real_controlling_pty(answer: bytes, expected: int) -> None:
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(
            [sys.executable, "-c", CONFIRM, str(INSTALLER), "tty"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        ) as process:
            try:
                os.close(slave)
                slave = -1
                prompt = b"Type purge to delete the listed data and identities: "
                output = b""
                deadline = time.monotonic() + 10
                while prompt not in output:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                        break
                    try:
                        chunk = os.read(master, 4096)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output += chunk
                assert prompt in output, output
                os.write(master, answer)
                assert process.wait(timeout=10) == expected
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)


def test_M7_06B_TC_DP_06_no_controlling_terminal_refuses() -> None:
    result = subprocess.run(
        [sys.executable, "-c", CONFIRM, str(INSTALLER), "absent"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        start_new_session=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""
