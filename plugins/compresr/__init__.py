"""Compresr Hermes plugin — a thin shim over the ``compresr`` PyPI SDK.

Registers Compresr's tool-output cache subdir (so cached files resolve on
Docker/Modal/SSH backends), then delegates to the SDK's ``register(ctx)``.
Fail-open: inert without the SDK or an API key.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Used only when the SDK isn't importable, so cache wiring still happens;
# otherwise the SDK's cache.CACHE_SUBPATH is the source of truth.
_FALLBACK_CACHE_SUBPATH = "cache/compresr/tool-output"

_INSTALL_HINT = (
    "compresr: the `compresr` Python package is not installed in Hermes's "
    "environment. Install it with `pip install compresr` (same interpreter "
    "that runs Hermes), then restart Hermes."
)


def _emit_console_warning(text: str) -> None:
    """Best-effort user-visible warning. Hermes routes ``logging`` to files
    only, so a plain ``logger.warning`` never reaches the user — and a missing
    SDK is the failure they most need to see. Mirrors the SDK's own emitter
    rather than importing it: the SDK is exactly what is unavailable here.
    Never raises."""
    try:
        from hermes_cli.cli_output import print_warning

        print_warning(text)
        return
    except Exception:
        pass
    try:
        import os
        import sys

        stream = sys.stderr
        if stream is None:
            return
        # A daemonised parent may have closed fd 2. isatty() returns False
        # rather than raising there, so the write below would succeed into the
        # buffer, fail at the syscall, and leave CPython unable to flush at
        # shutdown — exit code 120 from a warning. Probe the fd first; bail via
        # the outer except. No fileno (StringIO, capsys) means it is safe.
        try:
            fd = stream.fileno()
        except Exception:
            fd = None
        if fd is not None:
            os.fstat(fd)

        color = stream.isatty() and not os.environ.get("NO_COLOR")
        line = f"\033[33m⚠ {text}\033[0m" if color else f"⚠ {text}"
        stream.write(line + "\n")
        stream.flush()
    except Exception:
        pass


def register(ctx: Any) -> None:
    """Wire the SDK's cache dir into the backend surface, then delegate to it."""
    try:
        from compresr.integrations.hermes.cache import CACHE_SUBPATH
    except Exception:
        CACHE_SUBPATH = _FALLBACK_CACHE_SUBPATH

    try:
        from tools.credential_files import register_cache_dir

        register_cache_dir(CACHE_SUBPATH)
    except Exception as e:  # cache wiring must never block plugin load
        logger.debug("compresr: could not register cache dir (%s)", e)

    try:
        from compresr.integrations.hermes.plugin import register as _sdk_register
    except ImportError:
        # Warn, not error: the fail-open state shouldn't trip error alerting.
        logger.warning(_INSTALL_HINT)
        _emit_console_warning(_INSTALL_HINT)
        return
    _sdk_register(ctx)


__all__ = ["register"]
