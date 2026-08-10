"""Compresr compression counters must surface in usage accounting.

The Compresr plugin registers a ``transform_tool_result`` hook and optionally
a context engine, both exposing ``get_status()``. Before this surfacing, the
plugin could silently no-op for days and the only signal was recovery-cache
file deltas — usage.json had no compression fields at all. Covered here:

* the helper finds the hook's bound status via the plugin manager,
* ``_write_usage_file`` gains a top-level ``compresr`` key,
* ``/usage`` rendering produces the expected lines,
* with no plugin loaded, usage.json and rendering are unchanged.
"""

import json

import pytest

import hermes_cli.plugins as plugins_mod
from hermes_cli.compresr_usage import (
    collect_compresr_usage,
    render_compresr_usage_lines,
)
from hermes_cli.oneshot import _write_usage_file


class FakeCompressor:
    def get_status(self):
        return {
            "plugin": "compresr-sdk",
            "active": True,
            "config_error": None,
            "model": "latte_v2",
            "min_tokens": 500,
            "calls": 7,
            "errors": 1,
            "tokens_in": 32000,
            "tokens_saved": 18900,
            "recoveries": 2,
        }

    def on_transform_tool_result(self, **kwargs):
        return None


class FakeEngine:
    def get_status(self):
        return {
            "engine": "compresr",
            "compresr_model": "latte_v2",
            "compresr_calls": 3,
            "compresr_errors": 0,
            "compresr_tokens_in": 12000,
            "compresr_tokens_saved": 4100,
            "compresr_last_duration_ms": 210,
        }


@pytest.fixture
def fresh_manager(monkeypatch):
    monkeypatch.setattr(plugins_mod, "_plugin_manager", None)
    return plugins_mod.get_plugin_manager()


@pytest.fixture
def compresr_manager(fresh_manager):
    compressor = FakeCompressor()
    fresh_manager._hooks.setdefault("transform_tool_result", []).append(
        compressor.on_transform_tool_result
    )
    fresh_manager._context_engine = FakeEngine()
    return fresh_manager


def test_helper_finds_hook_and_engine_status(compresr_manager):
    report = collect_compresr_usage()
    assert report is not None
    assert report["tool_output"]["calls"] == 7
    assert report["tool_output"]["tokens_saved"] == 18900
    assert report["tool_output"]["recoveries"] == 2
    assert report["tool_output"]["model"] == "latte_v2"
    assert "config_error" not in report["tool_output"]
    assert report["context_engine"]["calls"] == 3
    assert report["context_engine"]["tokens_saved"] == 4100


def test_usage_file_gains_compresr_key(compresr_manager, tmp_path):
    path = tmp_path / "usage.json"
    _write_usage_file(str(path), {"input_tokens": 10, "model": "m"})
    data = json.loads(path.read_text())
    assert data["compresr"]["tool_output"]["calls"] == 7
    assert data["compresr"]["tool_output"]["tokens_saved"] == 18900
    assert data["compresr"]["context_engine"]["calls"] == 3


def test_usage_file_prefers_result_snapshot(compresr_manager, tmp_path):
    path = tmp_path / "usage.json"
    _write_usage_file(
        str(path),
        {"compresr": {"tool_output": {"calls": 99, "tokens_saved": 1}}},
    )
    data = json.loads(path.read_text())
    assert data["compresr"]["tool_output"]["calls"] == 99


def test_rendering_produces_expected_lines(compresr_manager):
    lines = render_compresr_usage_lines(collect_compresr_usage())
    assert lines == [
        "Compresr (tool output): 7 calls · 18,900 tok saved · 1 err",
        "Compresr (context engine): 3 calls · 4,100 tok saved",
    ]


def test_err_suffix_hidden_when_no_errors():
    lines = render_compresr_usage_lines(
        {"tool_output": {"calls": 2, "tokens_saved": 100, "errors": 0}}
    )
    assert lines == ["Compresr (tool output): 2 calls · 100 tok saved"]


def test_no_plugin_leaves_usage_unchanged(fresh_manager, tmp_path):
    assert collect_compresr_usage() is None
    assert render_compresr_usage_lines(None) == []
    path = tmp_path / "usage.json"
    _write_usage_file(str(path), {"input_tokens": 10, "model": "m"})
    data = json.loads(path.read_text())
    assert "compresr" not in data


def test_broken_status_never_raises(fresh_manager):
    class Broken:
        def get_status(self):
            raise RuntimeError("boom")

    fresh_manager._hooks["transform_tool_result"] = [Broken().get_status]
    fresh_manager._context_engine = Broken()
    assert collect_compresr_usage() is None
