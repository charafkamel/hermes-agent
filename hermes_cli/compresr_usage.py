"""Best-effort Compresr plugin counters for usage reporting.

The Compresr SDK plugin registers a ``transform_tool_result`` hook (tool-output
compression) and optionally a context engine; both bound instances expose
``get_status()``. These helpers surface those counters in ``--usage-file``
reports and the interactive ``/usage`` display. Everything is defensive: when
the plugin is absent (or anything fails) callers get ``None`` / no lines, so
vanilla runs are byte-identical to before.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_TOOL_OUTPUT_KEYS = (
    "calls",
    "errors",
    "tokens_in",
    "tokens_saved",
    "recoveries",
    "active",
    "min_tokens",
    "model",
)

_ENGINE_KEY_MAP = (
    ("calls", "compresr_calls"),
    ("errors", "compresr_errors"),
    ("tokens_in", "compresr_tokens_in"),
    ("tokens_saved", "compresr_tokens_saved"),
    ("last_duration_ms", "compresr_last_duration_ms"),
    ("model", "compresr_model"),
)


def _safe_status(obj: Any) -> Optional[Dict[str, Any]]:
    try:
        get_status = getattr(obj, "get_status", None)
        if not callable(get_status):
            return None
        status = get_status()
        return status if isinstance(status, dict) else None
    except Exception:
        return None


def _tool_output_status() -> Optional[Dict[str, Any]]:
    try:
        from hermes_cli.plugins import get_plugin_manager

        hooks = getattr(get_plugin_manager(), "_hooks", None) or {}
        for callback in list(hooks.get("transform_tool_result") or []):
            status = _safe_status(getattr(callback, "__self__", None))
            if status and "compresr" in str(status.get("plugin", "")).lower():
                return status
    except Exception:
        pass
    return None


def _context_engine_status(context_engine: Any = None) -> Optional[Dict[str, Any]]:
    candidates: List[Any] = [context_engine]
    try:
        from hermes_cli.plugins import get_plugin_manager

        candidates.append(getattr(get_plugin_manager(), "_context_engine", None))
    except Exception:
        pass
    for candidate in candidates:
        status = _safe_status(candidate)
        if status and "compresr" in (
            str(status.get("engine", "")) + str(status.get("plugin", ""))
        ).lower():
            return status
    return None


def collect_compresr_usage(
    context_engine: Any = None,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Return ``{"tool_output": {...}, "context_engine": {...}}`` or ``None``.

    Only sub-blocks that resolved are included; ``None`` means the Compresr
    plugin is not loaded (or exposed nothing usable). Never raises.
    """
    try:
        report: Dict[str, Dict[str, Any]] = {}
        status = _tool_output_status()
        if status:
            block = {key: status[key] for key in _TOOL_OUTPUT_KEYS if key in status}
            if block:
                report["tool_output"] = block
        status = _context_engine_status(context_engine)
        if status:
            block = {
                dst: status[src] for dst, src in _ENGINE_KEY_MAP if src in status
            }
            if block:
                report["context_engine"] = block
        return report or None
    except Exception:
        return None


def _format_count_parts(block: Dict[str, Any]) -> List[str]:
    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    parts = [
        f"{_as_int(block.get('calls')):,} calls",
        f"{_as_int(block.get('tokens_saved')):,} tok saved",
    ]
    errors = _as_int(block.get("errors"))
    if errors > 0:
        parts.append(f"{errors:,} err")
    return parts


def render_compresr_usage_lines(
    report: Optional[Dict[str, Dict[str, Any]]],
) -> List[str]:
    """Display lines for ``/usage``; empty list when the plugin is absent."""
    try:
        if not report:
            return []
        lines: List[str] = []
        tool_output = report.get("tool_output")
        if tool_output:
            lines.append(
                "Compresr (tool output): "
                + " · ".join(_format_count_parts(tool_output))
            )
        engine = report.get("context_engine")
        if engine:
            lines.append(
                "Compresr (context engine): "
                + " · ".join(_format_count_parts(engine))
            )
        return lines
    except Exception:
        return []
