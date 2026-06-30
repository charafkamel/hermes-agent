"""Persistent cumulative Compresr savings + the ``/compresr`` slash command.

Unlike the local-only debug event log, this is a SHIPPED, lightweight counter so
any user who turned Compresr on can run ``/compresr`` and see how many tokens
they've saved. Both compresr plugins write here:

  * ``plugins/context_engine/compresr``   — compaction-time savings
  * ``plugins/tool_output_compresr``       — per-turn tool-output savings + recoveries

State is a single JSON file at ``~/.hermes/compresr/stats.json`` holding only
aggregate counts (no output content, no PII). All reads/writes are best-effort
and fail-open: stats must never affect compression or break a session. Updates
take an exclusive file lock so the several gateway processes don't lose
increments; if locking is unavailable the update degrades to a plain read-modify
-write.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_VERSION = 1
_SECTIONS = ("compaction", "tool_output")


def _path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "compresr" / "stats.json"


def _blank() -> Dict[str, Any]:
    return {
        "version": _VERSION,
        "since": None,
        "updated": None,
        "compaction": {"calls": 0, "tokens_in": 0, "tokens_saved": 0, "errors": 0},
        "tool_output": {
            "calls": 0,
            "tokens_in": 0,
            "tokens_saved": 0,
            "errors": 0,
            "recoveries": 0,
        },
    }


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _coerce(raw: str) -> Dict[str, Any]:
    """Parse stored JSON into a well-shaped dict; never raises."""
    base = _blank()
    if not raw or not raw.strip():
        return base
    try:
        d = json.loads(raw)
    except Exception:
        return base
    if not isinstance(d, dict):
        return base
    base["since"] = d.get("since") or None
    base["updated"] = d.get("updated") or None
    for sect in _SECTIONS:
        src = d.get(sect)
        if isinstance(src, dict):
            for k, v in base[sect].items():
                if isinstance(src.get(k), (int, float)):
                    base[sect][k] = int(src[k])
    return base


def load() -> Dict[str, Any]:
    """Return the current aggregate counts (blank skeleton if unreadable)."""
    try:
        p = _path()
        if not p.exists():
            return _blank()
        return _coerce(p.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("compresr stats: load failed: %s", e)
        return _blank()


def _update(section: str, **deltas: int) -> None:
    """Add the given deltas to ``section`` counters. Best-effort, fail-open."""
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl  # POSIX only; absent on Windows -> plain RMW below
        except Exception:
            fcntl = None
        with open(p, "a+", encoding="utf-8") as f:
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass
            f.seek(0)
            d = _coerce(f.read())
            now = _now_iso()
            if not d.get("since"):
                d["since"] = now
            d["updated"] = now
            sect = d.setdefault(section, {})
            for k, v in deltas.items():
                sect[k] = int(sect.get(k, 0)) + int(v)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(d, indent=2))
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception as e:  # pragma: no cover - stats must never break the tool path
        logger.debug("compresr stats: update failed: %s", e)


# -- public recording API ---------------------------------------------------

def record_compaction(tokens_in: int = 0, tokens_saved: int = 0) -> None:
    _update("compaction", calls=1, tokens_in=max(0, tokens_in), tokens_saved=max(0, tokens_saved))


def record_tool_output(tokens_in: int = 0, tokens_saved: int = 0) -> None:
    _update("tool_output", calls=1, tokens_in=max(0, tokens_in), tokens_saved=max(0, tokens_saved))


def record_recovery() -> None:
    _update("tool_output", recoveries=1)


def record_error(section: str) -> None:
    if section in _SECTIONS:
        _update(section, errors=1)


# -- /compresr renderer ------------------------------------------------------

def render(_raw_args: str = "") -> str:
    """Handler for the ``/compresr`` slash command: a human-readable savings panel."""
    d = load()
    c, t = d["compaction"], d["tool_output"]
    total_saved = c["tokens_saved"] + t["tokens_saved"]
    total_in = c["tokens_in"] + t["tokens_in"]

    if not c["calls"] and not t["calls"]:
        return (
            "Compresr is enabled but hasn't compressed anything yet.\n"
            "Large tool outputs (≥ the min-tokens floor) and context compactions "
            "will show up here once they happen."
        )

    since = (d.get("since") or "")[:10] or "—"
    out = [f"Compresr — savings since {since}"]
    if c["calls"]:
        err = f" · {c['errors']} err" if c["errors"] else ""
        out.append(f"  Compaction:   {c['calls']:>4} calls · {c['tokens_saved']:>10,} tokens saved{err}")
    if t["calls"]:
        err = f" · {t['errors']} err" if t["errors"] else ""
        out.append(f"  Tool output:  {t['calls']:>4} calls · {t['tokens_saved']:>10,} tokens saved{err}")
        pct = (100.0 * t["recoveries"] / t["calls"]) if t["calls"] else 0.0
        out.append(f"  Recoveries:   {t['recoveries']:>4}   ({pct:.1f}% of tool-output compressions needed a read-back)")
    reduction = f"  (~{100.0 * total_saved / total_in:.0f}% reduction)" if total_in else ""
    out.append(f"  Total saved:  {total_saved:,} tokens{reduction}")
    return "\n".join(out)


def register_slash_command(ctx: Any) -> None:
    """Register ``/compresr`` once. Safe to call from both plugins (dedup is handled
    by the command registry); fail-open if the host doesn't support commands."""
    try:
        ctx.register_command(
            "compresr",
            render,
            description="Show cumulative Compresr token savings and metadata.",
        )
    except Exception as e:  # pragma: no cover - optional host capability
        logger.debug("compresr stats: could not register /compresr: %s", e)
