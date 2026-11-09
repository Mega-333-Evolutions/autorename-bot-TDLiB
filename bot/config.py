"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple


class ConfigError(RuntimeError):
    pass


DEFAULT_POST_BOT_ID = 8950016484  # the channel post bot: the only account whose answers in the communication chat count


def _parse_ids(value: Optional[str]) -> Tuple[int, ...]:
    ids = []
    for chunk in (value or "").replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"Invalid id {chunk!r} in ADMIN_IDS") from exc
    return tuple(ids)


def _get_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_any(env: Mapping[str, str], name: str) -> str:
    """A variable by name whatever its capital letters (``DB_Channel``, ``DB_CHANNEL`` and ``db_channel`` are one setting)."""
    raw = env.get(name)
    if raw is None:
        wanted = name.lower()
        raw = next((v for k, v in env.items() if k.lower() == wanted), "")
    return (raw or "").strip()


def _get_chat(env: Mapping[str, str], name: str) -> Optional[int]:
    """A chat id such as ``-1001234567890`` (unset or empty: ``None``; a bare channel id gets its ``-100`` in front)."""
    raw = _env_any(env, name)
    if not raw or raw.lower() in {"0", "off", "none", "false", "no"}:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a numeric chat id like -1001234567890, got {raw!r}") from exc
    return -1_000_000_000_000 - value if value > 0 else value


def _get_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    admin_ids: Tuple[int, ...]
    database_url: str
    log_channel: Optional[int]
    port: int
    temp_dir: str
    max_jobs: int
    max_file_mb: int
    progress_interval: float
    stream_transfer: bool = False  # EXPERIMENTAL: a big file is uploaded while it is still downloading (see README)
    tdlib_dir: str = "/tmp/tdlib"  # where TDLib keeps its files and its small database
    bot_dc: Optional[int] = None  # the data center the bot's account is on (1-5), if known: shown by /dc and /status
    td_log_level: int = 1  # how much TDLib writes to the log: 0 only fatal errors, 1 errors, 2 warnings, 3 and more are noisy
    db_channel: Optional[int] = None  # the Database Channel a manually renamed episode can be uploaded to
    comm_chat: Optional[int] = None  # the chat where this bot and the channel post bot talk to each other
    post_bot_id: int = DEFAULT_POST_BOT_ID

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def upload_enabled(self) -> bool:
        """Uploading to the Database Channel (and having the post published) needs both chats."""
        return bool(self.db_channel and self.comm_chat)


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = os.environ if env is None else env

    missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN", "OWNER_ID") if not env.get(k, "").strip()]
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))

    log_channel_raw = env.get("LOG_CHANNEL", "").strip()
    log_channel = None
    if log_channel_raw:
        try:
            log_channel = int(log_channel_raw)
        except ValueError as exc:
            raise ConfigError("LOG_CHANNEL must be a numeric chat id like -1001234567890") from exc

    db_channel = _get_chat(env, "DB_CHANNEL")
    comm_chat = _get_chat(env, "COMMUNICATION_CHAT")
    post_bot_raw = _env_any(env, "CHANNEL_POST_BOT_ID")
    try:
        post_bot_id = int(post_bot_raw) if post_bot_raw else DEFAULT_POST_BOT_ID
    except ValueError as exc:
        raise ConfigError(f"CHANNEL_POST_BOT_ID must be a numeric user id, got {post_bot_raw!r}") from exc

    bot_dc = _get_int(env, "BOT_DC", 0)
    if bot_dc not in range(0, 6):
        raise ConfigError(f"BOT_DC must be a data center number from 1 to 5, got {bot_dc}")
    max_jobs = max(1, min(_get_int(env, "MAX_CONCURRENT_JOBS", 1), 4))

    return Config(
        api_id=_get_int(env, "API_ID", 0),
        api_hash=env["API_HASH"].strip(),
        bot_token=env["BOT_TOKEN"].strip(),
        owner_id=_get_int(env, "OWNER_ID", 0),
        admin_ids=_parse_ids(env.get("ADMIN_IDS")),
        database_url=env.get("DATABASE_URL", "").strip() or "sqlite+aiosqlite:///bot.db",
        log_channel=log_channel,
        port=_get_int(env, "PORT", 7860),
        temp_dir=env.get("TEMP_DIR", "").strip() or "/tmp/rename",
        max_jobs=max_jobs,
        max_file_mb=max(1, _get_int(env, "MAX_FILE_MB", 2000)),
        progress_interval=max(2.0, float(env.get("PROGRESS_INTERVAL", "4") or 4)),
        stream_transfer=_get_bool(env, "STREAM_RENAME", False),
        tdlib_dir=env.get("TDLIB_DIR", "").strip() or "/tmp/tdlib",
        bot_dc=bot_dc or None,
        td_log_level=max(0, min(_get_int(env, "TDLIB_LOG_LEVEL", 1), 5)),
        db_channel=db_channel,
        comm_chat=comm_chat,
        post_bot_id=post_bot_id,
    )
