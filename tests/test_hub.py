"""Shared AI hub mode: the wait in the hub's line shows up as a job stage and the right headers are sent."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import process  # noqa: E402


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.text = data, status, ""

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(process, "WHISPER_URL", "http://ai-hub:11434")
    process._job.set([tmp_path, "scanning"])
    return tmp_path


def test_long_wait_is_shown_then_the_stage_returns(hub, monkeypatch):
    monkeypatch.setattr(process.requests, "get", lambda *a, **k: Resp({"position": 2, "wait_seconds": 41.0}))
    process._announce_wait("generate", "gemma4:e4b")
    assert (hub / ".stage").read_text() == "waiting"
    assert "2 ahead" in (hub / ".stage_note").read_text()
    process._after_wait()
    assert (hub / ".stage").read_text() == "scanning"
    assert not (hub / ".stage_note").exists()


def test_short_wait_is_not_shown(hub, monkeypatch):
    monkeypatch.setattr(process.requests, "get", lambda *a, **k: Resp({"position": 0, "wait_seconds": 1.0}))
    process._announce_wait("generate", "gemma4:e4b")
    assert not (hub / ".stage").exists()


def test_ollama_calls_identify_the_app_and_allow_for_the_queue(hub, monkeypatch):
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, headers=headers, timeout=timeout)
        return Resp({"response": "{}"})

    monkeypatch.setattr(process.requests, "get", lambda *a, **k: Resp({"position": 0, "wait_seconds": 0}))
    monkeypatch.setattr(process.requests, "post", post)
    process.ollama("hi", timeout=120)
    assert seen["headers"]["X-Client"] == "foodbox"
    assert seen["timeout"] >= 120 + 1800


def test_without_the_hub_nothing_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(process, "WHISPER_URL", "")
    process._job.set([tmp_path, "scanning"])
    process._announce_wait("generate", "gemma4:e4b")  # no request, no stage file
    assert not (tmp_path / ".stage").exists()
    assert process._hub_headers() == {}
