"""Monkeypatches to inject SSL verification bypass logic into the core IRC adapter."""

from __future__ import annotations

import logging
import os
import ssl
import sys
from typing import Any

logger = logging.getLogger(__name__)

_patched = False
_warned = False


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


def _inject_metadata_declarations() -> None:
    """Inject the new environment variable metadata into OPTIONAL_ENV_VARS dynamically.

    This ensures that the WebUI and Desktop UI can render the checkmark option
    on the Channels page for IRC.
    """
    try:
        from hermes_cli.config_defaults import OPTIONAL_ENV_VARS

        if "IRC_ALLOW_INVALID_SSL" not in OPTIONAL_ENV_VARS:
            OPTIONAL_ENV_VARS["IRC_ALLOW_INVALID_SSL"] = {
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
            }
    except Exception:
        logger.debug("Could not inject optional env var metadata", exc_info=True)


def _apply_irc_patches() -> None:
    """Surgically apply patches to the plugins.platforms.irc.adapter module."""
    global _patched
    if _patched:
        return

    try:
        # Import the target module
        import plugins.platforms.irc.adapter as adapter_mod
    except Exception:
        logger.warning(
            "hermes-irc-extras: Could not import plugins.platforms.irc.adapter", exc_info=True
        )
        return

    logger.debug("hermes-irc-extras: Applying lazy patches to IRC adapter")

    try:
        _patch_adapter_module(adapter_mod)
    except Exception:
        logger.warning(
            "hermes-irc-extras: Failed to apply IRC adapter patches; "
            "the plugin is inactive for this process",
            exc_info=True,
        )
        return

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

    # 4. Wrap IRCAdapter.__init__ to store self.allow_invalid_ssl
    if hasattr(adapter_mod, "IRCAdapter"):
        _orig_init = adapter_mod.IRCAdapter.__init__

        def _wrapped_init(self, config: Any, **kwargs: Any) -> None:
            _orig_init(self, config, **kwargs)
            extra = getattr(config, "extra", {}) or {}
            self.allow_invalid_ssl = _new_allow_invalid_ssl(extra)

        adapter_mod.IRCAdapter.__init__ = _wrapped_init
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
