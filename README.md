# hermes-irc-extras

A plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/) that adds features and security options to the IRC gateway adapter.

The first feature implements an option to **Accept Invalid or Self-Signed TLS Certificates** (`IRC_ALLOW_INVALID_SSL`) for connections to private IRC bouncers (such as ZNC), InspIRCd, or ergo test networks.

The second adds opt-in **passive channel logging** (`IRC_ENABLE_CHANNEL_LOGGING`) so the agent can search channel scrollback without spending a turn on every message it sees.

## Features & Configuration

- **Precedence-Aware:** Environment variables (`IRC_ALLOW_INVALID_SSL`) override `config.yaml` (`allow_invalid_ssl`) parameters.
- **Defensive Boolean Parsing:** Unquoted or quoted string values in configuration (e.g. `"true"`, `"yes"`, `"1"`, `"false"`, `"0"`, `"no"`) are defensively parsed.
- **Dynamic UI Integration:** Automatic integration with Hermes WebUI and Desktop UI configuration cards.

### Setup

Configure the option in your `.env` or `config.yaml`:

#### Environment Variables (`.env`)
```bash
IRC_ALLOW_INVALID_SSL=true
```

#### Configuration File (`config.yaml`)
```yaml
gateway:
  platforms:
    irc:
      enabled: true
      server: "irc.mybouncer.internal"
      port: 6697
      use_tls: true
      allow_invalid_ssl: true
```

## Passive Channel Logging (opt-in, default OFF)

Lets the agent monitor channels and query scrollback **without spending LLM turns on
messages nobody addressed to it**. Every `PRIVMSG` is written to a local SQLite database
before the adapter's own addressing/authorization gates run; those gates are unchanged, so
unaddressed traffic and traffic from users outside `IRC_ALLOWED_USERS` is recorded and then
dropped — zero agent turns, zero API cost, no reply to the channel.

When enabled, two read-only tools are registered for the agent:

- `search_irc_logs(query, channel?, nick?, hours?, limit?)` — FTS5 keyword/phrase search.
- `get_channel_history(channel, nick?, hours?, limit?)` — recent chronological scrollback.

```bash
IRC_ENABLE_CHANNEL_LOGGING=true          # default: false
IRC_CHANNEL_LOG_DB_PATH=                 # default: {profile}/state/irc_channel_logs.db
IRC_CHANNEL_LOG_RETENTION_DAYS=14        # 0 keeps everything; pruning is automatic
```

```yaml
gateway:
  platforms:
    irc:
      extra:
        enable_channel_logging: true
        channel_log_db_path: null
        channel_log_retention_days: 14
```

⚠️ **This records other people's conversations.** Enabling it logs every message in the
channels the bot sits in, including from users who cannot instruct the agent at all. Check
local expectations and any network policy before turning it on. Nothing is written while
the flag is off — the database is not even created.

⚠️ **Log contents are untrusted input.** Tool results are framed in the host's
`<untrusted_tool_result>` data boundary (with the boundary token defanged inside the
payload) so a passer-by in a logged channel cannot use the log as an indirect
prompt-injection channel into the agent.

## Installation

```bash
# Clone the repository
git clone https://github.com/b3nw/hermes-irc-extras.git ~/.hermes/plugins/hermes-irc-extras

# Install in editable mode
pip install -e ~/.hermes/plugins/hermes-irc-extras
```

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/test_irc_extras.py
```

## Security Warning

⚠️ **Disabling TLS verification leaves the connection vulnerable to Man-in-the-Middle (MITM) attacks.** The traffic remains encrypted but is no longer authenticated. Enable this option only with servers and networks you control.

## License

MIT License — see [LICENSE](LICENSE).
