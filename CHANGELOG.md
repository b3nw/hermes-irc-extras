# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-14

### Added
- Initial release of the `hermes-irc-extras` standalone plugin.
- Added `IRC_ALLOW_INVALID_SSL` togglable option to allow connecting to IRC servers using self-signed or invalid TLS certificates (vulnerable to MITM, secure-by-default).
- Added dynamic dashboard exposure supporting true/false checkbox options on the Hermes Desktop and Web UI automatically.
- Added support for defensive parsing of quoted/string-based boolean values in `config.yaml`.
- Comprehensive test suite for verification.
