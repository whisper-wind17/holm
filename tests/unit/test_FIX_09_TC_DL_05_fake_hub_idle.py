"""FIX-09 / TC-DL-05: a file GET must finish before Hub observations can be cleared."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen

import fixtures.hugging_face.fake_hub as fake_hub_module
import pytest
from fixtures.hugging_face import (
    TINY_REPOSITORY_COMMIT,
    TINY_REPOSITORY_ID,
    fake_hugging_face_hub,
)


def test_FIX_09_TC_DL_05_wait_until_idle_blocks_on_file_get_and_fails_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_started = threading.Event()
    release_get = threading.Event()
    waiter_started = threading.Event()
    waiter_finished = threading.Event()
    original_send = fake_hub_module._FakeHubHandler._send

    def held_send(
        handler: fake_hub_module._FakeHubHandler,
        method: str,
        reply: fake_hub_module._Reply,
    ) -> None:
        if not reply.track_transfer:
            original_send(handler, method, reply)
            return
        with handler.state.idle_condition:
            handler.state.active_file_gets += 1
        try:
            get_started.set()
            if not release_get.wait(timeout=5):
                raise AssertionError("file GET was not released")
            original_send(handler, method, reply)
        finally:
            with handler.state.idle_condition:
                handler.state.active_file_gets -= 1
                if handler.state.active_file_gets == 0:
                    handler.state.idle_condition.notify_all()

    monkeypatch.setattr(fake_hub_module._FakeHubHandler, "_send", held_send)
    with fake_hugging_face_hub(serve_synthetic_file_bodies=True) as hub:
        url = (
            f"{hub.endpoint}/{TINY_REPOSITORY_ID}/resolve/"
            f"{TINY_REPOSITORY_COMMIT}/pytorch_model.bin"
        )

        def file_get() -> bytes:
            with urlopen(url, timeout=5) as response:
                return response.read()

        def wait_for_idle() -> None:
            waiter_started.set()
            try:
                hub.wait_until_idle(5)
            finally:
                waiter_finished.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            get_result = pool.submit(file_get)
            try:
                assert get_started.wait(timeout=5)
                assert hub.state.active_file_gets == 1
                waiter_result = pool.submit(wait_for_idle)
                assert waiter_started.wait(timeout=5)
                assert not waiter_finished.wait(timeout=0.05)
                with pytest.raises(TimeoutError, match=r"active_file_gets=1"):
                    hub.wait_until_idle(0)
            finally:
                release_get.set()
            assert get_result.result(timeout=5)
            waiter_result.result(timeout=5)
            assert waiter_finished.is_set()
            hub.clear()
