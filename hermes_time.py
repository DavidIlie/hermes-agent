"""
Timezone-aware clock for Hermes.

Provides a single ``now()`` helper that returns a timezone-aware datetime
based on the user's configured IANA timezone (e.g. ``Asia/Kolkata``).

Resolution order:
  1. ``HERMES_TIMEZONE`` environment variable
  2. ``timezone`` key in ``~/.hermes/config.yaml``
  3. Falls back to the server's local time (``datetime.now().astimezone()``)

Invalid timezone values log a warning and fall back safely — Hermes never
crashes due to a bad timezone string.
"""

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python 3.8 fallback (shouldn't be needed — Hermes requires 3.9+)
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

# Cached state — resolved once, reused on every call.
# Call reset_cache() to force re-resolution (e.g. after config changes).
_cached_tz: Optional[ZoneInfo] = None
_cached_tz_name: Optional[str] = None
_cache_resolved: bool = False

# A multiplex gateway serves profiles with independent config files in one
# process.  The process-level cache above remains the fast path for legacy
# single-profile gateways, while this context-local override prevents the
# primary profile's HERMES_TIMEZONE/cache from leaking into a secondary turn
# or cron tick.
_timezone_override: ContextVar[Optional[str]] = ContextVar(
    "hermes_timezone_override",
    default=None,
)


def _resolve_timezone_name() -> str:
    """Read the configured IANA timezone string (or empty string).

    This does file I/O when falling through to config.yaml, so callers
    should cache the result rather than calling on every ``now()``.
    """
    # 1. Environment variable (highest priority — set by Supervisor, etc.)
    tz_env = os.getenv("HERMES_TIMEZONE", "").strip()
    if tz_env:
        return tz_env

    # 2. config.yaml ``timezone`` key
    try:
        # Use the config module's raw reader so this stays inside the shared
        # cache and config-read policy boundary.
        from hermes_cli.config import read_user_config_raw

        cfg = read_user_config_raw() or {}
        if cfg:
            # Managed scope: an administrator can pin ``timezone`` too. Overlay
            # via the shared helper (fail-open) since this reads config.yaml directly.
            try:
                from hermes_cli import managed_scope
                cfg = managed_scope.apply_managed_overlay(cfg)
            except Exception:
                pass
            tz_cfg = cfg.get("timezone", "")
            if isinstance(tz_cfg, str) and tz_cfg.strip():
                return tz_cfg.strip()
    except Exception:
        pass

    return ""


def _get_zoneinfo(name: str) -> Optional[ZoneInfo]:
    """Validate and return a ZoneInfo, or None if invalid."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, Exception) as exc:
        logger.warning(
            "Invalid timezone '%s': %s. Falling back to server local time.",
            name, exc,
        )
        return None


def get_timezone() -> Optional[ZoneInfo]:
    """Return the user's configured ZoneInfo, or None (meaning server-local).

    Resolved once and cached. Call ``reset_cache()`` after config changes.
    """
    override = _timezone_override.get()
    if override is not None:
        return _get_zoneinfo(override)

    global _cached_tz, _cached_tz_name, _cache_resolved
    if not _cache_resolved:
        _cached_tz_name = _resolve_timezone_name()
        _cached_tz = _get_zoneinfo(_cached_tz_name)
        _cache_resolved = True
    return _cached_tz


def _profile_timezone_name(profile_home: str | Path) -> str:
    """Read one profile's explicit timezone without consulting process env."""
    config_path = Path(profile_home).expanduser().resolve() / "config.yaml"
    try:
        if not config_path.exists():
            return ""
        from hermes_cli.config import read_user_config_raw

        cfg = read_user_config_raw(config_path) or {}
        value = cfg.get("timezone", "") if isinstance(cfg, dict) else ""
        return value.strip() if isinstance(value, str) else ""
    except Exception as exc:
        logger.warning(
            "Could not read timezone from profile config %s: %s",
            config_path,
            exc,
        )
        return ""


@contextmanager
def use_profile_timezone(profile_home: str | Path) -> Iterator[None]:
    """Scope ``now()`` to a profile's configured timezone when it has one."""
    timezone_name = _profile_timezone_name(profile_home)
    if not timezone_name:
        yield
        return

    token = _timezone_override.set(timezone_name)
    try:
        yield
    finally:
        _timezone_override.reset(token)


def reset_cache() -> None:
    """Clear the cached timezone so the next call re-resolves it.

    Call this after the configured timezone may have changed (e.g. after a
    config edit or ``HERMES_TIMEZONE`` update) to force ``get_timezone()`` /
    ``now()`` to read the new value instead of the value cached at first use.
    """
    global _cached_tz, _cached_tz_name, _cache_resolved
    _cached_tz = None
    _cached_tz_name = None
    _cache_resolved = False


def now() -> datetime:
    """
    Return the current time as a timezone-aware datetime.

    If a valid timezone is configured, returns wall-clock time in that zone.
    Otherwise returns the server's local time (via ``astimezone()``).
    """
    tz = get_timezone()
    if tz is not None:
        return datetime.now(tz)
    # No timezone configured — use server-local (still tz-aware)
    return datetime.now().astimezone()
