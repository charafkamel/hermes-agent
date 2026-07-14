"""Compresr (YC W26) context engine for Hermes.

A drop-in replacement for the built-in ``compressor`` context engine. Instead
of asking an auxiliary LLM to write an abstractive summary of the middle of the
conversation, this engine sends those turns to Compresr's query-specific
compression API (``latte_v1`` / ``latte_v2``), which scores spans against the
current goal and keeps only the answer-bearing ones.

Design: we subclass :class:`ContextCompressor` and override *only* the
"compress the middle window" step (``_generate_summary``) plus ``update_model``
(to recompute token budgets after the real model is known). All of the
surrounding machinery — old-tool-result pruning, head/tail protection, boundary
alignment, tool_call/tool_result pair sanitization, historical-media stripping,
and anti-thrashing accounting — is inherited verbatim. That makes an A/B against
the built-in engine a clean comparison of the *core compaction strategy* only.

Activate via ``~/.hermes/config.yaml``::

    context:
      engine: compresr

Configuration is read from environment first for secrets, then from an optional
``compresr:`` block in config.yaml for non-secret settings:

    COMPRESR_API_KEY        (required, .env only)  cmp_... key
    COMPRESR_BASE_URL       default https://api.compresr.ai/api
    COMPRESR_MODEL          default latte_v2  (latte_v1 | latte_v2)
    COMPRESR_TARGET_RATIO   optional Compresr ratio override (see semantics below)
    COMPRESR_TIMEOUT        default 60 (seconds)
    COMPRESR_COARSE         latte_v1 only: "1"/"0" paragraph vs token-level
    COMPRESR_DISABLE_PLACEHOLDERS  "1" to drop the "[N tokens dropped]" markers

target_compression_ratio semantics (Compresr canonical): a value in (0, 1] is a
*removal* fraction (0.8 removes ~80%); a value > 1 is an Nx target (5 -> ~1/5).
Hermes's ``compression.target_ratio`` is a *keep* fraction (0.2 keeps ~20%), so
when no explicit COMPRESR_TARGET_RATIO is set we map keep-fraction f to the Nx
factor 1/f (0.2 keep -> 5x).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from agent.context_compressor import ContextCompressor
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.compresr.ai/api"
_DEFAULT_MODEL = "latte_v2"
_DEFAULT_TIMEOUT = 60
_FAILURE_COOLDOWN_SECONDS = 30.0
_PLACEHOLDER_CONTEXT_LEN = 200_000
_MIN_KEEP_FRACTION = 0.01
_MAX_KEEP_FRACTION = 0.95
_DEFAULT_KEEP_FRACTION = 0.2
_MAX_NX = 200.0
_SOURCE_TAG = "integration:hermes"
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_SECRET_CTL_RE = re.compile(r"[\r\n\x00]")
_NUMERIC_ONLY_HOST_RE = re.compile(r"\A(?:0x[0-9a-fA-F]+|[0-9]+)\Z")
_FALLBACK_QUERY = (
    "Preserve the key facts, decisions, file paths, commands, results, and open "
    "tasks needed to continue this work."
)


def _read_with_cap(resp: Any, cap: int) -> bytes:
    """Bounded read tolerant of mocks whose read() takes no size arg."""
    try:
        raw = resp.read(cap + 1)
    except TypeError:
        raw = resp.read()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > cap:
        raise RuntimeError(f"response exceeded {cap} bytes")
    return raw


def _as_int(v: Any, default: int = 0) -> int:
    """Tolerant int coercion — never crash on 'N/A' server stats or a typo'd
    numeric config/env value; fall back to *default* instead of raising in
    ``__init__`` and silently disabling the engine."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: Optional[float]) -> Optional[float]:
    """Tolerant float coercion (see :func:`_as_int`)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _read_config_block(key: str = "compresr") -> Dict[str, Any]:
    """Best-effort read of a top-level *key* block from config.yaml.

    Environment variables take precedence; this only fills gaps. Never raises.
    """
    cfg_path = get_hermes_home() / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml

        with open(cfg_path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
        block = data.get(key, {})
        return block if isinstance(block, dict) else {}
    except Exception as e:  # pragma: no cover - config is optional
        logger.debug("compresr: could not read config block %r: %s", key, e)
        return {}


_BLOCKED_METADATA_HOST_NAMES = frozenset({
    "metadata.google.internal", "metadata.goog", "metadata",
})
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in (
        "169.254.0.0/16", "fd00::/8", "fe80::/10",
        "100.100.100.200/32", "168.63.129.16/32",
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    )
)
_LOCALHOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _sanitize_secret(raw: str, label: str) -> str:
    """Strip whitespace and reject CR/LF/NUL — urllib would raise ValueError
    with the raw key in the message. Return '' on rejection (never the value)."""
    if not raw:
        return ""
    stripped = raw.strip()
    if _SECRET_CTL_RE.search(stripped):
        logger.error("compresr: %s contained CR/LF/NUL and was rejected", label)
        return ""
    return stripped


def _resolve_host_ips(host: str) -> tuple:
    if not host:
        return ()
    ips: list = []
    try:
        ip = ipaddress.ip_address(host)
        mapped = getattr(ip, "ipv4_mapped", None)
        ips.append(mapped if mapped is not None else ip)
        return tuple(ips)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, socket.herror, UnicodeError, OSError):
        return ()
    for _, _, _, _, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        except ValueError:
            continue
        mapped = getattr(ip, "ipv4_mapped", None)
        ips.append(mapped if mapped is not None else ip)
    return tuple(ips)


def _is_blocked_host(host: str) -> bool:
    h = (host or "").rstrip(".").lower()
    if h in _BLOCKED_METADATA_HOST_NAMES:
        return True
    for ip in _resolve_host_ips(h):
        if any(ip in net for net in _BLOCKED_NETWORKS):
            return True
    return False


def _safe_url_repr(parsed) -> str:
    if parsed is None:
        return "?"
    try:
        return f"{parsed.scheme or '?'}://{parsed.hostname or '?'}"
    except Exception:
        return "?"


def _secure_base_url(url: str, default: str) -> str:
    """Reject non-HTTPS (except localhost), numeric-shorthand IPv4 literals,
    cloud-metadata hosts, and any host resolving to a private/link-local net.
    Log only scheme://host — the raw URL may carry userinfo credentials."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        parsed = None
    host = (parsed.hostname or "").lower() if parsed else ""
    safe = _safe_url_repr(parsed)

    if parsed and host and _NUMERIC_ONLY_HOST_RE.match(host):
        logger.warning(
            "compresr: refusing numeric-shorthand IPv4 hostname %s; using %s",
            safe, default,
        )
        return default

    if parsed and parsed.scheme in ("http", "https") and host in _LOCALHOST_NAMES:
        return url

    if parsed and _is_blocked_host(host):
        logger.warning(
            "compresr: refusing base_url %s (cloud-metadata or private host); using %s",
            safe, default,
        )
        return default
    if parsed and parsed.scheme == "https":
        return url
    logger.warning(
        "compresr: ignoring insecure base_url %s (must be https://); using %s",
        safe, default,
    )
    return default


class CompresrContextEngine(ContextCompressor):
    """Context engine that compacts via Compresr's query-specific API."""

    def __init__(self, **kwargs: Any) -> None:
        cfg = _read_config_block()

        def _opt(env_key: str, cfg_key: str, default: Any) -> Any:
            val = os.environ.get(env_key)
            if val is not None and val != "":
                return val
            if cfg_key in cfg and cfg[cfg_key] not in (None, ""):
                return cfg[cfg_key]
            return default

        self.compresr_api_key = _sanitize_secret(
            os.environ.get("COMPRESR_API_KEY", ""), "COMPRESR_API_KEY"
        )
        self.compresr_base_url = _secure_base_url(
            str(_opt("COMPRESR_BASE_URL", "base_url", _DEFAULT_BASE_URL)).rstrip("/"),
            _DEFAULT_BASE_URL,
        )
        self.compresr_model = str(_opt("COMPRESR_MODEL", "model", _DEFAULT_MODEL))
        self.compresr_timeout = max(
            1, _as_int(_opt("COMPRESR_TIMEOUT", "timeout", _DEFAULT_TIMEOUT), _DEFAULT_TIMEOUT)
        )
        self.compresr_coarse = str(_opt("COMPRESR_COARSE", "coarse", "")).lower() in (
            "1", "true", "yes",
        )
        self.compresr_disable_placeholders = str(
            _opt("COMPRESR_DISABLE_PLACEHOLDERS", "disable_placeholders", "")
        ).lower() in ("1", "true", "yes")
        self.compresr_ratio_override: Optional[float] = _as_float(
            _opt("COMPRESR_TARGET_RATIO", "target_ratio", ""), None
        )

        self.compresr_calls = 0
        self.compresr_errors = 0
        self.compresr_tokens_in = 0
        self.compresr_tokens_saved = 0
        self.compresr_last_duration_ms = 0

        # Forward the user's compression.* geometry (threshold / protected head &
        # tail / keep-ratio) so `context.engine: compresr` honors the same tuning
        # as the built-in compressor instead of silently using hardcoded defaults.
        # Only fills gaps — an explicit caller kwarg still wins.
        comp_cfg = _read_config_block("compression")
        if "threshold" in comp_cfg:
            _thr = _as_float(comp_cfg.get("threshold"), None)
            if _thr is not None:
                kwargs.setdefault("threshold_percent", _thr)
        if "protect_first_n" in comp_cfg:
            kwargs.setdefault("protect_first_n", _as_int(comp_cfg.get("protect_first_n"), 3))
        if "protect_last_n" in comp_cfg:
            kwargs.setdefault("protect_last_n", _as_int(comp_cfg.get("protect_last_n"), 20))
        if "target_ratio" in comp_cfg:
            _ratio = _as_float(comp_cfg.get("target_ratio"), None)
            if _ratio is not None:
                kwargs.setdefault("summary_target_ratio", _ratio)

        # Construct the parent with a placeholder model + explicit context length
        # so __init__ never makes a network/model-metadata lookup. agent_init
        # always calls update_model() right after, which sets the real values.
        kwargs.setdefault("model", "compresr-placeholder")
        kwargs.setdefault("config_context_length", _PLACEHOLDER_CONTEXT_LEN)
        # Compresr is an external service. If it is unavailable, preserve the
        # current transcript instead of inserting a deterministic placeholder
        # handoff and dropping the middle window.
        kwargs.setdefault("abort_on_summary_failure", True)
        super().__init__(**kwargs)

        if not self.compresr_api_key:
            logger.warning(
                "compresr: COMPRESR_API_KEY is not set — compaction will fail and "
                "compression will be aborted without dropping context. Set it in "
                "~/.hermes/.env."
            )

    # -- Identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "compresr"

    def is_available(self) -> bool:
        """Used by discover_context_engines() for the availability check."""
        return bool(self.compresr_api_key)

    # -- Token budgets must be recomputed when the real model arrives ------

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: Any = "",
        provider: str = "",
        api_mode: str = "",
        max_tokens: int | None = None,
    ) -> None:
        # Pure pass-through: the parent already computes all token budgets. This
        # override exists only to stay signature-compatible as the parent evolves
        # (re-flooring context_length here regressed small-context compaction).
        super().update_model(
            model, context_length, base_url, api_key, provider, api_mode, max_tokens
        )

    # -- Ratio mapping -----------------------------------------------------

    def _target_compression_ratio(self) -> float:
        """Resolve the Compresr ``target_compression_ratio`` to send.

        Explicit override wins. Otherwise map Hermes's keep-fraction
        ``summary_target_ratio`` (e.g. 0.2 = keep ~20%) to a Compresr Nx factor
        (1/0.2 = 5x). Clamp to Compresr's documented [.., 200] range.
        """
        if self.compresr_ratio_override is not None:
            return self.compresr_ratio_override
        keep = self.summary_target_ratio or _DEFAULT_KEEP_FRACTION
        keep = min(max(keep, _MIN_KEEP_FRACTION), _MAX_KEEP_FRACTION)
        return round(min(1.0 / keep, _MAX_NX), 3)

    # -- The one override that matters: compress the middle via Compresr ---

    def _generate_summary(
        self,
        turns_to_summarize: List[Dict[str, Any]],
        focus_topic: Optional[str] = None,
    ) -> Optional[str]:
        """Compress the middle window with Compresr instead of an LLM summary.

        Returns the compressed text (with the standard summary prefix so
        iterative re-compaction recognizes it), or None on failure — which lets
        the inherited ``compress()`` fall back to its deterministic handoff.
        """
        now = time.monotonic()
        if now < self._summary_failure_cooldown_until:
            logger.debug("compresr: in failure cooldown, skipping")
            return None

        context = self._serialize_for_summary(turns_to_summarize)
        if not context.strip():
            return None

        query = (focus_topic or "").strip() or _FALLBACK_QUERY

        # Preserve continuity: fold the prior handoff summary into the context so
        # Compresr can carry forward still-relevant facts across compactions.
        if self._previous_summary:
            context = (
                "[PRIOR CONTEXT SUMMARY]\n"
                + self._previous_summary
                + "\n\n[NEW CONVERSATION TURNS]\n"
                + context
            )

        try:
            compressed, stats = self._call_compresr(context, query)
        except Exception as e:
            self.compresr_errors += 1
            self._last_summary_error = f"compresr: {e}"
            # Mirror the base's failure behavior via the parent API so the
            # back-off persists to the session DB and survives across processes
            # (resume / gateway restart), not just in-memory for this process.
            self._record_compression_failure_cooldown(
                _FAILURE_COOLDOWN_SECONDS, self._last_summary_error
            )
            logger.warning("compresr: compression failed (%s) — falling back", e)
            return None

        if not compressed or not compressed.strip():
            self.compresr_errors += 1
            self._last_summary_error = "compresr: empty compressed_context"
            # Back off like the exception path (persisted), so a persistently-empty
            # endpoint isn't re-hit on every compaction across processes.
            self._record_compression_failure_cooldown(
                _FAILURE_COOLDOWN_SECONDS, self._last_summary_error
            )
            return None

        self.compresr_calls += 1
        self.compresr_tokens_in += _as_int(stats.get("original_tokens"))
        self.compresr_tokens_saved += _as_int(stats.get("tokens_saved"))
        self.compresr_last_duration_ms = _as_int(stats.get("duration_ms"))
        # Clear any prior failure back-off on success so a transient error
        # doesn't leave stale cooldown state in the session DB forever.
        self._summary_failure_cooldown_until = 0.0
        self._last_summary_error = None
        logger.info(
            "compresr: %s %s tokens -> %s tokens (saved %s, %sms server)",
            self.compresr_model,
            stats.get("original_tokens", "?"),
            stats.get("compressed_tokens", "?"),
            stats.get("tokens_saved", "?"),
            stats.get("duration_ms", "?"),
        )

        body = self._strip_summary_prefix(compressed)
        self._previous_summary = body
        return self._with_summary_prefix(body)

    # -- Compresr API call -------------------------------------------------

    def _call_compresr(self, context: str, query: str) -> tuple[str, Dict[str, Any]]:
        """POST to /compress/question-specific/ and return (text, stats).

        Raises on transport/HTTP/parse errors so the caller can fall back.
        """
        if not self.compresr_api_key:
            raise RuntimeError("COMPRESR_API_KEY not set")

        payload: Dict[str, Any] = {
            "context": context,
            "query": query,
            "compression_model_name": self.compresr_model,
            "target_compression_ratio": self._target_compression_ratio(),
            "source": _SOURCE_TAG,
        }
        if self.compresr_model == "latte_v1":
            payload["coarse"] = self.compresr_coarse
        if self.compresr_disable_placeholders:
            payload["disable_placeholders"] = True

        url = f"{self.compresr_base_url}/compress/question-specific/"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.compresr_api_key,
                "User-Agent": "hermes-compresr-engine/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.compresr_timeout) as resp:
                raw = _read_with_cap(resp, _MAX_RESPONSE_BYTES).decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = _read_with_cap(e, 4096).decode("utf-8")[:300]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"connection error: {e.reason}") from e
        except ValueError:
            # urllib raises ValueError containing the raw API key when a header
            # value has CTL chars — swallow the chain so it never reaches logs.
            raise RuntimeError(
                "invalid request headers (check COMPRESR_API_KEY for stray whitespace/CRLF)"
            ) from None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"non-JSON response ({e}); body prefix: {raw[:200]!r}") from e
        if not parsed.get("success", False):
            raise RuntimeError(
                f"API error: {parsed.get('message') or parsed.get('error')}"
            )
        d = parsed.get("data") or {}
        compressed = d.get("compressed_context", "")
        return compressed, d

    # -- Status (extends base with Compresr counters) ---------------------

    def get_status(self) -> Dict[str, Any]:
        status = super().get_status()
        status.update(
            {
                "engine": "compresr",
                "compresr_model": self.compresr_model,
                "compresr_calls": self.compresr_calls,
                "compresr_errors": self.compresr_errors,
                "compresr_tokens_in": self.compresr_tokens_in,
                "compresr_tokens_saved": self.compresr_tokens_saved,
                "compresr_last_duration_ms": self.compresr_last_duration_ms,
            }
        )
        return status


def register(ctx: Any) -> None:
    """Plugin entry point — called by the context-engine loader."""
    engine = CompresrContextEngine()
    # Refuse to register when unavailable so the loader falls back to the
    # built-in compressor instead of ending sessions in a provider-side
    # context-limit error under abort_on_summary_failure=True.
    if not engine.is_available():
        logger.error(
            "compresr: refusing to register — engine is not available "
            "(COMPRESR_API_KEY missing). Falling back to built-in compressor."
        )
        return
    ctx.register_context_engine(engine)
