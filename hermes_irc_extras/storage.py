"""SQLite storage for the opt-in passive IRC channel log.

Nothing in this module runs unless channel logging is explicitly enabled
(``IRC_ENABLE_CHANNEL_LOGGING``), so a default install never touches the disk.

The store is deliberately dependency-free (stdlib ``sqlite3`` only) and is
opened per call: the gateway writes from an asyncio worker thread while the
agent's tool handlers read from a different thread, and a fresh connection per
operation avoids sharing a handle across threads. WAL plus a 5s
``busy_timeout`` makes those concurrent readers/writers cheap and safe.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: ``busy_timeout`` applied to every connection, in milliseconds.
BUSY_TIMEOUT_MS = 5000

#: Days of scrollback kept by :func:`prune_old_records` unless overridden.
DEFAULT_RETENTION_DAYS = 14

DEFAULT_RESULT_LIMIT = 50
MAX_RESULT_LIMIT = 500

#: Single stored messages are truncated to this many characters. IRC lines are
#: capped at 512 bytes by the protocol, but bouncer playback and relay bots can
#: emit far longer pseudo-lines; the cap keeps one hostile peer from bloating
#: the log.
MAX_MESSAGE_CHARS = 4000

_FTS_TABLE = "irc_messages_fts"

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    # ``channel``/``nick`` are COLLATE NOCASE because IRC treats both as
    # case-insensitive; the collation keeps equality lookups both correct and
    # index-friendly (a LOWER() call in the WHERE clause would not be).
    """
    CREATE TABLE IF NOT EXISTS irc_messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp    REAL    NOT NULL,
        server       TEXT    NOT NULL DEFAULT '',
        channel      TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
        nick         TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
        userhost     TEXT    NOT NULL DEFAULT '',
        message      TEXT    NOT NULL DEFAULT '',
        is_addressed INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_irc_messages_channel_ts "
    "ON irc_messages(channel, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_irc_messages_nick_ts "
    "ON irc_messages(nick, timestamp)",
)

# FTS5 is compiled into most SQLite builds but not all; every statement here is
# applied best-effort and search falls back to LIKE when the table is absent.
_FTS_STATEMENTS: tuple[str, ...] = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5("
    f"    message, content='irc_messages', content_rowid='id'"
    f")",
    f"""
    CREATE TRIGGER IF NOT EXISTS irc_messages_fts_ai AFTER INSERT ON irc_messages BEGIN
        INSERT INTO {_FTS_TABLE}(rowid, message) VALUES (new.id, new.message);
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS irc_messages_fts_ad AFTER DELETE ON irc_messages BEGIN
        INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, message)
        VALUES ('delete', old.id, old.message);
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS irc_messages_fts_au AFTER UPDATE ON irc_messages BEGIN
        INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, message)
        VALUES ('delete', old.id, old.message);
        INSERT INTO {_FTS_TABLE}(rowid, message) VALUES (new.id, new.message);
    END
    """,
)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

#: Where an ``irc`` platform block may live in ``config.yaml``, lowest
#: precedence first — the same three locations, in the same "later wins"
#: order, that the host's ``gateway/config_loader.merge_platform_sections``
#: merges.
_YAML_IRC_BLOCKS: tuple[tuple[str, ...], ...] = (
    ("gateway", "platforms", "irc"),
    ("platforms", "irc"),
    ("gateway", "irc"),
)

#: The adapter-resolved IRC ``extra`` block, published by the ingestion patch
#: (:func:`set_shared_config`) so callers that hold no ``PlatformConfig`` —
#: every agent tool handler — resolve the same settings ingestion writes with.
_shared_config: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Configuration resolution (shared by the ingestion patch and the agent tools)
# ---------------------------------------------------------------------------

def _as_bool(value: Any, default: bool = False) -> bool:
    """Parse the loose true/false spellings that reach us from .env / YAML."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def set_shared_config(extra: dict[str, Any] | None) -> None:
    """Publish the resolved IRC ``extra`` block for callers without a config.

    The agent's tool handlers are reached without a ``PlatformConfig`` in
    hand, so without this they would resolve from the environment only and a
    ``config.yaml``-only database path would stay invisible to them.
    """
    global _shared_config
    _shared_config = dict(extra) if extra else {}


def reset_shared_config() -> None:
    """Forget the published block (a re-configured adapter, or a test)."""
    global _shared_config
    _shared_config = None


def _dig(data: Any, keys: tuple[str, ...]) -> Any:
    """Walk nested mappings, returning None as soon as the path breaks."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _load_yaml_extra() -> dict[str, Any]:
    """Read ``gateway.platforms.irc.extra`` from the active profile's config.

    Read on demand rather than cached: tool calls are rare, and an operator
    who edits ``config.yaml`` should not be answered from a stale copy. Every
    failure mode (no file, no PyYAML, malformed YAML) yields ``{}``, so the
    caller falls back to its own default and logging stays off.
    """
    path = _hermes_home() / "config.yaml"
    try:
        import yaml

        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("hermes-irc-extras: could not read %s", path, exc_info=True)
        return {}

    merged: dict[str, Any] = {}
    for keys in _YAML_IRC_BLOCKS:
        block = _dig(data, keys)
        if isinstance(block, dict) and isinstance(block.get("extra"), dict):
            merged.update(block["extra"])
    return merged


def _effective_extra(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the ``extra`` mapping the environment falls back to.

    A caller that supplies one (the ingestion patch, holding the adapter's own
    ``PlatformConfig.extra``) is taken at its word. Callers that supply none
    get ``config.yaml`` on top of whatever the ingestion patch published,
    which is what makes YAML-only configuration work end to end: the tools are
    registered and they read the same database the gateway is writing.
    """
    if extra is not None:
        return extra
    return {**(_shared_config or {}), **_load_yaml_extra()}


def logging_enabled(extra: dict[str, Any] | None = None) -> bool:
    """Return True when channel logging is switched on.

    Environment wins over ``config.yaml``, matching every other IRC knob.
    Absent both, logging is OFF — the opt-in default the feature promises.
    """
    env = (os.getenv("IRC_ENABLE_CHANNEL_LOGGING") or "").strip()
    if env:
        return _as_bool(env, default=False)
    extra = _effective_extra(extra)
    return _as_bool(extra.get("enable_channel_logging"), default=False)


def default_db_path() -> Path:
    """Return ``{profile}/state/irc_channel_logs.db`` for the active profile."""
    return _hermes_home() / "state" / "irc_channel_logs.db"


def _hermes_home() -> Path:
    """Resolve the active Hermes home, tolerating a partially loaded host."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        val = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(val) if val else (Path.home() / ".hermes")


def resolve_db_path(extra: dict[str, Any] | None = None) -> Path:
    """Resolve the log database path from env, then config.yaml, then default."""
    env = (os.getenv("IRC_CHANNEL_LOG_DB_PATH") or "").strip()
    if env:
        return Path(env).expanduser()
    configured = _effective_extra(extra).get("channel_log_db_path")
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip()).expanduser()
    return default_db_path()


def resolve_retention_days(extra: dict[str, Any] | None = None) -> float:
    """Resolve the retention window in days (``<= 0`` disables pruning)."""
    raw: Any = (os.getenv("IRC_CHANNEL_LOG_RETENTION_DAYS") or "").strip()
    if not raw:
        raw = _effective_extra(extra).get("channel_log_retention_days")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return float(DEFAULT_RETENTION_DAYS)
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "hermes-irc-extras: invalid channel log retention %r; using %s days",
            raw, DEFAULT_RETENTION_DAYS,
        )
        return float(DEFAULT_RETENTION_DAYS)


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

@contextmanager
def _connect(
    db_path: str | os.PathLike[str], *, create: bool = True
) -> Iterator[sqlite3.Connection]:
    """Yield a schema-ready connection, committing on clean exit.

    With ``create=False`` a missing file yields ``None`` instead of creating an
    empty database — read paths must never bring the log into existence.
    """
    path = Path(db_path)
    created = False
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        created = _create_private(path)
    elif not path.exists():
        yield None  # type: ignore[misc]
        return

    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        _ensure_schema(conn)
        if created:
            _tighten_companions(path)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _create_private(path: Path) -> bool:
    """Create a missing log database with 0600; report whether we made it.

    The log holds other people's conversations, so its mode must not be left
    to the process umask (0644 on many hosts publishes it to every local
    account). Creating the file ourselves with ``O_EXCL`` closes the window in
    which sqlite would create it world-readable. Best-effort: a filesystem
    without POSIX modes simply keeps whatever it gives us.
    """
    try:
        os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        return True
    except FileExistsError:
        return False
    except OSError:
        logger.debug(
            "hermes-irc-extras: could not create %s with 0600 permissions", path, exc_info=True
        )
        return False


def _tighten_companions(path: Path) -> None:
    """Best-effort 0600 on the ``-wal``/``-shm`` sidecars next to a new database.

    SQLite copies the database file's mode onto the sidecars it creates, so
    this only matters on builds/filesystems where it does not; a missing
    sidecar (or a platform without chmod) is not an error.
    """
    for suffix in ("-wal", "-shm"):
        try:
            os.chmod(str(path) + suffix, 0o600)
        except OSError:
            pass


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the table, indexes, and (best-effort) the FTS5 mirror."""
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)
    try:
        for statement in _FTS_STATEMENTS:
            conn.execute(statement)
    except sqlite3.OperationalError as exc:
        # No FTS5 in this SQLite build — searches degrade to LIKE.
        logger.debug("hermes-irc-extras: FTS5 unavailable (%s); using LIKE search", exc)


def _fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_FTS_TABLE,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def log_message(
    db_path: str | os.PathLike[str],
    server: str = "",
    channel: str = "",
    nick: str = "",
    userhost: str = "",
    message: str = "",
    is_addressed: bool = False,
    timestamp: float | None = None,
) -> int | None:
    """Append one message to the log; returns its rowid (None on failure).

    Never raises: a broken or unwritable log must not take down the IRC
    receive loop.
    """
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO irc_messages "
                "(timestamp, server, channel, nick, userhost, message, is_addressed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    float(timestamp if timestamp is not None else time.time()),
                    str(server or ""),
                    str(channel or ""),
                    str(nick or ""),
                    str(userhost or ""),
                    str(message or "")[:MAX_MESSAGE_CHARS],
                    1 if is_addressed else 0,
                ),
            )
            return int(cur.lastrowid) if cur.lastrowid is not None else None
    except Exception:
        logger.warning("hermes-irc-extras: failed to log IRC message", exc_info=True)
        return None


def prune_old_records(
    db_path: str | os.PathLike[str],
    retention_days: float = DEFAULT_RETENTION_DAYS,
) -> int:
    """Delete records older than ``retention_days``; returns rows removed.

    A retention of ``0`` or less keeps everything (pruning disabled).
    """
    try:
        days = float(retention_days)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        return 0

    cutoff = time.time() - days * 86400.0
    try:
        with _connect(db_path, create=False) as conn:
            if conn is None:
                return 0
            cur = conn.execute("DELETE FROM irc_messages WHERE timestamp < ?", (cutoff,))
            return int(cur.rowcount or 0)
    except Exception:
        logger.warning("hermes-irc-extras: failed to prune IRC channel log", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_RESULT_LIMIT
    if value <= 0:
        return DEFAULT_RESULT_LIMIT
    return min(value, MAX_RESULT_LIMIT)


def _filters(
    channel: str | None = None,
    nick: str | None = None,
    hours: float | None = None,
) -> tuple[list[str], list[Any]]:
    """Build the shared WHERE fragments for history and search."""
    clauses: list[str] = []
    params: list[Any] = []
    if channel and str(channel).strip():
        clauses.append("m.channel = ?")
        params.append(str(channel).strip())
    if nick and str(nick).strip():
        clauses.append("m.nick = ?")
        params.append(str(nick).strip())
    if hours is not None:
        try:
            window = float(hours)
        except (TypeError, ValueError):
            window = 0.0
        if window > 0:
            clauses.append("m.timestamp >= ?")
            params.append(time.time() - window * 3600.0)
    return clauses, params


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "server": row["server"],
        "channel": row["channel"],
        "nick": row["nick"],
        "userhost": row["userhost"],
        "message": row["message"],
        "is_addressed": bool(row["is_addressed"]),
    }


def query_history(
    db_path: str | os.PathLike[str],
    channel: str | None = None,
    limit: int = DEFAULT_RESULT_LIMIT,
    hours: float | None = None,
    nick: str | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent matching messages in chronological order."""
    clauses, params = _filters(channel, nick, hours)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT m.* FROM irc_messages m {where} "  # noqa: S608 - fragments are literals
        "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?"
    )
    try:
        with _connect(db_path, create=False) as conn:
            if conn is None:
                return []
            rows = conn.execute(sql, [*params, _clamp_limit(limit)]).fetchall()
    except Exception:
        logger.warning("hermes-irc-extras: channel log history query failed", exc_info=True)
        return []
    # Newest-first for the LIMIT, then flipped so the agent reads a transcript.
    return [_row_to_dict(row) for row in reversed(rows)]


#: FTS5 spells its boolean operators in uppercase; anything else is a term.
_FTS_OPERATORS = frozenset({"AND", "OR", "NOT", "NEAR"})


def _fts_expressions(query: str) -> list[str]:
    """Candidate FTS5 MATCH expressions, most expressive first.

    The raw query is tried first so an agent can use real FTS5 operators
    (``OR``, ``NEAR``, ``"exact phrase"``). Unbalanced quotes or stray
    punctuation make FTS5 raise a syntax error, so a quoted-token rewrite is
    offered as a second chance before the caller drops to LIKE: terms are
    quoted into literal phrases, operators are preserved, and operators left
    dangling at either end are dropped so the rewrite stays well-formed.
    """
    candidates = [query]
    parts = [
        token if token in _FTS_OPERATORS else f'"{token}"'
        for token in re.findall(r"[\w']+", query)
    ]
    while parts and parts[0] in _FTS_OPERATORS:
        parts.pop(0)
    while parts and parts[-1] in _FTS_OPERATORS:
        parts.pop()
    if parts:
        rewritten = " ".join(parts)
        if rewritten != query:
            candidates.append(rewritten)
    return candidates


def search_messages(
    db_path: str | os.PathLike[str],
    query: str,
    channel: str | None = None,
    nick: str | None = None,
    hours: float | None = None,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """Keyword-search the log, newest first.

    Uses FTS5 when the build supports it, otherwise a LIKE scan. Malformed
    FTS5 syntax degrades instead of raising.
    """
    text = (query or "").strip()
    if not text:
        return []

    clamped = _clamp_limit(limit)
    clauses, params = _filters(channel, nick, hours)
    tail = (" AND " + " AND ".join(clauses) if clauses else "")

    try:
        with _connect(db_path, create=False) as conn:
            if conn is None:
                return []
            rows: list[sqlite3.Row] | None = None
            if _fts_available(conn):
                sql = (
                    f"SELECT m.* FROM irc_messages m "  # noqa: S608 - fragments are literals
                    f"JOIN {_FTS_TABLE} f ON f.rowid = m.id "
                    f"WHERE {_FTS_TABLE} MATCH ?{tail} "
                    "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?"
                )
                for expression in _fts_expressions(text):
                    try:
                        rows = conn.execute(sql, [expression, *params, clamped]).fetchall()
                        break
                    except sqlite3.OperationalError as exc:
                        logger.debug(
                            "hermes-irc-extras: FTS expression %r rejected (%s)", expression, exc
                        )
            if rows is None:
                like_sql = (
                    f"SELECT m.* FROM irc_messages m "  # noqa: S608 - fragments are literals
                    f"WHERE m.message LIKE ? ESCAPE '\\'{tail} "
                    "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?"
                )
                pattern = "%" + _escape_like(text) + "%"
                rows = conn.execute(like_sql, [pattern, *params, clamped]).fetchall()
    except Exception:
        logger.warning("hermes-irc-extras: channel log search failed", exc_info=True)
        return []

    return [_row_to_dict(row) for row in rows]


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so a query of ``100%`` is not a match-everything."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
