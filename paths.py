from __future__ import annotations  # drift:ignore[AVS] reason:Canonical filesystem map is intentionally stable and shared.

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

DATA_DIR = ROOT_DIR / "data"
RUNTIME_DIR = ROOT_DIR / "runtime"

DB_DIR = DATA_DIR / "db"
USERS_DB = DB_DIR / "users.db"
QUICK_PUNISH_DB = DB_DIR / "quick_punish.db"
TAGGER_DB = DB_DIR / "tagger.db"
REVIEWER_DB = DB_DIR / "unanswered.db"
TIMED_ROLE_DB = DB_DIR / "timed_role_members.db"

API_TABLE_DIR = DATA_DIR / "api_table"
API_TABLE_GOOD_FILE = API_TABLE_DIR / "good.json"
API_TABLE_BAD_FILE = API_TABLE_DIR / "bad.json"
API_TABLE_PROMPT_FILE = API_TABLE_DIR / "prompt.txt"

BROADCAST_DIR = DATA_DIR / "broadcast"
BROADCAST_THREADS_FILE = BROADCAST_DIR / "broadcast_threads.json"
BROADCAST_STATS_FILE = BROADCAST_DIR / "broadcast_stats.json"

MENTION_DIR = DATA_DIR / "mention"
MENTION_SETTINGS_FILE = MENTION_DIR / "settings.json"
MENTION_THREADS_FILE = MENTION_DIR / "threads.json"
MENTION_USAGE_STATS_FILE = MENTION_DIR / "usage_stats.json"
MENTION_KB_DIR = MENTION_DIR / "kb"
MENTION_THREAD_METADATA_DIR = MENTION_DIR / "threadsMetadata"

ROLE_CONFIGURE_DIR = DATA_DIR / "role_configure"
ROLE_CONFIGURE_AVAILABLE_CHANNEL_FILE = ROLE_CONFIGURE_DIR / "available_channel.json"
ROLE_CONFIGURE_PANELS_FILE = ROLE_CONFIGURE_DIR / "panels.json"

PROMPT_DIR = DATA_DIR / "prompt"
SUMMARY_PROMPT_DIR = DATA_DIR / "summary_prompt"
XIAOZUOWEN_DIR = DATA_DIR / "xiaozuowen"

CONFIG_DIR = DATA_DIR / "config"
ROLE_SYNC_CONFIG_FILE = CONFIG_DIR / "role_sync_config.json"
QUICK_PUNISH_SYNC_CONFIG_FILE = CONFIG_DIR / "quick_punish_sync.json"

TEMP_DIR = RUNTIME_DIR / "temp"
APP_TEMP_DIR = TEMP_DIR / "app"
CONTEXT_TEMP_DIR = TEMP_DIR / "context"
MENTION_TEMP_DIR = TEMP_DIR / "mention"

SAVE_DIR = RUNTIME_DIR / "save"
LOGS_DIR = RUNTIME_DIR / "logs"
COMMAND_LOG_FILE = LOGS_DIR / "log.txt"
GC_LOG_FILE = LOGS_DIR / "gc.log"
RAG_DATA_DIR = RUNTIME_DIR / "rag_data"
API_TABLE_HISTORY_FILE = RUNTIME_DIR / "api_table_history.txt"
PROMPT_LOG_DIR = RUNTIME_DIR / "prompt_log"

RAG_PROMPT_DIR = ROOT_DIR / "rag_prompt"
UPLOADED_PROMPT_DIR = ROOT_DIR / "uploaded_prompt"

PROJECT_DIRS = (
    DATA_DIR,
    RUNTIME_DIR,
    DB_DIR,
    API_TABLE_DIR,
    BROADCAST_DIR,
    MENTION_DIR,
    MENTION_KB_DIR,
    MENTION_THREAD_METADATA_DIR,
    ROLE_CONFIGURE_DIR,
    PROMPT_DIR,
    SUMMARY_PROMPT_DIR,
    XIAOZUOWEN_DIR,
    CONFIG_DIR,
    TEMP_DIR,
    APP_TEMP_DIR,
    CONTEXT_TEMP_DIR,
    MENTION_TEMP_DIR,
    SAVE_DIR,
    LOGS_DIR,
    RAG_DATA_DIR,
    PROMPT_LOG_DIR,
)

LEGACY_PATHS = (
    ROOT_DIR / "users.db",
    ROOT_DIR / "quick_punish.db",
    ROOT_DIR / "api_table" / "good.json",
    ROOT_DIR / "api_table" / "bad.json",
    ROOT_DIR / "api_table" / "prompt.txt",
    ROOT_DIR / "api_table" / "history.txt",
    ROOT_DIR / "broadcast" / "broadcast_threads.json",
    ROOT_DIR / "broadcast" / "broadcast_stats.json",
    ROOT_DIR / "mention" / "settings.json",
    ROOT_DIR / "mention" / "threads.json",
    ROOT_DIR / "mention" / "usage_stats.json",
    ROOT_DIR / "mention" / "kb",
    ROOT_DIR / "mention" / "promptLog",
    ROOT_DIR / "mention" / "threadsMetadata",
    ROOT_DIR / "reviewer" / "unanswered.db",
    ROOT_DIR / "tagger" / "tagger.db",
    ROOT_DIR / "prompt",
    ROOT_DIR / "summary_prompt",
    ROOT_DIR / "xiaozuowen",
    ROOT_DIR / "role_configure" / "available_channel.json",
    ROOT_DIR / "role_configure" / "panels.json",
    ROOT_DIR / "role_configure" / "timed_role_members.db",
    ROOT_DIR / "cogs" / "config" / "role_sync_config.json",
    ROOT_DIR / "cogs" / "config" / "quick_punish_sync.json",
    ROOT_DIR / "app_temp",
    ROOT_DIR / "context_temp",
    ROOT_DIR / "mention_temp",
    ROOT_DIR / "app_save",
    ROOT_DIR / "logs",
    ROOT_DIR / "rag_data",
)


def ensure_project_dirs() -> None:
    for directory in PROJECT_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def find_legacy_paths() -> list[Path]:
    return [path for path in LEGACY_PATHS if path.exists()]


ensure_project_dirs()
