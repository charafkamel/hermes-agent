"""Tests for the shipped Compresr savings store + /compresr command."""

import sys
from pathlib import Path

import pytest

from plugins.tool_output_compresr import stats
from plugins.tool_output_compresr import ToolOutputCompressor


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the stats file at a temp location for each test."""
    p = tmp_path / "compresr" / "stats.json"
    monkeypatch.setattr(stats, "_path", lambda: p)
    return p


def test_blank_when_no_file(store):
    d = stats.load()
    assert d["compaction"]["calls"] == 0
    assert d["tool_output"]["calls"] == 0
    assert d["since"] is None


def test_records_accumulate(store):
    stats.record_tool_output(1000, 400)
    stats.record_tool_output(2000, 600)
    stats.record_compaction(5000, 1500)
    stats.record_recovery()
    stats.record_error("tool_output")

    d = stats.load()
    assert d["tool_output"]["calls"] == 2
    assert d["tool_output"]["tokens_in"] == 3000
    assert d["tool_output"]["tokens_saved"] == 1000
    assert d["tool_output"]["recoveries"] == 1
    assert d["tool_output"]["errors"] == 1
    assert d["compaction"]["calls"] == 1
    assert d["compaction"]["tokens_saved"] == 1500
    assert d["since"] is not None and d["updated"] is not None


def test_render_empty(store):
    out = stats.render()
    assert "hasn't compressed anything yet" in out


def test_render_populated(store):
    stats.record_compaction(40000, 12000)
    stats.record_tool_output(100000, 60000)
    stats.record_recovery()
    out = stats.render()
    assert "Compresr — savings since" in out
    assert "Compaction:" in out and "12,000 tokens saved" in out
    assert "Tool output:" in out and "60,000 tokens saved" in out
    assert "Recoveries:" in out
    # total saved 72,000 over 140,000 in -> ~51% reduction
    assert "72,000 tokens" in out and "51% reduction" in out


def test_corrupt_file_is_safe(store):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{ not json", encoding="utf-8")
    assert stats.load()["tool_output"]["calls"] == 0   # coerced to blank
    stats.record_tool_output(10, 5)                      # still records over it
    assert stats.load()["tool_output"]["calls"] == 1


def test_register_slash_command():
    captured = {}

    class Ctx:
        def register_command(self, name, handler, description=""):
            captured["name"] = name
            captured["handler"] = handler

    stats.register_slash_command(Ctx())
    assert captured["name"] == "compresr"
    assert callable(captured["handler"])


def test_recovery_read_detection():
    assert ToolOutputCompressor._is_recovery_read(
        {"path": "/home/u/.compresr/cache/ab12", "offset": 5, "limit": 3}
    )
    assert not ToolOutputCompressor._is_recovery_read({"path": "/home/u/notes.md"})
    assert not ToolOutputCompressor._is_recovery_read(None)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
