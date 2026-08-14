"""Unit and integration tests for hermes-irc-extras standalone plugin."""

from __future__ import annotations

import asyncio
import ssl

import pytest

# Import our plugin modules
import hermes_irc_extras.patches as patches_mod
from hermes_irc_extras import register


class _FakeIRCConnection:
    """Fake asyncio reader/writer representation."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.written_lines = []
        self._closed = False

    def write(self, data):
        self.written_lines.append(data)

    async def drain(self):
        await asyncio.sleep(0)

    async def readline(self):
        await asyncio.sleep(0.001)
        if self.responses:
            return self.responses.pop(0)
        return b""

    async def readuntil(self, separator=b"\n"):
        return await self.readline()

    async def read(self, n=-1):
        return await self.readline()

    def close(self):
        self._closed = True

    async def wait_closed(self):
        await asyncio.sleep(0)


class TestIRCAllowInvalidSSLStandalonePlugin:
    """Tests the patching and verification behavior of the standalone plugin."""

    _SSL_KEYS = (
        "IRC_SERVER",
        "IRC_PORT",
        "IRC_NICKNAME",
        "IRC_CHANNEL",
        "IRC_USE_TLS",
        "IRC_ALLOW_INVALID_SSL",
    )

    @pytest.fixture(autouse=True)
    def setup_patches_and_clean_env(self, monkeypatch):
        # 1. Clear any environment variables
        for key in self._SSL_KEYS:
            monkeypatch.delenv(key, raising=False)

        # 2. Reset the patched flag so we re-patch for testing
        patches_mod._patched = False

        # 3. Apply the patches dynamically
        register()
        patches_mod._apply_irc_patches()

        # Import patched module
        import plugins.platforms.irc.adapter as adapter_mod
        self.adapter_mod = adapter_mod

    def _tls_config(self, **extra):
        from gateway.config import PlatformConfig
        base = {
            "server": "irc.internal.test",
            "port": 6697,
            "nickname": "hermes",
            "channel": "#test",
            "use_tls": True,
        }
        base.update(extra)
        return PlatformConfig(enabled=True, extra=base)

    # ── flag resolution ──────────────────────────────────────────────

    def test_defaults_to_off(self):
        assert self.adapter_mod._allow_invalid_ssl({}) is False
        assert self.adapter_mod.IRCAdapter(self._tls_config()).allow_invalid_ssl is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", " true "])
    def test_env_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("IRC_ALLOW_INVALID_SSL", value)
        assert self.adapter_mod._allow_invalid_ssl({}) is True
        assert self.adapter_mod.IRCAdapter(self._tls_config()).allow_invalid_ssl is True

    def test_env_false_disables_even_when_extra_enables(self, monkeypatch):
        """Env wins over config.yaml, same as every other IRC knob."""
        monkeypatch.setenv("IRC_ALLOW_INVALID_SSL", "false")
        assert self.adapter_mod._allow_invalid_ssl({"allow_invalid_ssl": True}) is False
        adapter = self.adapter_mod.IRCAdapter(self._tls_config(allow_invalid_ssl=True))
        assert adapter.allow_invalid_ssl is False

    def test_read_from_extra_config(self):
        assert self.adapter_mod._allow_invalid_ssl({"allow_invalid_ssl": True}) is True
        adapter = self.adapter_mod.IRCAdapter(self._tls_config(allow_invalid_ssl=True))
        assert adapter.allow_invalid_ssl is True

    @pytest.mark.parametrize("val", ["true", "1", "yes", "TRUE", " true "])
    def test_read_from_extra_config_string_truthy(self, val):
        assert self.adapter_mod._allow_invalid_ssl({"allow_invalid_ssl": val}) is True
        adapter = self.adapter_mod.IRCAdapter(self._tls_config(allow_invalid_ssl=val))
        assert adapter.allow_invalid_ssl is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "FALSE", " false "])
    def test_read_from_extra_config_string_falsy(self, val):
        assert self.adapter_mod._allow_invalid_ssl({"allow_invalid_ssl": val}) is False
        adapter = self.adapter_mod.IRCAdapter(self._tls_config(allow_invalid_ssl=val))
        assert adapter.allow_invalid_ssl is False

    def test_blank_env_falls_through_to_extra(self, monkeypatch):
        """A cleared field in .env must not override config.yaml."""
        monkeypatch.setenv("IRC_ALLOW_INVALID_SSL", "")
        assert self.adapter_mod._allow_invalid_ssl({"allow_invalid_ssl": True}) is True

    # ── context construction ─────────────────────────────────────────

    def test_context_verifies_when_disabled(self):
        ctx = self.adapter_mod._build_ssl_context(False)
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_context_skips_verification_when_enabled(self):
        ctx = self.adapter_mod._build_ssl_context(True)
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    # ── blast radius: the global ssl module must stay untouched ──────

    def test_global_ssl_module_is_not_patched(self):
        """register() + _apply_irc_patches() must not mutate the stdlib ssl module."""
        original = ssl.create_default_context

        patches_mod._patched = False
        register()
        patches_mod._apply_irc_patches()

        assert ssl.create_default_context is original
        assert ssl.create_default_context.__module__ == "ssl"
        # ...while the adapter's own `ssl` name is shimmed and still resolves members.
        assert self.adapter_mod.ssl is not ssl
        assert self.adapter_mod.ssl.CERT_NONE is ssl.CERT_NONE
        assert self.adapter_mod.ssl.SSLContext is ssl.SSLContext

    def test_flag_does_not_leak_into_other_tls_consumers(self, monkeypatch):
        """IRC_ALLOW_INVALID_SSL must not weaken non-IRC TLS in the same process."""
        monkeypatch.setenv("IRC_ALLOW_INVALID_SSL", "true")

        ctx = ssl.create_default_context()
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED

        # The IRC adapter's shimmed name still honours the flag.
        irc_ctx = self.adapter_mod.ssl.create_default_context()
        assert irc_ctx.check_hostname is False
        assert irc_ctx.verify_mode == ssl.CERT_NONE

    # ── end-to-end through the two connect paths ─────────────────────

    @pytest.mark.asyncio
    async def test_connect_uses_unverified_context(self, monkeypatch):
        """adapter.connect() hands an unverified context to open_connection."""
        monkeypatch.setenv("IRC_ALLOW_INVALID_SSL", "true")

        # Keep the identity lock out of the picture (it touches real state).
        import gateway.status
        monkeypatch.setattr(gateway.status, "acquire_scoped_lock", lambda *a, **kw: (True, None))
        monkeypatch.setattr(gateway.status, "release_scoped_lock", lambda *a, **kw: None)

        captured = {}

        async def _fake_open(host, port, **kwargs):
            captured["ssl"] = kwargs.get("ssl")
            # Abort here: we only care about the context, not the handshake.
            raise OSError("connection refused")

        monkeypatch.setattr(self.adapter_mod.asyncio, "open_connection", _fake_open)

        adapter = self.adapter_mod.IRCAdapter(self._tls_config())
        assert await adapter.connect() is False

        ctx = captured["ssl"]
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    @pytest.mark.asyncio
    async def test_connect_verifies_by_default(self, monkeypatch):
        import gateway.status
        monkeypatch.setattr(gateway.status, "acquire_scoped_lock", lambda *a, **kw: (True, None))
        monkeypatch.setattr(gateway.status, "release_scoped_lock", lambda *a, **kw: None)

        captured = {}

        async def _fake_open(host, port, **kwargs):
            captured["ssl"] = kwargs.get("ssl")
            raise OSError("connection refused")

        monkeypatch.setattr(self.adapter_mod.asyncio, "open_connection", _fake_open)

        adapter = self.adapter_mod.IRCAdapter(self._tls_config())
        assert await adapter.connect() is False

        ctx = captured["ssl"]
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    @pytest.mark.asyncio
    async def test_standalone_send_uses_unverified_context(self, monkeypatch):
        """The out-of-process cron sender honours the flag too."""
        from gateway.config import PlatformConfig

        monkeypatch.setenv("IRC_SERVER", "irc.internal.test")
        monkeypatch.setenv("IRC_CHANNEL", "#cron")
        monkeypatch.setenv("IRC_NICKNAME", "hermesbot")
        monkeypatch.setenv("IRC_USE_TLS", "true")
        monkeypatch.setenv("IRC_ALLOW_INVALID_SSL", "true")

        conn = _FakeIRCConnection([b":server 001 hermesbot-cron :Welcome"])
        captured = {}

        async def _fake_open(host, port, **kwargs):
            captured["ssl"] = kwargs.get("ssl")
            return conn, conn

        monkeypatch.setattr(self.adapter_mod.asyncio, "open_connection", _fake_open)

        result = await self.adapter_mod._standalone_send(
            PlatformConfig(enabled=True, extra={}),
            "hermesuser",  # bare nick: no JOIN handshake to script
            "hello",
        )
        assert result.get("success") is True

        ctx = captured["ssl"]
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    @pytest.mark.asyncio
    async def test_standalone_send_skips_tls_entirely_without_use_tls(self, monkeypatch):
        """allow-invalid is irrelevant on a plaintext connection."""
        from gateway.config import PlatformConfig

        monkeypatch.setenv("IRC_SERVER", "irc.internal.test")
        monkeypatch.setenv("IRC_CHANNEL", "#cron")
        monkeypatch.setenv("IRC_NICKNAME", "hermesbot")
        monkeypatch.setenv("IRC_USE_TLS", "false")
        monkeypatch.setenv("IRC_ALLOW_INVALID_SSL", "true")

        conn = _FakeIRCConnection([b":server 001 hermesbot-cron :Welcome"])
        captured = {}

        async def _fake_open(host, port, **kwargs):
            captured["ssl"] = kwargs.get("ssl")
            return conn, conn

        monkeypatch.setattr(self.adapter_mod.asyncio, "open_connection", _fake_open)

        result = await self.adapter_mod._standalone_send(
            PlatformConfig(enabled=True, extra={}),
            "hermesuser",
            "hello",
        )
        assert result.get("success") is True
        assert captured["ssl"] is None


# ── Config surface (Desktop UI / WebUI exposure) ──────────────────────────


class TestIRCAllowInvalidSSLConfigSurfacePlugin:

    def test_listed_in_optional_env_vars(self):
        from hermes_cli.config_defaults import OPTIONAL_ENV_VARS

        info = OPTIONAL_ENV_VARS.get("IRC_ALLOW_INVALID_SSL")
        assert info is not None, "IRC_ALLOW_INVALID_SSL missing from OPTIONAL_ENV_VARS"
        assert info["description"]
        assert info["prompt"]
        assert info["password"] is False
        assert info["category"] == "messaging"
