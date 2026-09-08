"""Monkeypatches injecting the IRC extras into the core IRC adapter.

Two features live here:

* SSL verification bypass (``IRC_ALLOW_INVALID_SSL``).
* Opt-in passive channel logging (``IRC_ENABLE_CHANNEL_LOGGING``), which taps
  ``IRCAdapter._handle_line`` *ahead* of the adapter's own addressing and
  authorization gates. Every PRIVMSG is written to SQLite; the original method
  then applies its unchanged early-returns, so unaddressed and unauthorized
  traffic is recorded without ever reaching the turn machinery — zero LLM
  cost for chatter the agent is only observing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

_patched = False
_warned = False

#: The host registration context and whether the log tools were already
#: registered with it, remembered by :func:`note_plugin_context`. Channel
#: logging can be enabled in ``config.yaml`` alone, and the adapter's config —
#: the authoritative source for that — only exists long after the plugin was
#: loaded, so the tools may have to be registered late.
_plugin_ctx: Any = None
_log_tools_registered = False

#: Minimum wall-clock gap between opportunistic retention prunes, per adapter.
_PRUNE_INTERVAL_SECONDS = 3600.0

#: Marker set on our wrappers so re-applying the patches (the unit tests reset
#: ``_patched`` and re-patch) cannot chain a wrapper onto itself — for the
#: logging tap that would mean every message stored twice.
_WRAPPED_ATTR = "_irc_extras_wrapped"


def apply_patches() -> bool:
    """Register lazy patches to execute exactly when the IRC platform is loaded.

    Returns True if successfully registered.
    """
    try:
        from gateway.platform_registry import platform_registry
    except ImportError:
        # Host is unavailable (e.g. running mock unit tests)
        return False

    _orig_get = platform_registry.get

    def _wrapped_get(name: str) -> Any | None:
        entry = _orig_get(name)
        if name == "irc":
            _apply_irc_patches()
        return entry

    platform_registry.get = _wrapped_get

    # Register the environment metadata on load
    _inject_metadata_declarations()

    return True


def note_plugin_context(ctx: Any, registered: bool) -> None:
    """Remember the host context and whether the log tools went on it.

    Called once by :func:`hermes_irc_extras.register`. Keeping the context
    lets an adapter that turns out to have channel logging enabled — in
    ``config.yaml``, which the plugin loader could not see — still expose the
    query tools instead of logging into a database nothing can read.
    """
    global _plugin_ctx, _log_tools_registered
    _plugin_ctx = ctx
    _log_tools_registered = bool(registered)


#: Env vars this plugin contributes to the WebUI / Desktop UI config surface.
_OPTIONAL_ENV_VAR_METADATA: dict[str, dict[str, Any]] = {
    "IRC_ALLOW_INVALID_SSL": {
        "description": (
            "Accept invalid/self-signed IRC TLS certificates (true/false). "
            "Disables certificate and hostname verification — the connection "
            "stays encrypted but is no longer authenticated. Default: false."
        ),
        "prompt": "Accept invalid/self-signed SSL certs (true/false)",
        "url": None,
        "password": False,
        "category": "messaging",
        "advanced": True,
    },
    "IRC_ENABLE_CHANNEL_LOGGING": {
        "description": (
            "Passively log all IRC channel messages to a local SQLite database "
            "(true/false), including messages that are not addressed to the bot "
            "and messages from users outside IRC_ALLOWED_USERS. Direct messages "
            "to the bot are logged too, and every record keeps the sender's nick "
            "and user@host (their hostname or cloak) alongside the message text. "
            "Unaddressed traffic is still never answered and costs no agent "
            "turns; the log is only readable via the search_irc_logs and "
            "get_channel_history tools. Records third parties' chat and "
            "identifying metadata — check local expectations, and any network "
            "or data-protection policy, before enabling. Default: false."
        ),
        "prompt": "Passively log IRC channel messages to SQLite (true/false)",
        "url": None,
        "password": False,
        "category": "messaging",
        "advanced": True,
    },
    "IRC_CHANNEL_LOG_DB_PATH": {
        "description": (
            "Absolute path to the IRC channel log SQLite database. Holds "
            "channel and direct-message text plus each sender's user@host; new "
            "database files are created with owner-only (0600) permissions. "
            "Default: {profile}/state/irc_channel_logs.db."
        ),
        "prompt": "IRC channel log database path (blank for default)",
        "url": None,
        "password": False,
        "category": "messaging",
        "advanced": True,
    },
    "IRC_CHANNEL_LOG_RETENTION_DAYS": {
        "description": (
            "Days of IRC channel and direct-message scrollback to retain; older "
            "records (message text and sender user@host alike) are pruned "
            "automatically. Use 0 to keep everything forever. Default: 14."
        ),
        "prompt": "IRC channel log retention in days",
        "url": None,
        "password": False,
        "category": "messaging",
        "advanced": True,
    },
}


def _inject_metadata_declarations() -> None:
    """Inject the new environment variable metadata into OPTIONAL_ENV_VARS dynamically.

    This ensures that the WebUI and Desktop UI can render the checkmark option
    on the Channels page for IRC.
    """
    try:
        from hermes_cli.config_defaults import OPTIONAL_ENV_VARS

        for name, metadata in _OPTIONAL_ENV_VAR_METADATA.items():
            if name not in OPTIONAL_ENV_VARS:
                OPTIONAL_ENV_VARS[name] = dict(metadata)
    except Exception:
        logger.debug("Could not inject optional env var metadata", exc_info=True)


def _apply_irc_patches() -> None:
    """Surgically apply patches to the IRC adapter module(s)."""
    global _patched
    if _patched:
        return

    target_mods = []
    # Check if loaded under hermes_plugins namespace
    if "hermes_plugins.irc_platform.adapter" in sys.modules:
        target_mods.append(sys.modules["hermes_plugins.irc_platform.adapter"])

    try:
        import plugins.platforms.irc.adapter as adapter_mod
        if adapter_mod not in target_mods:
            target_mods.append(adapter_mod)
    except Exception:
        pass

    for mod in list(sys.modules.values()):
        name = getattr(mod, "__name__", "")
        if name and (
            name.endswith("irc_platform.adapter") or name.endswith("platforms.irc.adapter")
        ):
            if mod not in target_mods:
                target_mods.append(mod)

    if not target_mods:
        logger.warning("hermes-irc-extras: Could not locate IRC adapter module")
        return

    logger.debug(
        "hermes-irc-extras: Applying lazy patches to %d IRC adapter module(s)",
        len(target_mods),
    )

    patched_any = False
    for mod in target_mods:
        try:
            _patch_adapter_module(mod)
            patched_any = True
        except Exception:
            logger.warning(
                "hermes-irc-extras: Failed to apply IRC adapter patches to %s",
                getattr(mod, "__name__", "unknown"),
                exc_info=True,
            )

    if patched_any:
        _patched = True


def _patch_adapter_module(adapter_mod: Any) -> None:
    """Apply every individual patch, skipping any whose target has drifted away."""
    # 1. Define custom context builder. The original is taken from the real `ssl`
    #    module so that re-applying the patches can never chain onto our own shim.
    _orig_create_default_context = ssl.create_default_context

    def _new_build_ssl_context(allow_invalid: bool, *args: Any, **kwargs: Any) -> Any:
        global _warned
        ctx = _orig_create_default_context(*args, **kwargs)
        if allow_invalid:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            if not _warned:
                _warned = True
                # Use module logger to match output expectations
                logging.getLogger("plugins.platforms.irc.adapter").warning(
                    "IRC: IRC_ALLOW_INVALID_SSL is enabled — TLS certificate and "
                    "hostname verification are disabled. The connection is encrypted "
                    "but not authenticated (vulnerable to MITM); use only with "
                    "servers you control."
                )
        return ctx

    def _resolve_allow_invalid() -> bool:
        """Resolve the flag from the environment first, then from caller frame locals."""
        env = (os.getenv("IRC_ALLOW_INVALID_SSL") or "").strip()
        if env:
            return env.lower() in {"1", "true", "yes"}
        try:
            frame = sys._getframe(1)
            while frame:
                locals_ = frame.f_locals
                if "self" in locals_:
                    self_obj = locals_["self"]
                    if self_obj.__class__.__name__ == "IRCAdapter":
                        return bool(getattr(self_obj, "allow_invalid_ssl", False))
                if "config" in locals_:
                    config_obj = locals_["config"]
                    if hasattr(config_obj, "extra"):
                        extra = config_obj.extra or {}
                        val = extra.get("allow_invalid_ssl", False)
                        if isinstance(val, str):
                            return val.strip().lower() in {"1", "true", "yes"}
                        return bool(val)
                frame = frame.f_back
        except Exception:
            pass
        return False

    class _IRCSSLShim:
        """Stand-in for the `ssl` name inside the IRC adapter module ONLY.

        Every attribute except ``create_default_context`` is delegated to the real
        ``ssl`` module, so the global module is never mutated and other subsystems
        (provider auth, email, ...) keep full certificate verification.
        """

        __slots__ = ("_real_ssl",)

        def __init__(self, real_ssl: Any) -> None:
            self._real_ssl = real_ssl

        def create_default_context(self, *args: Any, **kwargs: Any) -> Any:
            return _new_build_ssl_context(_resolve_allow_invalid(), *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(object.__getattribute__(self, "_real_ssl"), name)

    # 2. Shadow the adapter module's `ssl` name with the shim. The global `ssl`
    #    module is deliberately left untouched.
    if hasattr(adapter_mod, "ssl"):
        adapter_mod.ssl = _IRCSSLShim(ssl)
    else:
        logger.warning("hermes-irc-extras: skipping ssl shim — adapter has no `ssl` attribute")

    # 3. Define helper for config resolution
    def _new_allow_invalid_ssl(extra: dict[str, Any] | None = None) -> bool:
        env = (os.getenv("IRC_ALLOW_INVALID_SSL") or "").strip()
        if env:
            return env.lower() in {"1", "true", "yes"}
        val = (extra or {}).get("allow_invalid_ssl", False)
        if isinstance(val, str):
            return val.strip().lower() in {"1", "true", "yes"}
        return bool(val)

    adapter_mod._allow_invalid_ssl = _new_allow_invalid_ssl
    adapter_mod._build_ssl_context = _new_build_ssl_context

    # 4. Wrap IRCAdapter.__init__ to store self.allow_invalid_ssl and resolve
    #    the channel-logging settings, then install the ingestion tap.
    if hasattr(adapter_mod, "IRCAdapter"):
        _orig_init = adapter_mod.IRCAdapter.__init__

        if not getattr(_orig_init, _WRAPPED_ATTR, False):

            def _wrapped_init(self, config: Any, **kwargs: Any) -> None:
                _orig_init(self, config, **kwargs)
                extra = getattr(config, "extra", {}) or {}
                self.allow_invalid_ssl = _new_allow_invalid_ssl(extra)
                _init_channel_logging(self, extra)

            setattr(_wrapped_init, _WRAPPED_ATTR, True)
            adapter_mod.IRCAdapter.__init__ = _wrapped_init

        _patch_handle_line(adapter_mod)
    else:
        logger.warning("hermes-irc-extras: skipping IRCAdapter.__init__ patch — class not found")

    # 5. Wrap _env_enablement to inject allow_invalid_ssl into the registry seed
    if hasattr(adapter_mod, "_env_enablement"):
        _orig_env_enablement = adapter_mod._env_enablement

        def _wrapped_env_enablement() -> dict[str, Any] | None:
            seed = _orig_env_enablement()
            if seed is None:
                seed = {}
            allow_invalid_ssl = os.getenv("IRC_ALLOW_INVALID_SSL", "").strip().lower()
            if allow_invalid_ssl:
                seed["allow_invalid_ssl"] = allow_invalid_ssl in {"1", "true", "yes"}
            return seed

        adapter_mod._env_enablement = _wrapped_env_enablement
    else:
        logger.warning("hermes-irc-extras: skipping _env_enablement patch — function not found")

    # 6. Wrap interactive_setup to prompt during `hermes gateway setup`
    if not hasattr(adapter_mod, "interactive_setup"):
        logger.warning("hermes-irc-extras: skipping interactive_setup patch — function not found")
        return

    _orig_setup = adapter_mod.interactive_setup

    def _wrapped_setup() -> None:
        _orig_setup()
        try:
            from hermes_cli.setup_helpers import (
                get_env_value,
                print_info,
                print_warning,
                prompt_yes_no,
                save_env_value,
            )

            use_tls = (get_env_value("IRC_USE_TLS") or "").lower() in {"1", "true", "yes"}
            if use_tls:
                print_info("   Self-signed certificate? (private ZNC / InspIRCd / ergo)")
                allow_invalid_ssl = prompt_yes_no(
                    "Accept invalid/self-signed TLS certificates?",
                    (get_env_value("IRC_ALLOW_INVALID_SSL") or "").lower() in {"1", "true", "yes"},
                )
                save_env_value("IRC_ALLOW_INVALID_SSL", "true" if allow_invalid_ssl else "false")
                if allow_invalid_ssl:
                    print_warning(
                        "⚠️  Certificate verification disabled — the connection is "
                        "encrypted but not authenticated. Use only with servers you control."
                    )
            else:
                save_env_value("IRC_ALLOW_INVALID_SSL", "false")
        except Exception:
            logger.debug("hermes-irc-extras: interactive setup prompt failed", exc_info=True)

    adapter_mod.interactive_setup = _wrapped_setup


# ---------------------------------------------------------------------------
# Passive channel logging (opt-in via IRC_ENABLE_CHANNEL_LOGGING)
# ---------------------------------------------------------------------------

def _init_channel_logging(adapter: Any, extra: dict[str, Any]) -> None:
    """Resolve and stash this adapter's channel-logging settings.

    Resolution happens once per adapter (env first, then ``config.yaml``) so
    the per-line hot path is a single attribute read. The resolved values are
    then published to the shared plugin state and, if the tools were not
    registered at load time, registered now — the adapter's config is the one
    authoritative view of a YAML-only setup, and without this the gateway
    would write a log the agent has no tool to read. Failure here leaves
    logging off rather than breaking adapter construction.
    """
    adapter._irc_extras_logging = False
    adapter._irc_extras_db_path = None
    adapter._irc_extras_retention_days = 0.0
    adapter._irc_extras_last_prune = 0.0
    try:
        from . import storage

        if not storage.logging_enabled(extra):
            storage.reset_shared_config()
            return
        adapter._irc_extras_logging = True
        adapter._irc_extras_db_path = storage.resolve_db_path(extra)
        adapter._irc_extras_retention_days = storage.resolve_retention_days(extra)
        storage.set_shared_config(
            {
                "enable_channel_logging": True,
                "channel_log_db_path": str(adapter._irc_extras_db_path),
                "channel_log_retention_days": adapter._irc_extras_retention_days,
            }
        )
        _register_log_tools(extra)
        logger.info(
            "hermes-irc-extras: IRC channel logging enabled (db=%s, retention=%sd)",
            adapter._irc_extras_db_path,
            adapter._irc_extras_retention_days or "unlimited",
        )
    except Exception:
        adapter._irc_extras_logging = False
        logger.warning(
            "hermes-irc-extras: could not initialize channel logging; it stays disabled",
            exc_info=True,
        )


def _register_log_tools(extra: dict[str, Any]) -> None:
    """Register the query tools for an adapter that enabled logging in YAML.

    A no-op when the plugin loader already registered them (the env-var case)
    or when the host gave us no context to register with.
    """
    global _log_tools_registered
    if _log_tools_registered or _plugin_ctx is None:
        return
    try:
        from .tools import register_tools

        _log_tools_registered = register_tools(_plugin_ctx, extra)
    except Exception:
        logger.warning(
            "hermes-irc-extras: could not register IRC channel log tools", exc_info=True
        )


def _patch_handle_line(adapter_mod: Any) -> None:
    """Tap ``IRCAdapter._handle_line`` to record PRIVMSGs before the gates.

    The tap is installed unconditionally but is inert unless the adapter
    resolved logging as enabled, so a default install performs no disk writes.
    Installing it always (rather than only when the env var is set at patch
    time) is what lets ``config.yaml`` enable the feature too — the patches run
    before any adapter, and therefore any config, exists.
    """
    if not hasattr(adapter_mod.IRCAdapter, "_handle_line"):
        logger.warning(
            "hermes-irc-extras: skipping channel logging — IRCAdapter has no _handle_line"
        )
        return

    _orig_handle_line = adapter_mod.IRCAdapter._handle_line
    if getattr(_orig_handle_line, _WRAPPED_ATTR, False):
        return

    async def _wrapped_handle_line(self, raw: str) -> None:
        if getattr(self, "_irc_extras_logging", False):
            try:
                await _log_line(self, adapter_mod, raw)
            except Exception:
                # Logging is strictly best-effort: never break the receive loop.
                logger.debug("hermes-irc-extras: channel log write failed", exc_info=True)
        # The original keeps its own early-returns for unaddressed and
        # unauthorized messages, so those still cost zero agent turns.
        return await _orig_handle_line(self, raw)

    setattr(_wrapped_handle_line, _WRAPPED_ATTR, True)
    adapter_mod.IRCAdapter._handle_line = _wrapped_handle_line


def _extract_privmsg(adapter: Any, adapter_mod: Any, raw: str) -> dict[str, Any] | None:
    """Turn a raw IRC line into a log record, or None if it is not loggable.

    Mirrors the adapter's own PRIVMSG handling (own-message and CTCP skips,
    ``/me`` rendering, channel-vs-DM routing, addressing detection) so the
    stored ``is_addressed`` flag matches the dispatch decision the original
    method is about to make.
    """
    parse = getattr(adapter_mod, "_parse_irc_message", None)
    extract_nick = getattr(adapter_mod, "_extract_nick", None)
    if parse is None or extract_nick is None:
        return None

    msg = parse(raw)
    if msg.get("command") != "PRIVMSG":
        return None
    params = msg.get("params") or []
    if len(params) < 2:
        return None

    prefix = msg.get("prefix") or ""
    sender_nick = extract_nick(prefix)
    if not sender_nick:
        return None

    own_nick = getattr(adapter, "_current_nick", "") or getattr(adapter, "nickname", "")
    if own_nick and sender_nick.lower() == own_nick.lower():
        return None  # our own echo

    target = params[0]
    text = params[1]

    # CTCP ACTION (/me) reads as narration; every other CTCP is protocol noise.
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = f"* {sender_nick} {text[8:-1]}"
    elif text.startswith("\x01"):
        return None

    is_channel = target.startswith("#") or target.startswith("&")
    if is_channel:
        addressed = any(
            text.lower().startswith(candidate.lower())
            for candidate in (f"{own_nick}:", f"{own_nick},", f"{own_nick} ")
            if own_nick
        )
    else:
        # A DM is addressed to us by definition.
        addressed = True

    strip = getattr(adapter_mod, "_strip_irc_control_chars", None)
    if callable(strip):
        try:
            text = strip(text)
        except Exception:
            pass

    return {
        "server": str(getattr(adapter, "server", "") or ""),
        # DMs are filed under the sender's nick, matching the adapter's chat_id.
        "channel": target if is_channel else sender_nick,
        "nick": sender_nick,
        "userhost": prefix.split("!", 1)[1] if "!" in prefix else "",
        "message": text,
        "is_addressed": addressed,
    }


async def _log_line(adapter: Any, adapter_mod: Any, raw: str) -> None:
    """Write one line to the channel log off the event loop thread."""
    db_path = getattr(adapter, "_irc_extras_db_path", None)
    if db_path is None:
        return

    record = _extract_privmsg(adapter, adapter_mod, raw)
    if record is None:
        return

    from . import storage

    # sqlite3 is blocking; keep the IRC receive loop responsive.
    await asyncio.to_thread(storage.log_message, db_path, **record)
    await _maybe_prune(adapter, db_path)


async def _maybe_prune(adapter: Any, db_path: Any) -> None:
    """Enforce retention at most once an hour, piggybacking on ingestion."""
    retention_days = getattr(adapter, "_irc_extras_retention_days", 0.0) or 0.0
    if retention_days <= 0:
        return

    now = time.monotonic()
    last = getattr(adapter, "_irc_extras_last_prune", 0.0)
    if last and (now - last) < _PRUNE_INTERVAL_SECONDS:
        return
    adapter._irc_extras_last_prune = now

    from . import storage

    removed = await asyncio.to_thread(storage.prune_old_records, db_path, retention_days)
    if removed:
        logger.info(
            "hermes-irc-extras: pruned %d IRC log record(s) older than %s day(s)",
            removed, retention_days,
        )
