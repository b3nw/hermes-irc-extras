# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Opt-in passive channel logging (`IRC_ENABLE_CHANNEL_LOGGING`, default `false`): every IRC `PRIVMSG` is recorded to a local SQLite database ahead of the adapter's addressing and authorization gates, so unaddressed and unauthorized messages are logged without spending an agent turn or an LLM call.
- Added `search_irc_logs` and `get_channel_history` agent tools (registered only while logging is enabled) for FTS5 keyword search and chronological scrollback, with channel/nick/time-window filters.
- Tool results are framed in the host's `<untrusted_tool_result>` data boundary and the boundary token is defanged inside the payload, so logged third-party IRC text cannot be used as an indirect prompt-injection channel.
- Added `IRC_CHANNEL_LOG_DB_PATH` (default `{profile}/state/irc_channel_logs.db`) and `IRC_CHANNEL_LOG_RETENTION_DAYS` (default `14`, `0` to keep everything) with automatic retention pruning; all three settings are exposed to the Hermes Desktop and Web UI config surface.

## [0.1.0] - 2026-08-14

### Added
- Initial release of the `hermes-irc-extras` standalone plugin.
- Added `IRC_ALLOW_INVALID_SSL` togglable option to allow connecting to IRC servers using self-signed or invalid TLS certificates (vulnerable to MITM, secure-by-default).
- Added dynamic dashboard exposure supporting true/false checkbox options on the Hermes Desktop and Web UI automatically.
- Added support for defensive parsing of quoted/string-based boolean values in `config.yaml`.
- Comprehensive test suite for verification.
