"""hermes-irc-extras — Additional features and security extensions for the IRC relay.

A standalone, fork-free Hermes Agent plugin.
"""

from __future__ import annotations

import logging

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


def register(ctx=None) -> None:
    """Plugin entry point — called once at startup by the Hermes plugin loader.

    Safely installs the IRC extras monkeypatches onto the core adapter modules
    without modifying the core files on disk. If the host environment does not
    support these extensions, logs a warning and exits cleanly without throwing.

    When channel logging is enabled, also registers the log inspection tools so
    the agent can query scrollback on demand. With logging off (the default) no
    tools are registered at all.
    """
    from .patches import apply_patches

    try:
        active = apply_patches()
    except Exception:
        logger.exception("hermes-irc-extras: unexpected error during initialization")
        return

    try:
        from .tools import register_tools

        register_tools(ctx)
    except Exception:
        # A tool-registration failure must not cost the host the SSL patches.
        logger.exception("hermes-irc-extras: failed to register IRC channel log tools")

    if active:
        logger.info("hermes-irc-extras v%s active", __version__)
    else:
        logger.warning(
            "hermes-irc-extras v%s loaded but INACTIVE (host unavailable "
            "or unsupported adapter signature)", __version__
        )
