"""SQLite 数据层（架构评审决策 2）。

单文件 WAL 库，标准库 sqlite3，零新依赖。解决三件事：
- 会议/分段/覆盖区间/断网洞 持久化 → server 重启不丢状态（录制中分段逐条落库）；
- list_meetings 从 O(n×全文件JSON) 变成索引查询；
- 写入原子性（事务取代非原子的 JSON 整文件覆写）。

服务端是单进程单事件循环，所有 DB 调用同步执行（单次操作亚毫秒级）；
用一把锁保护连接以防线程池路径（FastAPI def endpoint）并发进入。
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    active INTEGER NOT NULL DEFAULT 1,
    token_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meetings (
    session_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL DEFAULT 'TEST1',
    created_at TEXT NOT NULL,
    title TEXT,
    state TEXT NOT NULL DEFAULT 'recording',   -- recording|suspended|finalizing|done
    summary_json TEXT,                          -- NULL = 纪要未生成（summary_pending）
    language TEXT DEFAULT 'auto',
    summary_language TEXT DEFAULT 'auto',
    speakers_json TEXT,                         -- 会议级说话人摘要（延迟定稿/恢复时用）
    audio_end_ms INTEGER,                       -- final=1 时的音频总时长
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    session_id TEXT NOT NULL,
    seg_id TEXT NOT NULL,
    ord INTEGER NOT NULL DEFAULT 0,             -- 插入序，start_ms 相同/缺失时保持稳定顺序
    start_ms INTEGER NOT NULL DEFAULT 0,
    end_ms INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    speaker_id TEXT,
    speaker_label TEXT,
    speaker_final INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live',
    state TEXT NOT NULL DEFAULT 'provisional',
    revision INTEGER NOT NULL DEFAULT 1,
    caption_revision INTEGER NOT NULL DEFAULT 0,
    speaker_revision INTEGER NOT NULL DEFAULT 0,
    translation_revision INTEGER NOT NULL DEFAULT 0,
    translation TEXT,                            -- 实时翻译译文（v2 翻译管道落库）
    PRIMARY KEY (session_id, seg_id)
);
CREATE INDEX IF NOT EXISTS idx_segments_time ON segments(session_id, start_ms, ord);
CREATE TABLE IF NOT EXISTS coverage (
    session_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coverage_sid ON coverage(session_id);
CREATE TABLE IF NOT EXISTS pending_gaps (
    session_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    PRIMARY KEY (session_id, start_ms)
);
CREATE TABLE IF NOT EXISTS agenda_events (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT 'default',
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT,
    recurrence_rule TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    linked_meeting_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agenda_events_owner_start ON agenda_events(owner, start_at);
CREATE TABLE IF NOT EXISTS agenda_reminders (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    remind_at TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'screen',
    fired_at TEXT,
    FOREIGN KEY(event_id) REFERENCES agenda_events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agenda_reminders_at ON agenda_reminders(remind_at, fired_at);
CREATE TABLE IF NOT EXISTS agenda_todos (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT 'default',
    text TEXT NOT NULL,
    due_at TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    source_event_id TEXT,
    assignee TEXT NOT NULL DEFAULT '我',
    priority TEXT NOT NULL DEFAULT 'normal',
    remind_mode TEXT NOT NULL DEFAULT 'none',
    remind_at TEXT,
    note TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(source_event_id) REFERENCES agenda_events(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS agenda_voice_captures (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    mark_ts INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    event_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, mark_ts)
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    owner_user_id TEXT,
    display_name TEXT NOT NULL DEFAULT '落叶录音笔',
    binding_generation INTEGER NOT NULL DEFAULT 0,
    firmware_version TEXT,
    hardware_revision TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    protocol_version TEXT,
    battery_percent INTEGER,
    speaker_diarization_enabled INTEGER NOT NULL DEFAULT 1,
    config_revision INTEGER NOT NULL DEFAULT 1,
    bound_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_user_id, bound_at DESC);
CREATE TABLE IF NOT EXISTS device_pairings (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    code_digest TEXT NOT NULL,
    nonce_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    claimed_user_id TEXT,
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    token_delivered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(device_id, nonce_digest),
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE,
    FOREIGN KEY(claimed_user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pairings_active_code
    ON device_pairings(code_digest) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_pairings_device ON device_pairings(device_id, created_at DESC);
CREATE TABLE IF NOT EXISTS device_tokens (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,
    binding_generation INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_device ON device_tokens(device_id, revoked_at);
CREATE TABLE IF NOT EXISTS device_storage (
    device_id TEXT PRIMARY KEY,
    binding_generation INTEGER NOT NULL,
    current_scan_id TEXT,
    scan_complete INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    free_bytes INTEGER NOT NULL DEFAULT 0,
    auto_cleanup_enabled INTEGER NOT NULL DEFAULT 1,
    retention_days INTEGER NOT NULL DEFAULT 30,
    trigger_free_percent INTEGER NOT NULL DEFAULT 15,
    target_free_percent INTEGER NOT NULL DEFAULT 20,
    scanned_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS device_storage_sessions (
    device_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL,
    client_session_id TEXT NOT NULL,
    server_session_id TEXT,
    scan_id TEXT NOT NULL,
    upload_state TEXT NOT NULL,
    local_bytes INTEGER NOT NULL DEFAULT 0,
    ended_at_utc INTEGER,
    deletable INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(device_id, binding_generation, client_session_id),
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_storage_sessions_device
    ON device_storage_sessions(device_id, binding_generation, ended_at_utc DESC);
CREATE TABLE IF NOT EXISTS device_storage_commands (
    command_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    result_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_storage_commands_pending
    ON device_storage_commands(device_id, binding_generation, status, created_at);
CREATE TABLE IF NOT EXISTS device_sessions (
    server_session_id TEXT PRIMARY KEY,
    client_session_id TEXT NOT NULL,
    request_hash TEXT,
    device_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL,
    started_at_utc TEXT NOT NULL,
    ended_at_utc TEXT,
    codec TEXT NOT NULL,
    sample_rate INTEGER NOT NULL,
    channels INTEGER NOT NULL,
    bits_per_sample INTEGER NOT NULL,
    scene TEXT NOT NULL DEFAULT 'meeting',
    title TEXT,
    source_language TEXT NOT NULL DEFAULT 'auto',
    target_language TEXT,
    upload_mode TEXT NOT NULL DEFAULT 'live',
    speaker_diarization_enabled INTEGER NOT NULL DEFAULT 1,
    canonical_total_bytes INTEGER,
    canonical_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'uploading',
    expected_chunks INTEGER,
    expected_samples INTEGER,
    revision INTEGER NOT NULL DEFAULT 0,
    caption_revision INTEGER NOT NULL DEFAULT 0,
    speaker_revision INTEGER NOT NULL DEFAULT 0,
    translation_revision INTEGER NOT NULL DEFAULT 0,
    summary_revision INTEGER NOT NULL DEFAULT 0,
    display_revision INTEGER NOT NULL DEFAULT 0,
    partial_caption TEXT NOT NULL DEFAULT '',
    partial_start_ms INTEGER NOT NULL DEFAULT 0,
    partial_end_ms INTEGER NOT NULL DEFAULT 0,
    partial_updated_at TEXT,
    failure_code TEXT,
    failure_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(device_id, client_session_id),
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_device_sessions_owner ON device_sessions(owner_user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS device_display_events (
    server_session_id TEXT NOT NULL,
    display_revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    start_ms INTEGER NOT NULL DEFAULT 0,
    end_ms INTEGER NOT NULL DEFAULT 0,
    seg_id TEXT,
    caption_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY(server_session_id, display_revision),
    FOREIGN KEY(server_session_id) REFERENCES device_sessions(server_session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_device_display_events_cursor
ON device_display_events(server_session_id, display_revision);
CREATE TABLE IF NOT EXISTS device_audio_chunks (
    server_session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    start_sample INTEGER NOT NULL,
    sample_count INTEGER NOT NULL,
    byte_count INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(server_session_id, seq),
    FOREIGN KEY(server_session_id) REFERENCES device_sessions(server_session_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS device_live_epochs (
    server_session_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    start_seq INTEGER NOT NULL,
    start_byte INTEGER NOT NULL,
    gap_start_byte INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(server_session_id, epoch),
    UNIQUE(server_session_id, start_seq),
    FOREIGN KEY(server_session_id) REFERENCES device_sessions(server_session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_device_live_epochs_latest
    ON device_live_epochs(server_session_id, epoch DESC);
CREATE TABLE IF NOT EXISTS device_audio_ranges (
    server_session_id TEXT NOT NULL,
    start_byte INTEGER NOT NULL,
    end_byte INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(server_session_id, start_byte),
    FOREIGN KEY(server_session_id) REFERENCES device_sessions(server_session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_device_audio_ranges_session_end
    ON device_audio_ranges(server_session_id, end_byte);
CREATE TABLE IF NOT EXISTS offline_asr_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    reason TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    order_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, start_ms, end_ms, reason)
);
CREATE INDEX IF NOT EXISTS idx_offline_asr_jobs_claim
    ON offline_asr_jobs(state, available_at, order_key, session_id, chunk_index, id);
CREATE INDEX IF NOT EXISTS idx_offline_asr_jobs_session
    ON offline_asr_jobs(session_id, state, reason);
CREATE TABLE IF NOT EXISTS device_session_marks (
    server_session_id TEXT NOT NULL,
    client_mark_id TEXT NOT NULL,
    offset_samples INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'mark',
    label TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(server_session_id, client_mark_id),
    FOREIGN KEY(server_session_id) REFERENCES device_sessions(server_session_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS api_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);
CREATE TABLE IF NOT EXISTS agenda_revisions (
    owner_user_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_voice_todos (
    device_id TEXT NOT NULL,
    client_todo_id TEXT NOT NULL,
    server_todo_id TEXT UNIQUE,
    owner_user_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL,
    audio_sha256 TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    audio_bytes INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    transcript TEXT,
    result_json TEXT,
    agenda_todo_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(device_id, client_todo_id),
    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE RESTRICT,
    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE RESTRICT
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            # 幂等迁移：老库补 translation 列（新库已在 _SCHEMA 中）
            try:
                self._conn.execute("ALTER TABLE segments ADD COLUMN translation TEXT")
            except sqlite3.OperationalError:
                pass
            # 多账号迁移：老库中的历史会议统一交给 TEST1，保证升级后仍可见。
            try:
                self._conn.execute("ALTER TABLE meetings ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT 'TEST1'")
            except sqlite3.OperationalError:
                pass
            self._conn.execute("UPDATE meetings SET owner_user_id='TEST1' WHERE owner_user_id IS NULL OR owner_user_id='' ")
            self._conn.execute("UPDATE agenda_events SET owner='TEST1' WHERE owner IS NULL OR owner='' OR owner='default'")
            self._conn.execute("UPDATE agenda_todos SET owner='TEST1' WHERE owner IS NULL OR owner='' OR owner='default'")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_meetings_owner_created ON meetings(owner_user_id, created_at DESC)")
            for column_sql in (
                "ALTER TABLE device_sessions ADD COLUMN request_hash TEXT",
                "ALTER TABLE device_sessions ADD COLUMN scene TEXT NOT NULL DEFAULT 'meeting'",
                "ALTER TABLE device_sessions ADD COLUMN title TEXT",
                "ALTER TABLE device_sessions ADD COLUMN source_language TEXT NOT NULL DEFAULT 'auto'",
                "ALTER TABLE device_sessions ADD COLUMN target_language TEXT",
                "ALTER TABLE device_sessions ADD COLUMN failure_code TEXT",
                "ALTER TABLE device_sessions ADD COLUMN failure_message TEXT",
                "ALTER TABLE device_sessions ADD COLUMN upload_mode TEXT NOT NULL DEFAULT 'live'",
                "ALTER TABLE device_sessions ADD COLUMN canonical_total_bytes INTEGER",
                "ALTER TABLE device_sessions ADD COLUMN canonical_sha256 TEXT",
                "ALTER TABLE device_sessions ADD COLUMN display_revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE device_sessions ADD COLUMN caption_revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE device_sessions ADD COLUMN speaker_revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE device_sessions ADD COLUMN translation_revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE device_sessions ADD COLUMN summary_revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE device_sessions ADD COLUMN partial_caption TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE device_sessions ADD COLUMN partial_start_ms INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE device_sessions ADD COLUMN partial_end_ms INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE device_sessions ADD COLUMN partial_updated_at TEXT",
                "ALTER TABLE segments ADD COLUMN caption_revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE segments ADD COLUMN speaker_revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE segments ADD COLUMN translation_revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE devices ADD COLUMN speaker_diarization_enabled INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE devices ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE device_sessions ADD COLUMN speaker_diarization_enabled INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE device_voice_todos ADD COLUMN server_todo_id TEXT",
                "ALTER TABLE agenda_todos ADD COLUMN assignee TEXT NOT NULL DEFAULT '我'",
                "ALTER TABLE agenda_todos ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'",
                "ALTER TABLE agenda_todos ADD COLUMN remind_mode TEXT NOT NULL DEFAULT 'none'",
                "ALTER TABLE agenda_todos ADD COLUMN remind_at TEXT",
                "ALTER TABLE agenda_todos ADD COLUMN note TEXT",
                "ALTER TABLE agenda_todos ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE agenda_todos ADD COLUMN completed_at TEXT",
                "ALTER TABLE agenda_todos ADD COLUMN created_at TEXT",
                "ALTER TABLE agenda_todos ADD COLUMN updated_at TEXT",
            ):
                try:
                    self._conn.execute(column_sql)
                except sqlite3.OperationalError:
                    pass
            # V1.0.1 channel cursors use the historical global revision as an
            # event stamp.  This preserves ordering for old rows without
            # renumbering any recorder cursor.
            self._conn.execute(
                "UPDATE segments SET caption_revision=revision"
                " WHERE caption_revision=0 AND text!=''")
            self._conn.execute(
                "UPDATE device_sessions SET caption_revision=COALESCE(("
                " SELECT MAX(caption_revision) FROM segments"
                " WHERE segments.session_id=device_sessions.server_session_id),0)"
                " WHERE caption_revision=0")
            agenda_now = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "UPDATE agenda_todos SET created_at=COALESCE(created_at,?),"
                " updated_at=COALESCE(updated_at,?)", (agenda_now, agenda_now))
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_voice_todos_server_id"
                " ON device_voice_todos(server_todo_id) WHERE server_todo_id IS NOT NULL")
            for row in self._conn.execute(
                    "SELECT device_id,client_todo_id FROM device_voice_todos"
                    " WHERE server_todo_id IS NULL OR server_todo_id='' ").fetchall():
                stable_id = "lyt-" + hashlib.sha256(
                    f"{row['device_id']}:{row['client_todo_id']}".encode()).hexdigest()[:32]
                self._conn.execute(
                    "UPDATE device_voice_todos SET server_todo_id=?"
                    " WHERE device_id=? AND client_todo_id=?",
                    (stable_id, row["device_id"], row["client_todo_id"]))

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def transaction(self):
        """with db.transaction(): 里的多条 execute 原子提交。"""
        return _Txn(self._conn, self._lock)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # meta k/v
    def get_meta(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))


class _Txn:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self.conn, self.lock = conn, lock

    def __enter__(self):
        self.lock.acquire()
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        finally:
            self.lock.release()
        return False


def row_to_segment(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seg_id": row["seg_id"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "text": row["text"],
        "speaker_id": row["speaker_id"],
        "speaker_label": row["speaker_label"],
        "speaker_final": bool(row["speaker_final"]),
        "source": row["source"],
        "state": row["state"],
        "revision": row["revision"],
        "caption_revision": (row["caption_revision"]
                             if "caption_revision" in row.keys() else row["revision"]),
        "speaker_revision": (row["speaker_revision"]
                             if "speaker_revision" in row.keys() else 0),
        "translation_revision": (row["translation_revision"]
                                 if "translation_revision" in row.keys() else 0),
        "translation": row["translation"] if "translation" in row.keys() else None,
    }


def loads_or(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default
