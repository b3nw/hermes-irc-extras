# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-20

### Added
- Opt-in passive channel logging (`IRC_ENABLE_CHANNEL_LOGGING`, default `false`): every IRC `PRIVMSG` is recorded to a local SQLite database ahead of the adapter's addressing and authorization gates, so unaddressed and unauthorized messages are logged without spending an agent turn or an LLM call.
- Added `search_irc_logs` and `get_channel_history` agent tools (registered only while logging is enabled) for FTS5 keyword search and chronological scrollback, with channel/nick/time-window filters.
- Tool results are framed in the host's `<untrusted_tool_result>` data boundary and the boundary token is defanged inside the payload, so logged third-party IRC text cannot be used as an indirect prompt-injection channel.
- Added `IRC_CHANNEL_LOG_DB_PATH` (default `{profile}/state/irc_channel_logs.db`) and `IRC_CHANNEL_LOG_RETENTION_DAYS` (default `14`, `0` to keep everything) with automatic retention pruning; all three settings are exposed to the Hermes Desktop and Web UI config surface.
- Every setting can be configured in `config.yaml` alone (`gateway.platforms.irc.extra`) as well as in the environment: enabling logging there registers the query tools, and the tools read the database path the gateway is writing to. Precedence is environment, then `config.yaml`, then the adapter's resolved settings.
- New log databases are created with owner-only (`0600`) permissions instead of whatever the process umask allows.

### Privacy
- Channel logging records more than channel traffic: direct messages to the bot are logged too, and every record keeps the sender's nick and `user@host` (hostname or cloak) alongside the message text. The README, the tool descriptions and the Desktop/Web UI setting descriptions all disclose this; read them before enabling the feature.

## [0.1.0] - 2026-08-14

### Added
- Initial release of the `hermes-irc-extras` standalone plugin.
- Added `IRC_ALLOW_INVALID_SSL` togglable option to allow connecting to IRC servers using self-signed or invalid TLS certificates (vulnerable to MITM, secure-by-default).
- Added dynamic dashboard exposure supporting true/false checkbox options on the Hermes Desktop and Web UI automatically.
- Added support for defensive parsing of quoted/string-based boolean values in `config.yaml`.
- Comprehensive test suite for verification.
