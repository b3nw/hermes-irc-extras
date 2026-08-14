# hermes-irc-extras

A plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/) that adds features and security options to the IRC gateway adapter.

The first feature implements an option to **Accept Invalid or Self-Signed TLS Certificates** (`IRC_ALLOW_INVALID_SSL`) for connections to private IRC bouncers (such as ZNC), InspIRCd, or ergo test networks.

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
