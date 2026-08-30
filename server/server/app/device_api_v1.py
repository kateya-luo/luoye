"""落叶设备云端 API v1。

这是设备与 ClearMeeting 的唯一正式 HTTP 边界。账号密码只用于 Web，设备使用可撤销
opaque token；录音会话在创建时冻结 owner 与 binding_generation，保证解绑/换绑不会
改变历史录音归属。所有可重试写入都实现内容校验后的幂等重放。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import struct
import uuid
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from .agenda import (AgendaStore, DEFAULT_TZ, clean_voice_todo_content,
                     extract_optional_voice_todo, get_timezone)
from .auth import CurrentUser, require_auth
from .deepseek_client import DeepSeekClient
from .funasr_client import FunASRClient
from .funasr_offline_client import FunASROfflineClient
from .speaker_diarizer import SpeakerDiarizer
from .translator import dominant_lang

logger = logging.getLogger("ai_recorder.device_v2")

API_CONTRACT = "luoye-device-api/2"
SERVER_RELEASE = os.getenv("SERVER_RELEASE", "clearmeeting-server-v2.0.0")
DEVICE_AUTH_PROFILE = os.getenv("DEVICE_AUTH_PROFILE", "engineering").strip().lower()
if DEVICE_AUTH_PROFILE != "engineering":
    raise RuntimeError(
        "DEVICE_AUTH_PROFILE currently supports only 'engineering'; "
        "factory signature verification is not implemented")
PAIRING_TTL_SECONDS = max(60, int(os.getenv("DEVICE_PAIRING_TTL_SECONDS", "600")))
DEVICE_TOKEN_TTL_SECONDS = max(3600, int(os.getenv("DEVICE_TOKEN_TTL_SECONDS", str(365 * 86400))))
ONLINE_WINDOW_SECONDS = max(15, int(os.getenv("DEVICE_ONLINE_WINDOW_SECONDS", "90")))
# Protocol v1 firmware has fixed 160 KiB audio chunks and a ~938 KiB voice
# todo buffer.  Operations may raise these ceilings, never configure them below
# what a conforming device must be able to send.
MAX_CHUNK_BYTES = max(163_840, int(os.getenv("DEVICE_MAX_CHUNK_BYTES", str(1024 * 1024))))
RANGE_BLOCK_BYTES = 10 * 1024 * 1024
MAX_RANGE_BYTES = RANGE_BLOCK_BYTES
RANGE_COPY_BYTES = 64 * 1024
MAX_TODO_BYTES = max(1024 * 1024, int(os.getenv("DEVICE_MAX_TODO_BYTES", str(4 * 1024 * 1024))))
CLAIM_RATE_WINDOW_SECONDS = max(60, int(os.getenv("DEVICE_CLAIM_RATE_WINDOW_SECONDS", "300")))
CLAIM_RATE_MAX_FAILURES = max(2, int(os.getenv("DEVICE_CLAIM_RATE_MAX_FAILURES", "5")))
LIVE_ASR_START_TIMEOUT_SECONDS = max(
    1.0, float(os.getenv("DEVICE_LIVE_ASR_START_TIMEOUT_SECONDS", "5")))
LIVE_ASR_SEND_TIMEOUT_SECONDS = max(
    1.0, float(os.getenv("DEVICE_LIVE_ASR_SEND_TIMEOUT_SECONDS", "5")))
DEVICE_ID_RE = re.compile(r"^LY-[0-9A-F]{12}$")
HEX_32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_claim_failures: dict[str, deque[float]] = defaultdict(deque)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def parse_time(value: str | None, *, fallback_now: bool = True) -> str | None:
    if value is None or not str(value).strip():
        return iso_now() if fallback_now else None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise api_error(400, "INVALID_TIME", "时间必须是 ISO-8601") from exc
    if parsed.tzinfo is None:
        raise api_error(400, "INVALID_TIME", "时间必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def request_id() -> str:
    return "req-" + uuid.uuid4().hex[:20]


def api_error(status: int, code: str, message: str, *, retryable: bool = False,
              extra: dict[str, Any] | None = None) -> HTTPException:
    body: dict[str, Any] = {
        "error": {"code": code, "message": message, "retryable": retryable,
                  "request_id": request_id()}
    }
    if extra:
        body.update(extra)
    return HTTPException(status_code=status, detail=body)


def normalized_device_id(value: str) -> str:
    device_id = str(value or "").strip().upper()
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise api_error(400, "INVALID_DEVICE_ID", "device_id 格式必须为 LY-AABBCCDDEEFF")
    return device_id


def masked_account(username: str) -> str:
    value = str(username or "")
    if len(value) <= 2:
        return value[:1] + "*"
    return value[:2] + "*" * max(2, len(value) - 2)


def device_text(value: Any, max_utf8_bytes: int) -> str:
    """固件定长字段清洗：仅 BMP、无控制字符，并在 UTF-8 字节边界截断。"""
    clean = "".join(ch for ch in str(value or "")
                    if 0x20 <= ord(ch) <= 0xFFFF and not 0x7F <= ord(ch) <= 0x9F)
    raw = clean.encode("utf-8")[:max_utf8_bytes]
    while raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]
    return ""


def durable_write(path: Path, data: bytes) -> None:
    """同目录临时文件 + fsync + replace，避免断电留下半个可见文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


async def read_bounded_body(request: Request, limit: int, *, code: str,
                            message: str) -> bytes:
    """Read an HTTP body without ever buffering more than ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise api_error(400, "CONTENT_LENGTH_INVALID", "Content-Length 无效") from exc
        if declared_size < 0:
            raise api_error(400, "CONTENT_LENGTH_INVALID", "Content-Length 无效")
        if declared_size > limit:
            raise api_error(413, code, message)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise api_error(413, code, message)
        body.extend(chunk)
    return bytes(body)


def require_media_type(request: Request, expected: str) -> None:
    value = request.headers.get("content-type", "")
    media_type = value.split(";", 1)[0].strip().lower()
    if media_type != expected.lower():
        raise api_error(415, "CONTENT_TYPE_UNSUPPORTED",
                        f"Content-Type 必须是 {expected}")


class PairStartInput(BaseModel):
    device_id: str
    pairing_code: str
    nonce: str
    firmware_version: str = Field(min_length=1, max_length=64)
    hardware_revision: str = Field(default="unknown", max_length=128)
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    protocol_version: str = API_CONTRACT


class PairStatusInput(BaseModel):
    device_id: str
    nonce: str


class ClaimInput(BaseModel):
    pairing_code: str
    display_name: str | None = Field(default=None, max_length=80)


class DeviceUpdateInput(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    speaker_diarization_enabled: bool | None = None


class StorageSessionInput(BaseModel):
    client_session_id: str = Field(min_length=1, max_length=128)
    server_session_id: str | None = Field(default=None, max_length=128)
    local_bytes: int = Field(ge=0)
    ended_at_utc: int | None = Field(default=None, ge=0)
    # Accepted for v0.8.1 compatibility, but v0.14.2 storage management does
    # not use cloud meeting/upload state to decide whether a local file may be
    # deleted.
    upload_state: str = Field(default="local", min_length=1, max_length=32)
    deletable: bool = True


class StorageSnapshotInput(BaseModel):
    binding_generation: int = Field(ge=1)
    scan_id: str = Field(min_length=8, max_length=64)
    scan_start: bool = False
    complete: bool = False
    total_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    sessions: list[StorageSessionInput] = Field(default_factory=list, max_length=32)


class StorageCommandInput(BaseModel):
    action: Literal["delete_sessions", "delete_all_closed"]
    session_ids: list[str] = Field(default_factory=list, max_length=32)


class StorageCommandAckInput(BaseModel):
    binding_generation: int = Field(ge=1)
    status: Literal["completed", "failed", "rejected"]
    deleted_session_ids: list[str] = Field(default_factory=list, max_length=32)
    deleted_count: int = Field(default=0, ge=0, le=1000)
    freed_bytes: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=64)


class AudioFormat(BaseModel):
    codec: str = "pcm_s16le"
    sample_rate: int = 16000
    channels: int = 1
    bits_per_sample: int = 16


class SessionCreateInput(BaseModel):
    client_session_id: str
    started_at_utc: str | None = None
    binding_generation: int = Field(ge=1)
    audio: AudioFormat = Field(default_factory=AudioFormat)
    scene: Literal["meeting", "translate"] = "meeting"
    title: str | None = Field(default=None, max_length=200)
    source_language: str = Field(default="auto", max_length=16)
    target_language: str | None = Field(default=None, max_length=16)
    upload_mode: Literal["live", "bulk", "repair"] = "live"
    speaker_diarization_enabled: bool | None = None


class MarkInput(BaseModel):
    offset_samples: int = Field(ge=0)
    kind: str = Field(default="mark", max_length=32)
    label: str | None = Field(default=None, max_length=200)


class SessionEndInput(BaseModel):
    total_chunks: int = Field(ge=0, le=2_000_000)
    total_samples: int = Field(ge=0)
    ended_at_utc: str | None = None
    binding_generation: int = Field(ge=1)


class UploadPlanInput(BaseModel):
    total_bytes: int = Field(ge=0)
    total_samples: int = Field(ge=0)
    binding_generation: int = Field(ge=1)
    mode: Literal["bulk", "repair"]


class RangeCompleteInput(BaseModel):
    total_bytes: int = Field(ge=0)
    total_samples: int = Field(ge=0)
    ended_at_utc: str | None = None
    binding_generation: int = Field(ge=1)
    file_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class SessionCancelInput(BaseModel):
    binding_generation: int = Field(ge=1)
    reason: str = Field(default="local_delete", min_length=1, max_length=64)


class LiveResumeInput(BaseModel):
    binding_generation: int = Field(ge=1)
    gap_start_bytes: int = Field(ge=0)
    resume_offset_bytes: int = Field(ge=0)


class SessionDeferInput(BaseModel):
    total_bytes: int = Field(ge=0)
    total_samples: int = Field(ge=0)
    ended_at_utc: str | None = None
    binding_generation: int = Field(ge=1)


class TodoActionInput(BaseModel):
    action: str
    revision: int = Field(ge=1)


@dataclass(frozen=True)
class DevicePrincipal:
    device_id: str
    owner_user_id: str
    binding_generation: int
    token_id: str


@dataclass
class DeviceLiveRuntime:
    """One in-process streaming ASR cursor for a reliable device session.

    Audio durability never depends on this object.  It deliberately is not
    reconstructed from old chunks after a process restart: the complete PCM
    file and the offline recognizer remain the authoritative recovery path.
    """

    asr: FunASRClient
    next_seq: int
    next_sample: int
    segment_start_sample: int
    source_language: str
    target_language: str | None
    speaker_enabled: bool = False
    diarizer: SpeakerDiarizer | None = None
    speaker_pcm: bytearray = field(default_factory=bytearray)
    speaker_pcm_start_sample: int = 0
    speaker_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    speaker_tasks: set[asyncio.Task] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    translation_context: list[tuple[str, str]] = field(default_factory=list)
    last_final_text: str = ""


class DeviceService:
    def __init__(self, storage: Any, *,
                 on_session_complete: Callable[[str, int], Awaitable[None]] | None = None,
                 on_audio_range_committed: Callable[[str], Awaitable[None]] | None = None,
                 offline_progress: Callable[[str], dict[str, Any]] | None = None,
                 on_live_caption: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
                 on_mark: Callable[[str], Awaitable[None]] | None = None
                 ) -> None:
        self.storage = storage
        self.db = storage.db
        self.on_session_complete = on_session_complete
        self.on_audio_range_committed = on_audio_range_committed
        self.offline_progress = offline_progress
        self.on_live_caption = on_live_caption
        self.on_mark = on_mark
        secret = os.getenv("DEVICE_API_SECRET", "").strip() or self.db.get_meta("device_api_v1_secret")
        if not secret:
            secret = secrets.token_urlsafe(48)
            self.db.set_meta("device_api_v1_secret", secret)
        self._secret = secret.encode()
        self.chunk_root = storage.root / "device_v1" / "chunks"
        self.todo_root = storage.root / "device_v1" / "todos"
        self.range_root = storage.root / "device_v2" / "ranges"
        self.chunk_root.mkdir(parents=True, exist_ok=True)
        self.todo_root.mkdir(parents=True, exist_ok=True)
        self.range_root.mkdir(parents=True, exist_ok=True)
        self._live: dict[str, DeviceLiveRuntime] = {}
        self._live_init_locks: dict[str, asyncio.Lock] = {}
        self._live_disabled: set[str] = set()
        self._range_locks: dict[str, asyncio.Lock] = {}
        self._translation_llm = DeepSeekClient()
        self._callback_tasks: set[asyncio.Task] = set()

    async def _notify_audio_range_committed(self, session_id: str) -> None:
        if self.on_audio_range_committed is None:
            return
        try:
            await self.on_audio_range_committed(session_id)
        except Exception:
            # 音频范围已经 fsync + 入库；调度失败不能让固件误以为上传失败而重传。
            # 启动恢复扫描会再次建立缺失的 ASR 任务。
            logger.exception("device_range_schedule_failed session=%s", session_id)

    def _notify_live_caption(self, session_id: str, caption: dict[str, Any]) -> None:
        if self.on_live_caption is None:
            return
        task = asyncio.create_task(self.on_live_caption(session_id, caption))
        self._callback_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._callback_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error("device_live_caption_callback_failed session=%s error=%s",
                             session_id, error, exc_info=error)

        task.add_done_callback(done)

    @staticmethod
    def _translation_target(value: str | None) -> str | None:
        value = str(value or "").strip().lower().replace("_", "-")
        if not value:
            return None
        return value.split("-", 1)[0]

    async def _ensure_live_runtime(self, session: Any) -> DeviceLiveRuntime | None:
        """Start streaming ASR at the current durable contiguous cursor.

        Initialising at the already-acknowledged cursor is intentional.  If
        the server restarted, old chunks are *not* replayed into a fresh ASR
        websocket (which could duplicate captions); offline ASR covers them at
        session end.
        """
        session_id = str(session["server_session_id"])
        if session["status"] != "uploading" or session_id in self._live_disabled:
            return None
        runtime = self._live.get(session_id)
        if runtime is not None:
            return runtime
        init_lock = self._live_init_locks.setdefault(session_id, asyncio.Lock())
        async with init_lock:
            runtime = self._live.get(session_id)
            if runtime is not None:
                return runtime
            progress = self._live_progress(session_id)
            cursor = int(progress["live_acknowledged_bytes"]) // 2
            runtime = DeviceLiveRuntime(
                asr=FunASRClient(), next_seq=int(progress["live_next_seq"]),
                next_sample=cursor, segment_start_sample=cursor,
                source_language=str(session["source_language"] or "auto"),
                target_language=self._translation_target(session["target_language"]),
                speaker_enabled=bool(session["speaker_diarization_enabled"]),
                diarizer=(SpeakerDiarizer()
                          if bool(session["speaker_diarization_enabled"]) else None),
                speaker_pcm_start_sample=cursor,
            )
            try:
                await asyncio.wait_for(
                    runtime.asr.start(session_id, runtime.source_language),
                    timeout=LIVE_ASR_START_TIMEOUT_SECONDS)
            except Exception:
                logger.exception("device_live_asr_start_failed session=%s; offline fallback active",
                                 session_id)
                self._live_disabled.add(session_id)
                with suppress(BaseException):
                    await runtime.asr.close()
                return None
            self._live[session_id] = runtime
            logger.info("device_live_asr_started session=%s next_seq=%d next_sample=%d",
                        session_id, runtime.next_seq, runtime.next_sample)
            return runtime

    def _store_live_caption(self, session_id: str, *, text: str, start_sample: int,
                            end_sample: int) -> tuple[str, int]:
        seg_id = "lyseg-" + uuid.uuid4().hex
        now = iso_now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM device_sessions WHERE server_session_id=?",
                (session_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"device session disappeared: {session_id}")
            revision = int(row["revision"]) + 1
            conn.execute(
                "INSERT INTO segments(session_id,seg_id,ord,start_ms,end_ms,text,speaker_id,"
                "speaker_label,speaker_final,source,state,revision)"
                " VALUES(?,?,(SELECT COALESCE(MAX(ord),0)+1 FROM segments WHERE session_id=?),"
                "?,?,?,NULL,NULL,0,'live','provisional',?)",
                (session_id, seg_id, session_id, start_sample * 1000 // 16000,
                 end_sample * 1000 // 16000, text, revision))
            conn.execute(
                "UPDATE device_sessions SET revision=?,updated_at=? WHERE server_session_id=?",
                (revision, now, session_id))
            conn.execute("UPDATE meetings SET updated_at=? WHERE session_id=?",
                         (now, session_id))
        return seg_id, revision

    def _store_live_translation(self, session_id: str, seg_id: str, text: str) -> int:
        now = iso_now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM device_sessions WHERE server_session_id=?",
                (session_id,)).fetchone()
            segment = conn.execute(
                "SELECT 1 FROM segments WHERE session_id=? AND seg_id=?",
                (session_id, seg_id)).fetchone()
            if row is None or segment is None:
                raise RuntimeError(f"device caption disappeared: {session_id}/{seg_id}")
            revision = int(row["revision"]) + 1
            conn.execute(
                "UPDATE segments SET translation=?,revision=? WHERE session_id=? AND seg_id=?",
                (text, revision, session_id, seg_id))
            conn.execute(
                "UPDATE device_sessions SET revision=?,updated_at=? WHERE server_session_id=?",
                (revision, now, session_id))
            conn.execute("UPDATE meetings SET updated_at=? WHERE session_id=?",
                         (now, session_id))
        return revision

    def _store_live_speaker(self, session_id: str, seg_id: str,
                            speaker_id: str, speaker_label: str | None) -> int:
        """Persist a delayed CAM++ result without replaying the ASR segment."""
        now = iso_now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM device_sessions WHERE server_session_id=?",
                (session_id,)).fetchone()
            segment = conn.execute(
                "SELECT 1 FROM segments WHERE session_id=? AND seg_id=?",
                (session_id, seg_id)).fetchone()
            if row is None or segment is None:
                raise RuntimeError(f"device caption disappeared: {session_id}/{seg_id}")
            revision = int(row["revision"]) + 1
            conn.execute(
                "UPDATE segments SET speaker_id=?,speaker_label=?,speaker_final=0,revision=?"
                " WHERE session_id=? AND seg_id=?",
                (speaker_id, speaker_label, revision, session_id, seg_id))
            conn.execute(
                "UPDATE device_sessions SET revision=?,updated_at=? WHERE server_session_id=?",
                (revision, now, session_id))
            conn.execute("UPDATE meetings SET updated_at=? WHERE session_id=?",
                         (now, session_id))
        return revision

    async def _assign_live_speaker(self, session_id: str, runtime: DeviceLiveRuntime,
                                   seg_id: str, text: str, start_sample: int,
                                   end_sample: int, pcm: bytes) -> None:
        if not runtime.speaker_enabled or runtime.diarizer is None or not pcm:
            return
        async with runtime.speaker_lock:
            decision = await runtime.diarizer.assign_detailed(pcm)
        speaker_id = decision.speaker_id
        if not speaker_id:
            logger.info(
                "device_live_speaker_skipped session=%s seg=%s bytes=%d state=%s confidence=%.3f",
                session_id, seg_id, len(pcm), decision.state, decision.confidence)
            return
        label = runtime.diarizer.label(speaker_id)
        revision = self._store_live_speaker(session_id, seg_id, speaker_id, label)
        logger.info("device_live_speaker session=%s seg=%s speaker=%s revision=%d",
                    session_id, seg_id, speaker_id, revision)
        self._notify_live_caption(session_id, {
            "seg_id": seg_id, "text": text,
            "start_ms": start_sample * 1000 // 16000,
            "end_ms": end_sample * 1000 // 16000,
            "speaker_id": speaker_id, "speaker_label": label,
            "speaker_state": decision.state,
            "speaker_confidence": round(decision.confidence, 4),
            "revision": revision, "language": runtime.source_language,
        })

    def _queue_live_speaker(self, session_id: str, runtime: DeviceLiveRuntime,
                            seg_id: str, text: str, start_sample: int,
                            end_sample: int, pcm: bytes) -> None:
        if not runtime.speaker_enabled or runtime.diarizer is None:
            return
        task = asyncio.create_task(self._assign_live_speaker(
            session_id, runtime, seg_id, text, start_sample, end_sample, pcm))
        runtime.speaker_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            runtime.speaker_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error("device_live_speaker_failed session=%s seg=%s error=%s",
                             session_id, seg_id, error, exc_info=error)

        task.add_done_callback(done)

    async def _translate_live_caption(self, session_id: str, runtime: DeviceLiveRuntime,
                                      seg_id: str, text: str) -> None:
        target = runtime.target_language
        if not target or dominant_lang(text) == target:
            return
        try:
            translated = await self._translation_llm.translate(
                text, target, context=list(runtime.translation_context))
        except Exception:
            logger.exception("device_live_translation_failed session=%s seg=%s",
                             session_id, seg_id)
            return
        translated = str(translated or "").strip()
        if not translated:
            return
        runtime.translation_context.append((text, translated))
        if len(runtime.translation_context) > 3:
            runtime.translation_context.pop(0)
        revision = self._store_live_translation(session_id, seg_id, translated)
        logger.info("device_live_translation session=%s seg=%s revision=%d",
                    session_id, seg_id, revision)

    async def _persist_live_results(self, session_id: str, runtime: DeviceLiveRuntime,
                                    results: list[dict[str, Any]], end_sample: int) -> None:
        finals: list[str] = []
        for result in results:
            text = str(result.get("text") or "").strip()
            if result.get("is_final") and text and text != runtime.last_final_text:
                finals.append(text)
        if not finals:
            return
        start = min(runtime.segment_start_sample, end_sample)
        span = max(0, end_sample - start)
        for index, text in enumerate(finals):
            seg_start = start + span * index // len(finals)
            seg_end = start + span * (index + 1) // len(finals)
            seg_id, revision = self._store_live_caption(
                session_id, text=text, start_sample=seg_start, end_sample=seg_end)
            runtime.last_final_text = text
            runtime.segment_start_sample = seg_end
            logger.info("device_live_caption session=%s seg=%s revision=%d samples=%d..%d",
                        session_id, seg_id, revision, seg_start, seg_end)
            self._notify_live_caption(session_id, {
                "seg_id": seg_id,
                "text": text,
                "start_ms": seg_start * 1000 // 16000,
                "end_ms": seg_end * 1000 // 16000,
                "speaker_id": None,
                "speaker_label": None,
                "revision": revision,
                "language": runtime.source_language,
            })
            pcm_start = max(0, seg_start - runtime.speaker_pcm_start_sample) * 2
            pcm_end = max(0, seg_end - runtime.speaker_pcm_start_sample) * 2
            self._queue_live_speaker(session_id, runtime, seg_id, text,
                                     seg_start, seg_end,
                                     bytes(runtime.speaker_pcm[pcm_start:pcm_end]))
            await self._translate_live_caption(session_id, runtime, seg_id, text)
        consumed = max(0, runtime.segment_start_sample - runtime.speaker_pcm_start_sample) * 2
        if consumed:
            del runtime.speaker_pcm[:consumed]
            runtime.speaker_pcm_start_sample = runtime.segment_start_sample

    async def _feed_live_locked(self, session_id: str, runtime: DeviceLiveRuntime) -> None:
        while True:
            row = self.db.query_one(
                "SELECT seq,start_sample,sample_count,path FROM device_audio_chunks"
                " WHERE server_session_id=? AND seq=?",
                (session_id, runtime.next_seq))
            if row is None:
                return
            if int(row["start_sample"]) != runtime.next_sample:
                logger.warning(
                    "device_live_noncontiguous session=%s seq=%d expected_sample=%d got=%d",
                    session_id, runtime.next_seq, runtime.next_sample, int(row["start_sample"]))
                return
            try:
                data = Path(row["path"]).read_bytes()
                if runtime.speaker_enabled:
                    runtime.speaker_pcm.extend(data)
                    # The voiceprint window has its own bounded timeline. It
                    # must never grow with a long-running ASR final segment.
                    max_speaker_bytes = (runtime.diarizer.max_bytes
                                         if runtime.diarizer is not None else 0)
                    if max_speaker_bytes and len(runtime.speaker_pcm) > max_speaker_bytes:
                        trim = len(runtime.speaker_pcm) - max_speaker_bytes
                        trim -= trim % 2
                        del runtime.speaker_pcm[:trim]
                        runtime.speaker_pcm_start_sample += trim // 2
                results = await asyncio.wait_for(
                    runtime.asr.send_audio(data), timeout=LIVE_ASR_SEND_TIMEOUT_SECONDS)
            except Exception:
                # The send outcome may be ambiguous.  Never replay it into the
                # same or a new live recognizer; the durable offline pass is the
                # exactly-once recovery authority.
                logger.exception("device_live_asr_failed session=%s seq=%d; offline fallback active",
                                 session_id, runtime.next_seq)
                self._live_disabled.add(session_id)
                self._live.pop(session_id, None)
                with suppress(BaseException):
                    await runtime.asr.close()
                return
            end_sample = runtime.next_sample + int(row["sample_count"])
            # Advance before handling results: if result persistence fails we
            # still must not feed this audio twice.
            runtime.next_seq += 1
            runtime.next_sample = end_sample
            try:
                await self._persist_live_results(session_id, runtime, results, end_sample)
            except Exception:
                logger.exception("device_live_result_persist_failed session=%s seq=%d",
                                 session_id, runtime.next_seq - 1)

    async def _feed_live(self, session_id: str, runtime: DeviceLiveRuntime | None) -> None:
        if runtime is None or self._live.get(session_id) is not runtime:
            return
        async with runtime.lock:
            await self._feed_live_locked(session_id, runtime)

    async def _finish_live(self, session_id: str, end_sample: int) -> None:
        runtime = self._live.get(session_id)
        if runtime is None:
            return
        async with runtime.lock:
            try:
                await self._feed_live_locked(session_id, runtime)
                if self._live.get(session_id) is runtime:
                    results = await runtime.asr.finish()
                    await self._persist_live_results(session_id, runtime, results, end_sample)
                if runtime.speaker_tasks:
                    await asyncio.gather(*tuple(runtime.speaker_tasks), return_exceptions=True)
            except Exception:
                logger.exception("device_live_asr_finish_failed session=%s; offline fallback active",
                                 session_id)
            finally:
                self._live.pop(session_id, None)
                self._live_init_locks.pop(session_id, None)
                with suppress(BaseException):
                    await runtime.asr.close()

    async def _abort_live(self, session_id: str) -> None:
        runtime = self._live.pop(session_id, None)
        self._live_init_locks.pop(session_id, None)
        self._live_disabled.add(session_id)
        if runtime is not None:
            with suppress(BaseException):
                await runtime.asr.close()

    async def _rotate_live(self, session_id: str) -> None:
        """Close one streaming recognizer without disabling later epochs."""
        runtime = self._live.get(session_id)
        if runtime is not None:
            await self._finish_live(session_id, runtime.next_sample)
        self._live_disabled.discard(session_id)

    def digest_code(self, code: str) -> str:
        return hmac.new(self._secret, f"pair-code:{code}".encode(), hashlib.sha256).hexdigest()

    def digest_nonce(self, device_id: str, nonce: str) -> str:
        return hmac.new(self._secret, f"pair-nonce:{device_id}:{nonce.lower()}".encode(),
                        hashlib.sha256).hexdigest()

    @staticmethod
    def digest_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _claim_bucket(identity: str) -> deque[float]:
        import time
        now = time.time()
        attempts = _claim_failures[identity]
        while attempts and attempts[0] < now - CLAIM_RATE_WINDOW_SECONDS:
            attempts.popleft()
        return attempts

    def check_claim_rate(self, identity: str) -> None:
        attempts = self._claim_bucket(identity)
        if len(attempts) >= CLAIM_RATE_MAX_FAILURES:
            raise api_error(429, "PAIRING_CLAIM_RATE_LIMITED", "配对尝试次数过多，请稍后再试",
                            retryable=True)

    def record_claim_failure(self, identity: str) -> None:
        import time
        self._claim_bucket(identity).append(time.time())

    @staticmethod
    def clear_claim_failures(identity: str) -> None:
        _claim_failures.pop(identity, None)

    def _pairing_response(self, row: Any) -> dict[str, Any]:
        if row["status"] not in {"pending", "claimed", "delivered"}:
            raise api_error(409, "PAIRING_NOT_ACTIVE", "配对请求已失效")
        return {
            "binding_status": "bound" if row["status"] in {"claimed", "delivered"} else "pending",
            "expires_at": row["expires_at"],
            "poll_after_seconds": 2,
        }

    def _expire_pairings(self, now: str) -> None:
        # This must commit before any request transaction that can raise.  If
        # done inside such a transaction, HTTPException would roll the cleanup
        # back and the partial unique index would keep an expired code occupied.
        self.db.execute(
            "UPDATE device_pairings SET status='expired',updated_at=?"
            " WHERE status='pending' AND expires_at<=?", (now, now))

    def start_pairing(self, body: PairStartInput) -> dict[str, Any]:
        device_id = normalized_device_id(body.device_id)
        code = str(body.pairing_code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise api_error(400, "INVALID_PAIRING_CODE", "配对码必须是六位数字")
        if not HEX_32_RE.fullmatch(body.nonce):
            raise api_error(400, "INVALID_PAIRING_NONCE", "nonce 必须是 128-bit 十六进制")
        if body.protocol_version != API_CONTRACT:
            raise api_error(409, "PROTOCOL_VERSION_UNSUPPORTED", "设备协议版本不受支持")
        now = iso_now()
        self._expire_pairings(now)
        expires = (utcnow() + timedelta(seconds=PAIRING_TTL_SECONDS)).isoformat()
        nonce_digest = self.digest_nonce(device_id, body.nonce)
        code_digest = self.digest_code(code)
        capabilities = sorted({str(x)[:64] for x in body.capabilities if str(x).strip()})
        try:
            with self.db.transaction() as conn:
                existing = conn.execute(
                    "SELECT * FROM device_pairings WHERE device_id=? AND nonce_digest=?",
                    (device_id, nonce_digest)).fetchone()
                if existing:
                    if datetime.fromisoformat(existing["expires_at"]) <= utcnow():
                        raise api_error(410, "PAIRING_EXPIRED", "配对请求已过期，请生成新 nonce")
                    if existing["status"] not in {"pending", "claimed", "delivered"}:
                        raise api_error(409, "PAIRING_NOT_ACTIVE", "配对请求已失效")
                    return self._pairing_response(existing)
                conn.execute(
                    "INSERT INTO devices(device_id,display_name,binding_generation,firmware_version,"
                    "hardware_revision,capabilities_json,protocol_version,last_seen_at,created_at,updated_at)"
                    " VALUES(?, '落叶录音笔',0,?,?,?,?,?,?,?)"
                    " ON CONFLICT(device_id) DO UPDATE SET firmware_version=excluded.firmware_version,"
                    " hardware_revision=excluded.hardware_revision,capabilities_json=excluded.capabilities_json,"
                    " protocol_version=excluded.protocol_version,last_seen_at=excluded.last_seen_at,"
                    " updated_at=excluded.updated_at",
                    (device_id, body.firmware_version, body.hardware_revision,
                     json.dumps(capabilities, ensure_ascii=False), body.protocol_version, now, now, now))
                conn.execute(
                    "UPDATE device_pairings SET status='superseded',updated_at=?"
                    " WHERE device_id=? AND status='pending'", (now, device_id))
                pid = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO device_pairings(id,device_id,code_digest,nonce_digest,status,expires_at,"
                    "created_at,updated_at) VALUES(?,?,?,?, 'pending',?,?,?)",
                    (pid, device_id, code_digest, nonce_digest, expires, now, now))
                row = conn.execute("SELECT * FROM device_pairings WHERE id=?", (pid,)).fetchone()
            return self._pairing_response(row)
        except sqlite3.IntegrityError as exc:
            raise api_error(409, "PAIRING_CODE_IN_USE", "配对码发生碰撞，请设备生成新配对码") from exc

    def claim(self, body: ClaimInput, user: CurrentUser) -> dict[str, Any]:
        code = str(body.pairing_code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise api_error(400, "INVALID_PAIRING_CODE", "配对码必须是六位数字")
        now = iso_now()
        self._expire_pairings(now)
        with self.db.transaction() as conn:
            pairing = conn.execute(
                "SELECT * FROM device_pairings WHERE code_digest=? AND status='pending'"
                " AND expires_at>?", (self.digest_code(code), now)).fetchone()
            if not pairing:
                expired = conn.execute(
                    "SELECT 1 FROM device_pairings WHERE code_digest=? AND status='expired'"
                    " ORDER BY created_at DESC LIMIT 1", (self.digest_code(code),)).fetchone()
                if expired:
                    raise api_error(410, "PAIRING_EXPIRED", "配对码已过期")
                raise api_error(404, "PAIRING_NOT_FOUND", "配对码不存在或已被认领")
            device = conn.execute("SELECT * FROM devices WHERE device_id=?",
                                  (pairing["device_id"],)).fetchone()
            if device["owner_user_id"] and device["owner_user_id"] != user.id:
                raise api_error(409, "DEVICE_ALREADY_BOUND", "设备已属于其他账号")
            generation = int(device["binding_generation"])
            if device["owner_user_id"] is None:
                generation += 1
            display_name = (body.display_name or device["display_name"] or "落叶录音笔").strip()
            conn.execute(
                "UPDATE devices SET owner_user_id=?,display_name=?,binding_generation=?,bound_at=?,"
                "updated_at=? WHERE device_id=?",
                (user.id, display_name, generation, now, now, device["device_id"]))
            conn.execute(
                "UPDATE device_pairings SET status='claimed',claimed_user_id=?,claimed_at=?,updated_at=?"
                " WHERE id=?", (user.id, now, now, pairing["id"]))
            # Binding itself is a device-visible agenda change.  Revision one
            # makes the first empty snapshot distinguishable from "unchanged".
            conn.execute(
                "INSERT INTO agenda_revisions(owner_user_id,revision,updated_at) VALUES(?,1,?)"
                " ON CONFLICT(owner_user_id) DO NOTHING", (user.id, now))
            updated = conn.execute("SELECT * FROM devices WHERE device_id=?", (device["device_id"],)).fetchone()
        return {"device": self.device_json(updated)}

    def pairing_status(self, body: PairStatusInput) -> dict[str, Any]:
        device_id = normalized_device_id(body.device_id)
        if not HEX_32_RE.fullmatch(body.nonce):
            raise api_error(400, "INVALID_PAIRING_NONCE", "nonce 必须是 128-bit 十六进制")
        nonce_digest = self.digest_nonce(device_id, body.nonce)
        now = iso_now()
        self._expire_pairings(now)
        token: str | None = None
        with self.db.transaction() as conn:
            pairing = conn.execute(
                "SELECT * FROM device_pairings WHERE device_id=? AND nonce_digest=?",
                (device_id, nonce_digest)).fetchone()
            if not pairing:
                raise api_error(404, "PAIRING_NOT_FOUND", "配对请求不存在")
            if (pairing["status"] == "expired"
                    or datetime.fromisoformat(pairing["expires_at"]) <= utcnow()):
                raise api_error(410, "PAIRING_EXPIRED", "配对请求已过期")
            if pairing["status"] == "pending":
                return self._pairing_response(pairing)
            if pairing["status"] not in {"claimed", "delivered"}:
                raise api_error(409, "PAIRING_NOT_ACTIVE", "配对请求已失效")
            device = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
            user = conn.execute("SELECT username FROM users WHERE id=?",
                                (device["owner_user_id"],)).fetchone()
            # status 响应可能在网络中丢失，因此允许持 nonce 重试。每次重试签发新 opaque
            # token 并撤销同代旧 token；generation 不变，不会改变历史数据归属。
            token = "lyd_" + secrets.token_urlsafe(36)
            token_expires_at = (utcnow() + timedelta(seconds=DEVICE_TOKEN_TTL_SECONDS)).isoformat()
            conn.execute("UPDATE device_tokens SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                         (now, device_id))
            conn.execute(
                "INSERT INTO device_tokens(id,device_id,token_digest,binding_generation,expires_at,"
                "created_at) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, device_id, self.digest_token(token),
                 int(device["binding_generation"]),
                 token_expires_at, now))
            conn.execute(
                "UPDATE device_pairings SET status='delivered',token_delivered_at=?,updated_at=? WHERE id=?",
                (now, now, pairing["id"]))
        response = {
            "binding_status": "bound",
            "binding_generation": int(device["binding_generation"]),
            "masked_account": device_text(masked_account(user["username"] if user else ""), 47),
            "server_time": iso_now(),
            "expires_at": pairing["expires_at"],
            "device_token_expires_at": token_expires_at,
        }
        response["device_token"] = token
        return response

    def authenticate(self, authorization: str | None, battery_percent: int | None = None) -> DevicePrincipal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise api_error(401, "DEVICE_TOKEN_REQUIRED", "缺少设备令牌")
        token = authorization.split(" ", 1)[1].strip()
        row = self.db.query_one(
            "SELECT t.*,d.owner_user_id,d.binding_generation AS current_generation"
            " FROM device_tokens t JOIN devices d ON d.device_id=t.device_id"
            " WHERE t.token_digest=?", (self.digest_token(token),))
        if not row or row["revoked_at"] is not None or datetime.fromisoformat(row["expires_at"]) <= utcnow():
            raise api_error(401, "DEVICE_TOKEN_INVALID", "设备令牌无效或已过期")
        if not row["owner_user_id"] or int(row["binding_generation"]) != int(row["current_generation"]):
            raise api_error(403, "BINDING_GENERATION_MISMATCH", "设备绑定已变化")
        now = iso_now()
        battery = None if battery_percent is None else max(0, min(100, int(battery_percent)))
        self.db.execute("UPDATE device_tokens SET last_used_at=? WHERE id=?", (now, row["id"]))
        if battery is None:
            self.db.execute("UPDATE devices SET last_seen_at=?,updated_at=? WHERE device_id=?",
                            (now, now, row["device_id"]))
        else:
            self.db.execute(
                "UPDATE devices SET last_seen_at=?,battery_percent=?,updated_at=? WHERE device_id=?",
                (now, battery, now, row["device_id"]))
        return DevicePrincipal(row["device_id"], row["owner_user_id"],
                               int(row["current_generation"]), row["id"])

    @staticmethod
    def validate_protocol_headers(protocol: str | None, firmware: str | None,
                                  device_header: str | None) -> tuple[str, str]:
        if protocol != API_CONTRACT:
            raise api_error(409, "PROTOCOL_VERSION_UNSUPPORTED",
                            "X-Luoye-Protocol 必须是 luoye-device-api/2")
        firmware_value = str(firmware or "").strip()
        if not firmware_value or len(firmware_value) > 64:
            raise api_error(400, "FIRMWARE_HEADER_INVALID", "缺少有效的 X-Luoye-Firmware")
        if not device_header:
            raise api_error(400, "DEVICE_HEADER_INVALID", "缺少有效的 X-Luoye-Device")
        return firmware_value, normalized_device_id(device_header)

    def validate_pair_headers(self, body_device_id: str, body_firmware: str | None,
                              body_protocol: str | None, protocol: str | None,
                              firmware: str | None, device_header: str | None) -> None:
        firmware_value, header_device_id = self.validate_protocol_headers(
            protocol, firmware, device_header)
        if body_protocol is not None and body_protocol != protocol:
            raise api_error(400, "PROTOCOL_HEADER_MISMATCH",
                            "请求体 protocol_version 与 X-Luoye-Protocol 不一致")
        if normalized_device_id(body_device_id) != header_device_id:
            raise api_error(400, "DEVICE_HEADER_MISMATCH",
                            "请求体 device_id 与 X-Luoye-Device 不一致")
        if body_firmware is not None and str(body_firmware).strip() != firmware_value:
            raise api_error(400, "FIRMWARE_HEADER_MISMATCH",
                            "请求体 firmware_version 与 X-Luoye-Firmware 不一致")

    async def require_device(self, authorization: str | None = Header(default=None),
                             x_battery_percent: int | None = Header(default=None),
                             x_luoye_protocol: str | None = Header(
                                 default=None, alias="X-Luoye-Protocol"),
                             x_luoye_firmware: str | None = Header(
                                 default=None, alias="X-Luoye-Firmware"),
                             x_luoye_device: str | None = Header(
                                 default=None, alias="X-Luoye-Device")) -> DevicePrincipal:
        firmware, header_device_id = self.validate_protocol_headers(
            x_luoye_protocol, x_luoye_firmware, x_luoye_device)
        principal = self.authenticate(authorization, x_battery_percent)
        if header_device_id != principal.device_id:
            raise api_error(403, "DEVICE_HEADER_MISMATCH",
                            "X-Luoye-Device 与设备令牌不一致")
        # Firmware can be upgraded without rebinding.  The authenticated header
        # is the latest inventory value and never changes ownership.
        self.db.execute(
            "UPDATE devices SET firmware_version=?,protocol_version=?,updated_at=?"
            " WHERE device_id=?",
            (firmware, x_luoye_protocol, iso_now(), principal.device_id))
        return principal

    def device_json(self, row: Any) -> dict[str, Any]:
        last_seen = datetime.fromisoformat(row["last_seen_at"]) if row["last_seen_at"] else None
        return {
            "device_id": row["device_id"],
            "display_name": row["display_name"],
            "binding_generation": int(row["binding_generation"]),
            "hardware_revision": row["hardware_revision"],
            "firmware_version": row["firmware_version"],
            "last_seen_at": row["last_seen_at"],
            "online": bool(last_seen and (utcnow() - last_seen).total_seconds() <= ONLINE_WINDOW_SECONDS),
            "battery_percent": row["battery_percent"],
            "bound_at": row["bound_at"],
            "capabilities": json.loads(row["capabilities_json"] or "[]"),
            "protocol_version": row["protocol_version"],
            "speaker_diarization_enabled": bool(row["speaker_diarization_enabled"]),
            "config_revision": int(row["config_revision"]),
        }

    def list_devices(self, user: CurrentUser) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM devices WHERE owner_user_id=? ORDER BY bound_at DESC",
                             (user.id,))
        return {"devices": [self.device_json(row) for row in rows]}

    def update_device(self, device_id: str, body: DeviceUpdateInput,
                      user: CurrentUser) -> dict[str, Any]:
        device_id = normalized_device_id(device_id)
        updates, values = [], []
        if body.display_name is not None:
            name = body.display_name.strip()
            if not name:
                raise api_error(400, "INVALID_DISPLAY_NAME", "设备名称不能为空")
            updates.append("display_name=?")
            values.append(name)
        if body.speaker_diarization_enabled is not None:
            updates.extend(["speaker_diarization_enabled=?", "config_revision=config_revision+1"])
            values.append(1 if body.speaker_diarization_enabled else 0)
        if updates:
            updates.append("updated_at=?")
            values.extend([iso_now(), device_id, user.id])
            self.db.execute(f"UPDATE devices SET {','.join(updates)} WHERE device_id=? AND owner_user_id=?",
                            tuple(values))
        row = self.db.query_one("SELECT * FROM devices WHERE device_id=? AND owner_user_id=?",
                                (device_id, user.id))
        if not row:
            raise api_error(404, "DEVICE_NOT_FOUND", "设备不存在")
        return {"device": self.device_json(row)}

    def unbind(self, device_id: str, user: CurrentUser) -> dict[str, Any]:
        device_id = normalized_device_id(device_id)
        now = iso_now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM devices WHERE device_id=? AND owner_user_id=?",
                               (device_id, user.id)).fetchone()
            if not row:
                raise api_error(404, "DEVICE_NOT_FOUND", "设备不存在")
            generation = int(row["binding_generation"]) + 1
            conn.execute("UPDATE devices SET owner_user_id=NULL,binding_generation=?,bound_at=NULL,"
                         "updated_at=? WHERE device_id=?", (generation, now, device_id))
            conn.execute("UPDATE device_tokens SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                         (now, device_id))
            conn.execute("UPDATE device_pairings SET status='revoked',updated_at=?"
                         " WHERE device_id=? AND status IN ('pending','claimed','delivered')",
                         (now, device_id))
            conn.execute("UPDATE device_storage_commands SET status='cancelled',updated_at=?,"
                         " completed_at=? WHERE device_id=? AND status IN ('queued','in_progress')",
                         (now, now, device_id))
        return {"ok": True, "device_id": device_id, "binding_generation": generation}

    def device_config(self, principal: DevicePrincipal) -> dict[str, Any]:
        row = self.db.query_one("SELECT speaker_diarization_enabled,config_revision FROM devices WHERE device_id=?",
                                (principal.device_id,))
        if not row:
            raise api_error(404, "DEVICE_NOT_FOUND", "设备不存在")
        return {"revision": int(row["config_revision"]),
                "speaker_diarization_enabled": bool(row["speaker_diarization_enabled"]),
                "applies": "next_session"}

    def _owned_device(self, device_id: str, user: CurrentUser) -> Any:
        device_id = normalized_device_id(device_id)
        row = self.db.query_one(
            "SELECT * FROM devices WHERE device_id=? AND owner_user_id=?",
            (device_id, user.id))
        if not row:
            raise api_error(404, "DEVICE_NOT_FOUND", "设备不存在或不属于当前账号")
        return row

    def _next_storage_command(self, principal: DevicePrincipal) -> dict[str, Any] | None:
        now = iso_now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM device_storage_commands WHERE device_id=?"
                " AND binding_generation=? AND status IN ('queued','in_progress')"
                " ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END,created_at LIMIT 1",
                (principal.device_id, principal.binding_generation)).fetchone()
            if not row:
                return None
            if row["status"] == "queued":
                conn.execute(
                    "UPDATE device_storage_commands SET status='in_progress',started_at=?,updated_at=?"
                    " WHERE command_id=? AND status='queued'", (now, now, row["command_id"]))
        payload = json.loads(row["payload_json"] or "{}")
        return {"command_id": row["command_id"], "action": row["action"], **payload}

    def storage_snapshot(self, body: StorageSnapshotInput,
                         principal: DevicePrincipal) -> dict[str, Any]:
        if body.binding_generation != principal.binding_generation:
            raise api_error(409, "BINDING_GENERATION_MISMATCH", "设备绑定代次已变化")
        if body.free_bytes > body.total_bytes and body.total_bytes:
            raise api_error(400, "STORAGE_TOTAL_INVALID", "可用容量不能大于总容量")
        for item in body.sessions:
            if not CLIENT_ID_RE.fullmatch(item.client_session_id):
                raise api_error(400, "CLIENT_SESSION_ID_INVALID", "本地录音 ID 无效")
        now = iso_now()
        with self.db.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM device_storage WHERE device_id=?", (principal.device_id,)).fetchone()
            if current and int(current["binding_generation"]) != principal.binding_generation:
                conn.execute("DELETE FROM device_storage_sessions WHERE device_id=?",
                             (principal.device_id,))
            if body.scan_start or not current or current["current_scan_id"] != body.scan_id:
                scan_complete = 0
            else:
                scan_complete = int(current["scan_complete"])
            conn.execute(
                "INSERT INTO device_storage(device_id,binding_generation,current_scan_id,scan_complete,"
                "total_bytes,free_bytes,scanned_at,updated_at) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET binding_generation=excluded.binding_generation,"
                "current_scan_id=excluded.current_scan_id,scan_complete=excluded.scan_complete,"
                "total_bytes=excluded.total_bytes,free_bytes=excluded.free_bytes,"
                "scanned_at=excluded.scanned_at,updated_at=excluded.updated_at",
                (principal.device_id, principal.binding_generation, body.scan_id, scan_complete,
                 body.total_bytes, body.free_bytes, now, now))
            for item in body.sessions:
                conn.execute(
                    "INSERT INTO device_storage_sessions(device_id,binding_generation,client_session_id,"
                    "server_session_id,scan_id,upload_state,local_bytes,ended_at_utc,deletable,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,binding_generation,client_session_id)"
                    " DO UPDATE SET server_session_id=excluded.server_session_id,scan_id=excluded.scan_id,"
                    "upload_state=excluded.upload_state,local_bytes=excluded.local_bytes,"
                    "ended_at_utc=excluded.ended_at_utc,deletable=excluded.deletable,updated_at=excluded.updated_at",
                    (principal.device_id, principal.binding_generation, item.client_session_id,
                     None, body.scan_id, "local", item.local_bytes,
                     item.ended_at_utc, 1, now))
            if body.complete:
                conn.execute(
                    "DELETE FROM device_storage_sessions WHERE device_id=? AND binding_generation=?"
                    " AND scan_id<>?", (principal.device_id, principal.binding_generation, body.scan_id))
                conn.execute("UPDATE device_storage SET scan_complete=1,updated_at=? WHERE device_id=?",
                             (now, principal.device_id))
        return {"command": self._next_storage_command(principal)}

    def storage_command_ack(self, command_id: str, body: StorageCommandAckInput,
                            principal: DevicePrincipal) -> dict[str, Any]:
        if body.binding_generation != principal.binding_generation:
            raise api_error(409, "BINDING_GENERATION_MISMATCH", "设备绑定代次已变化")
        now = iso_now()
        result = body.model_dump()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM device_storage_commands WHERE command_id=? AND device_id=?"
                " AND binding_generation=?", (command_id, principal.device_id,
                                               principal.binding_generation)).fetchone()
            if not row:
                raise api_error(404, "STORAGE_COMMAND_NOT_FOUND", "存储命令不存在")
            if row["status"] not in ("queued", "in_progress"):
                return {"ok": True, "command_id": command_id, "status": row["status"]}
            conn.execute(
                "UPDATE device_storage_commands SET status=?,result_json=?,completed_at=?,updated_at=?"
                " WHERE command_id=?", (body.status, json.dumps(result, ensure_ascii=False),
                                        now, now, command_id))
            for session_id in body.deleted_session_ids:
                conn.execute(
                    "DELETE FROM device_storage_sessions WHERE device_id=? AND binding_generation=?"
                    " AND client_session_id=?", (principal.device_id,
                                                  principal.binding_generation, session_id))
            if row["action"] == "delete_all_closed" and body.status == "completed":
                conn.execute(
                    "DELETE FROM device_storage_sessions WHERE device_id=? AND binding_generation=?",
                    (principal.device_id, principal.binding_generation))
        return {"ok": True, "command_id": command_id, "status": body.status}

    def get_storage(self, device_id: str, user: CurrentUser) -> dict[str, Any]:
        device = self._owned_device(device_id, user)
        device_id = device["device_id"]
        generation = int(device["binding_generation"])
        storage = self.db.query_one("SELECT * FROM device_storage WHERE device_id=?", (device_id,))
        sessions = self.db.query(
            "SELECT client_session_id,local_bytes,ended_at_utc,updated_at"
            " FROM device_storage_sessions WHERE device_id=?"
            " AND binding_generation=? ORDER BY COALESCE(ended_at_utc,0) DESC,client_session_id DESC",
            (device_id, generation))
        commands = self.db.query(
            "SELECT command_id,action,status,result_json,created_at,completed_at FROM device_storage_commands"
            " WHERE device_id=? AND binding_generation=? ORDER BY created_at DESC LIMIT 10",
            (device_id, generation))
        total = int(storage["total_bytes"]) if storage else 0
        free = int(storage["free_bytes"]) if storage else 0
        return {
            "device_id": device_id,
            "binding_generation": generation,
            "online": self.device_json(device)["online"],
            "total_bytes": total,
            "free_bytes": free,
            "used_bytes": max(0, total - free),
            "scan_complete": bool(storage and storage["scan_complete"]),
            "scanned_at": storage["scanned_at"] if storage else None,
            "sessions": [dict(row) for row in sessions],
            "commands": [{**dict(row), "result": json.loads(row["result_json"])
                           if row["result_json"] else None} for row in commands],
        }

    def create_storage_command(self, device_id: str, body: StorageCommandInput,
                               user: CurrentUser) -> dict[str, Any]:
        device = self._owned_device(device_id, user)
        device_id = device["device_id"]
        generation = int(device["binding_generation"])
        session_ids = list(dict.fromkeys(body.session_ids))
        payload: dict[str, Any] = {}
        if body.action == "delete_sessions":
            if not session_ids:
                raise api_error(400, "STORAGE_SESSIONS_REQUIRED", "请选择要删除的设备录音")
            if any(not CLIENT_ID_RE.fullmatch(value) for value in session_ids):
                raise api_error(400, "CLIENT_SESSION_ID_INVALID", "本地录音 ID 无效")
            placeholders = ",".join("?" for _ in session_ids)
            rows = self.db.query(
                f"SELECT client_session_id FROM device_storage_sessions WHERE device_id=?"
                f" AND binding_generation=? AND client_session_id IN ({placeholders})",
                (device_id, generation, *session_ids))
            if len(rows) != len(session_ids):
                raise api_error(404, "STORAGE_SESSION_NOT_FOUND",
                                "选择的设备录音不存在或清单尚未更新")
            payload["session_ids"] = session_ids
        command_id = "lysc-" + uuid.uuid4().hex
        now = iso_now()
        self.db.execute(
            "INSERT INTO device_storage_commands(command_id,device_id,owner_user_id,binding_generation,"
            "action,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?, 'queued',?,?)",
            (command_id, device_id, user.id, generation, body.action,
             json.dumps(payload, ensure_ascii=False), now, now))
        return {"command_id": command_id, "status": "queued", "action": body.action, **payload}

    def _idempotency_lookup(self, scope: str, key: str | None,
                            payload_hash: str) -> dict[str, Any] | None:
        if not key or len(key) > 200:
            raise api_error(400, "IDEMPOTENCY_KEY_REQUIRED", "缺少或无效的 Idempotency-Key")
        row = self.db.query_one(
            "SELECT request_hash,status_code,response_json FROM api_idempotency"
            " WHERE scope=? AND idempotency_key=?", (scope, key))
        if not row:
            return None
        if not hmac.compare_digest(row["request_hash"], payload_hash):
            raise api_error(409, "IDEMPOTENCY_KEY_REUSED", "同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def _idempotency_store(self, scope: str, key: str, payload_hash: str,
                           response: dict[str, Any], status_code: int = 200) -> None:
        try:
            self.db.execute(
                "INSERT INTO api_idempotency(scope,idempotency_key,request_hash,status_code,response_json,"
                "created_at) VALUES(?,?,?,?,?,?)",
                (scope, key, payload_hash, status_code,
                 json.dumps(response, ensure_ascii=False, sort_keys=True), iso_now()))
        except sqlite3.IntegrityError:
            replay = self._idempotency_lookup(scope, key, payload_hash)
            if replay != response:
                raise api_error(409, "IDEMPOTENCY_RACE", "并发幂等请求结果冲突")

    def _check_generation(self, principal: DevicePrincipal, generation: int) -> None:
        if generation != principal.binding_generation:
            raise api_error(403, "BINDING_GENERATION_MISMATCH", "设备绑定已变化")

    def _session(self, principal: DevicePrincipal, server_session_id: str) -> Any:
        if not CLIENT_ID_RE.fullmatch(server_session_id):
            raise api_error(400, "INVALID_SESSION_ID", "会话编号无效")
        row = self.db.query_one(
            "SELECT * FROM device_sessions WHERE server_session_id=? AND device_id=?",
            (server_session_id, principal.device_id))
        if not row:
            raise api_error(404, "SESSION_NOT_FOUND", "会话不存在")
        if (row["owner_user_id"] != principal.owner_user_id
                or int(row["binding_generation"]) != principal.binding_generation):
            raise api_error(403, "SESSION_BINDING_MISMATCH", "会话属于其他绑定代次")
        return row

    def _progress(self, server_session_id: str) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT seq,start_sample,sample_count,byte_count FROM device_audio_chunks"
            " WHERE server_session_id=? ORDER BY seq", (server_session_id,))
        by_seq = {int(row["seq"]): row for row in rows}
        next_seq = 0
        acknowledged_samples = 0
        acknowledged_bytes = 0
        while next_seq in by_seq:
            row = by_seq[next_seq]
            # ACK means both sequence and sample/byte position are contiguous.
            # A future or misplaced chunk remains durable but is not permission
            # for firmware to delete its local copy.
            if int(row["start_sample"]) != acknowledged_samples:
                break
            acknowledged_samples += int(row["sample_count"])
            acknowledged_bytes += int(row["byte_count"])
            next_seq += 1
        return {
            "next_seq": next_seq,
            "received_chunks": next_seq,
            "total_received_chunks": len(rows),
            "received_samples": acknowledged_samples,
            "acknowledged_bytes": acknowledged_bytes,
        }

    def _live_progress(self, server_session_id: str) -> dict[str, Any]:
        """Return the cursor of the newest realtime epoch.

        Canonical coverage intentionally remains separate: a realtime epoch
        may start after an offline hole, while the missing bytes still have to
        be repaired manually before the recording becomes complete.
        """
        epoch = self.db.query_one(
            "SELECT epoch,start_seq,start_byte,gap_start_byte"
            " FROM device_live_epochs WHERE server_session_id=?"
            " ORDER BY epoch DESC LIMIT 1", (server_session_id,))
        epoch_number = int(epoch["epoch"]) if epoch else 0
        next_seq = int(epoch["start_seq"]) if epoch else 0
        acknowledged = int(epoch["start_byte"]) if epoch else 0
        rows = self.db.query(
            "SELECT seq,start_sample,byte_count FROM device_audio_chunks"
            " WHERE server_session_id=? AND seq>=? ORDER BY seq",
            (server_session_id, next_seq))
        by_seq = {int(row["seq"]): row for row in rows}
        received_chunks = 0
        while next_seq in by_seq:
            row = by_seq[next_seq]
            if int(row["start_sample"]) * 2 != acknowledged:
                break
            acknowledged += int(row["byte_count"])
            next_seq += 1
            received_chunks += 1
        return {
            "live_epoch": epoch_number,
            "live_next_seq": next_seq,
            "live_received_chunks": received_chunks,
            "live_received_samples": acknowledged // 2,
            "live_acknowledged_bytes": acknowledged,
        }

    def _upload_ack(self, server_session_id: str) -> dict[str, Any]:
        return {**self._progress(server_session_id),
                **self._live_progress(server_session_id)}

    @staticmethod
    def _missing_sequences(present: Any, total: int,
                           limit: int = 64) -> tuple[list[int], int]:
        """Return a bounded missing prefix and the exact missing count."""
        sample: list[int] = []
        missing_count = 0
        cursor = 0
        valid = sorted({int(value) for value in present if 0 <= int(value) < total})
        for seq in valid:
            if seq > cursor:
                gap = seq - cursor
                missing_count += gap
                take = min(gap, max(0, limit - len(sample)))
                sample.extend(range(cursor, cursor + take))
            cursor = max(cursor, seq + 1)
        if cursor < total:
            gap = total - cursor
            missing_count += gap
            take = min(gap, max(0, limit - len(sample)))
            sample.extend(range(cursor, cursor + take))
        return sample, missing_count

    def _range_lock(self, server_session_id: str) -> asyncio.Lock:
        lock = self._range_locks.get(server_session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._range_locks[server_session_id] = lock
        return lock

    def _canonical_partial(self, server_session_id: str) -> Path:
        return self.storage.root / "audio_cache" / f"{server_session_id}.b.pcm.part"

    def _canonical_final(self, server_session_id: str) -> Path:
        return self.storage.root / "audio_cache" / f"{server_session_id}.b.pcm"

    def _range_rows(self, server_session_id: str) -> list[Any]:
        return self.db.query(
            "SELECT start_byte,end_byte,sha256,byte_count FROM device_audio_ranges"
            " WHERE server_session_id=? ORDER BY start_byte",
            (server_session_id,))

    @staticmethod
    def _merged_coverage(rows: Any, total_bytes: int) -> list[tuple[int, int]]:
        merged: list[tuple[int, int]] = []
        for row in rows:
            start = max(0, int(row["start_byte"]))
            end = min(total_bytes, int(row["end_byte"]))
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    @staticmethod
    def _missing_byte_ranges(covered: list[tuple[int, int]], total_bytes: int,
                             *, limit: int = 64) -> tuple[list[dict[str, int]], int]:
        missing: list[dict[str, int]] = []
        missing_bytes = 0
        cursor = 0
        for start, end in covered:
            if start > cursor:
                missing_bytes += start - cursor
                block = cursor
                while block < start and len(missing) < limit:
                    length = min(RANGE_BLOCK_BYTES, start - block)
                    missing.append({"offset": block, "length": length})
                    block += length
            cursor = max(cursor, end)
        if cursor < total_bytes:
            missing_bytes += total_bytes - cursor
            block = cursor
            while block < total_bytes and len(missing) < limit:
                length = min(RANGE_BLOCK_BYTES, total_bytes - block)
                missing.append({"offset": block, "length": length})
                block += length
        return missing, missing_bytes

    def _range_progress(self, server_session_id: str, total_bytes: int) -> dict[str, Any]:
        covered = self._merged_coverage(self._range_rows(server_session_id), total_bytes)
        missing, missing_bytes = self._missing_byte_ranges(covered, total_bytes)
        return {
            "block_bytes": RANGE_BLOCK_BYTES,
            "total_bytes": total_bytes,
            "covered_bytes": total_bytes - missing_bytes,
            "covered_ranges": [{"offset": start, "length": end - start}
                               for start, end in covered[-32:]],
            "missing_ranges": missing,
            "missing_bytes": missing_bytes,
            "missing_truncated": bool(missing) and sum(r["length"] for r in missing) < missing_bytes,
            "complete": missing_bytes == 0,
        }

    def _copy_file_at(self, target: Path, source: Path, offset: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.touch()
        with target.open("r+b") as output, source.open("rb") as input_file:
            output.seek(offset)
            while True:
                block = input_file.read(RANGE_COPY_BYTES)
                if not block:
                    break
                output.write(block)
            output.flush()
            os.fsync(output.fileno())

    def _materialize_live_chunks(self, server_session_id: str) -> None:
        """Copy durable live chunks into the canonical sparse file once.

        This converts the v2 live lane into exact byte coverage, allowing a
        closed recording to switch to 10 MiB repair uploads without replaying
        audio the server already owns.
        """
        target = self._canonical_partial(server_session_id)
        chunks = self.db.query(
            "SELECT seq,sha256,start_sample,byte_count,path FROM device_audio_chunks"
            " WHERE server_session_id=? ORDER BY start_sample", (server_session_id,))
        for chunk in chunks:
            start = int(chunk["start_sample"]) * 2
            end = start + int(chunk["byte_count"])
            existing = self.db.query_one(
                "SELECT sha256,end_byte FROM device_audio_ranges"
                " WHERE server_session_id=? AND start_byte=?",
                (server_session_id, start))
            if existing:
                if existing["sha256"] != chunk["sha256"] or int(existing["end_byte"]) != end:
                    raise api_error(409, "AUDIO_RANGE_CONFLICT",
                                    "实时分片与批量覆盖区间冲突")
                continue
            overlap = self.db.query_one(
                "SELECT start_byte,end_byte FROM device_audio_ranges"
                " WHERE server_session_id=? AND start_byte < ? AND end_byte > ?",
                (server_session_id, end, start))
            if overlap:
                raise api_error(409, "AUDIO_RANGE_OVERLAP",
                                "实时分片与批量覆盖区间重叠")
            path = Path(chunk["path"])
            if not path.exists() or path.stat().st_size != int(chunk["byte_count"]):
                raise api_error(409, "LIVE_CHUNK_MISSING",
                                "服务器实时分片文件缺失，无法生成修复计划", retryable=True)
            self._copy_file_at(target, path, start)
            self.db.execute(
                "INSERT INTO device_audio_ranges(server_session_id,start_byte,end_byte,sha256,"
                "byte_count,created_at) VALUES(?,?,?,?,?,?)",
                (server_session_id, start, end, chunk["sha256"], int(chunk["byte_count"]), iso_now()))

    def cleanup_session_chunks(self, server_session_id: str) -> None:
        """完整 .b.pcm 已持久化后删除分片实体；DB 元数据保留供 ACK/审计。"""
        rows = self.db.query("SELECT path FROM device_audio_chunks WHERE server_session_id=?",
                             (server_session_id,))
        for row in rows:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError:
                logger.warning("chunk_cleanup_failed session=%s path=%s",
                               server_session_id, row["path"])
        directory = self.chunk_root / server_session_id
        if directory.exists():
            for orphan in directory.iterdir():
                if orphan.is_file():
                    try:
                        orphan.unlink()
                    except OSError:
                        pass
        try:
            directory.rmdir()
        except OSError:
            pass

    def create_session(self, body: SessionCreateInput, principal: DevicePrincipal,
                       idempotency_key: str | None) -> dict[str, Any]:
        self._check_generation(principal, body.binding_generation)
        if not CLIENT_ID_RE.fullmatch(body.client_session_id):
            raise api_error(400, "INVALID_CLIENT_SESSION_ID", "client_session_id 无效")
        audio = body.audio
        if (audio.codec, audio.sample_rate, audio.channels, audio.bits_per_sample) != (
                "pcm_s16le", 16000, 1, 16):
            raise api_error(422, "AUDIO_FORMAT_UNSUPPORTED",
                            "云端只接受 PCM S16LE/16kHz/单声道/16bit")
        configured = self.db.query_one(
            "SELECT speaker_diarization_enabled FROM devices WHERE device_id=?",
            (principal.device_id,))
        # Speaker diarization is a cloud-owned per-device policy.  Firmware
        # transports audio only; ignore the optional legacy request field so a
        # stale device cache can never override the setting selected in the
        # web device manager.  Keep the field in the schema for rolling-upgrade
        # compatibility with older firmware.
        speaker_enabled = bool(
            configured["speaker_diarization_enabled"] if configured else 1)
        payload = body.model_dump()
        payload["speaker_diarization_enabled"] = speaker_enabled
        # Transport mode may legitimately change from live to repair after a
        # device reset or network outage.  It is not part of the recording's
        # identity, so create-session idempotency must remain stable across
        # that transition.
        identity_payload = dict(payload)
        identity_payload.pop("upload_mode", None)
        payload_hash = canonical_hash(identity_payload)
        scope = f"device:{principal.device_id}:session-create"
        replay = self._idempotency_lookup(scope, idempotency_key, payload_hash)
        if replay is not None:
            # create 重放的会话身份不变，但 ACK 必须是服务器当前持久进度，
            # 否则设备重启后会从 0 重传整场录音。
            existing = self.db.query_one(
                "SELECT status,revision FROM device_sessions WHERE server_session_id=? AND device_id=?",
                (replay["server_session_id"], principal.device_id))
            if existing:
                return replay | {"status": existing["status"],
                                 "revision": int(existing["revision"]),
                                 **self._upload_ack(replay["server_session_id"])}
            raise api_error(409, "IDEMPOTENCY_STATE_MISSING", "幂等会话状态不完整")
        existing = self.db.query_one(
            "SELECT * FROM device_sessions WHERE device_id=? AND client_session_id=?",
            (principal.device_id, body.client_session_id))
        if existing:
            if (existing["owner_user_id"] != principal.owner_user_id
                    or int(existing["binding_generation"]) != principal.binding_generation):
                raise api_error(403, "SESSION_BINDING_MISMATCH", "本地会话属于旧绑定，禁止上传")
            if existing["request_hash"] and existing["request_hash"] != payload_hash:
                raise api_error(409, "CLIENT_SESSION_ID_REUSED",
                                "client_session_id 已对应不同会话内容")
            progress = self._upload_ack(existing["server_session_id"])
            response = {
                "server_session_id": existing["server_session_id"],
                "client_session_id": existing["client_session_id"],
                "status": existing["status"], "revision": int(existing["revision"]), **progress,
            }
            self._idempotency_store(scope, idempotency_key, payload_hash, response)
            return response
        server_id = "ly-" + uuid.uuid4().hex
        started = parse_time(body.started_at_utc)
        now = iso_now()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO device_sessions(server_session_id,client_session_id,request_hash,device_id,"
                    "owner_user_id,binding_generation,started_at_utc,codec,sample_rate,channels,bits_per_sample,"
                    "scene,title,source_language,target_language,upload_mode,speaker_diarization_enabled,status,revision,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'uploading',0,?,?)",
                    (server_id, body.client_session_id, payload_hash, principal.device_id,
                     principal.owner_user_id, principal.binding_generation, started, audio.codec,
                     audio.sample_rate, audio.channels, audio.bits_per_sample, body.scene,
                     body.title.strip() if body.title else None, body.source_language,
                     body.target_language, body.upload_mode,
                     1 if speaker_enabled else 0, now, now))
                conn.execute(
                    "INSERT INTO meetings(session_id,owner_user_id,created_at,state,language,summary_language,"
                    "updated_at) VALUES(?,?,?,'recording',?,?,?)",
                    (server_id, principal.owner_user_id, started, body.source_language,
                     body.target_language or "auto", now))
                if body.title and body.title.strip():
                    conn.execute("UPDATE meetings SET title=? WHERE session_id=?",
                                 (body.title.strip(), server_id))
        except sqlite3.IntegrityError:
            # 两个并发 create 只允许一个插入，输家读取胜家结果。
            existing = self.db.query_one(
                "SELECT * FROM device_sessions WHERE device_id=? AND client_session_id=?",
                (principal.device_id, body.client_session_id))
            if not existing or (existing["request_hash"] and existing["request_hash"] != payload_hash):
                raise api_error(409, "CLIENT_SESSION_ID_REUSED",
                                "client_session_id 已对应不同会话内容")
            progress = self._upload_ack(existing["server_session_id"])
            response = {"server_session_id": existing["server_session_id"],
                        "client_session_id": existing["client_session_id"],
                        "status": existing["status"], "revision": int(existing["revision"]),
                        **progress}
            self._idempotency_store(scope, idempotency_key, payload_hash, response)
            return response
        response = {
            "server_session_id": server_id, "client_session_id": body.client_session_id,
            "status": "uploading", "upload_mode": body.upload_mode,
            "revision": 0, "next_seq": 0,
            "received_chunks": 0, "total_received_chunks": 0,
            "received_samples": 0, "acknowledged_bytes": 0,
            "live_epoch": 0, "live_next_seq": 0,
            "live_received_chunks": 0, "live_received_samples": 0,
            "live_acknowledged_bytes": 0,
        }
        self._idempotency_store(scope, idempotency_key, payload_hash, response)
        return response

    async def upload_chunk(self, server_session_id: str, seq: int, data: bytes,
                           content_sha256: str | None, byte_offset: int | None,
                           byte_count: int | None,
                           principal: DevicePrincipal) -> dict[str, Any]:
        session = self._session(principal, server_session_id)
        if seq < 0 or seq > 2_000_000:
            raise api_error(400, "INVALID_CHUNK_SEQUENCE", "分片序号无效")
        if not content_sha256 or not SHA256_RE.fullmatch(content_sha256):
            raise api_error(400, "CONTENT_SHA256_REQUIRED", "缺少有效的 X-Content-SHA256")
        actual_sha = hashlib.sha256(data).hexdigest()
        if len(data) > MAX_CHUNK_BYTES:
            raise api_error(413, "AUDIO_CHUNK_TOO_LARGE", "音频分片超过服务器上限")
        if not data:
            raise api_error(422, "AUDIO_CHUNK_EMPTY", "音频分片不能为空")
        if not hmac.compare_digest(actual_sha, content_sha256.lower()):
            raise api_error(422, "AUDIO_HASH_MISMATCH", "音频分片 SHA-256 校验失败")
        if byte_count is None:
            raise api_error(400, "AUDIO_BYTE_COUNT_REQUIRED", "缺少 X-Byte-Count")
        if byte_count != len(data):
            raise api_error(422, "AUDIO_BYTE_COUNT_MISMATCH", "X-Byte-Count 与正文长度不一致")
        if byte_offset is None or byte_offset < 0 or byte_offset % 2:
            raise api_error(400, "AUDIO_OFFSET_REQUIRED", "缺少有效的 X-Byte-Offset")
        if len(data) % 2:
            raise api_error(422, "AUDIO_ALIGNMENT_INVALID", "PCM 字节数必须按 16-bit 对齐")
        start_sample = byte_offset // 2
        sample_count = len(data) // 2
        if start_sample < 0 or sample_count < 0:
            raise api_error(422, "AUDIO_LAYOUT_INVALID", "分片采样位置或长度无效")
        existing = self.db.query_one(
            "SELECT * FROM device_audio_chunks WHERE server_session_id=? AND seq=?",
            (server_session_id, seq))
        if existing:
            if (existing["sha256"] != actual_sha or int(existing["start_sample"]) != start_sample
                    or int(existing["sample_count"]) != sample_count):
                raise api_error(409, "AUDIO_CHUNK_CONFLICT", "相同序号已对应不同音频内容")
            return {"accepted": True, "duplicate": True, "seq": seq,
                    **self._upload_ack(server_session_id)}
        if session["status"] != "uploading":
            raise api_error(409, "SESSION_NOT_UPLOADING", "会话已结束，不能增加新分片")
        overlap = self.db.query_one(
            "SELECT seq FROM device_audio_chunks WHERE server_session_id=?"
            " AND start_sample < ? AND (start_sample + sample_count) > ?",
            (server_session_id, start_sample + sample_count, start_sample))
        if overlap:
            raise api_error(409, "AUDIO_RANGE_OVERLAP", "音频分片采样范围重叠")
        # Capture the durable cursor before inserting this request.  This is
        # what prevents replaying old chunks after a process restart.
        runtime = await self._ensure_live_runtime(session)
        directory = self.chunk_root / server_session_id
        directory.mkdir(parents=True, exist_ok=True)
        # 每个并发上传使用独立终文件；DB 唯一约束败的请求只删自己的文件，
        # 不会误删已经获胜并被 DB 引用的分片。
        path = directory / f"{seq:08d}-{uuid.uuid4().hex}.pcm"
        durable_write(path, data)
        now = iso_now()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO device_audio_chunks(server_session_id,seq,sha256,start_sample,"
                    "sample_count,byte_count,path,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (server_session_id, seq, actual_sha, start_sample, sample_count, len(data),
                     str(path), now))
                conn.execute("UPDATE device_sessions SET updated_at=?"
                             " WHERE server_session_id=?", (now, server_session_id))
        except sqlite3.IntegrityError:
            path.unlink(missing_ok=True)
            return await self.upload_chunk(server_session_id, seq, data, content_sha256,
                                           byte_offset, byte_count, principal)
        await self._feed_live(server_session_id, runtime)
        return {"accepted": True, "duplicate": False, "seq": seq,
                **self._upload_ack(server_session_id)}

    async def resume_live(self, server_session_id: str, body: LiveResumeInput,
                          principal: DevicePrincipal,
                          idempotency_key: str | None) -> dict[str, Any]:
        """Start a new realtime epoch after a locally recorded network gap."""
        self._check_generation(principal, body.binding_generation)
        if body.gap_start_bytes % 2 or body.resume_offset_bytes % 2:
            raise api_error(422, "AUDIO_ALIGNMENT_INVALID",
                            "实时恢复位置必须按16-bit PCM对齐")
        if body.resume_offset_bytes < body.gap_start_bytes:
            raise api_error(422, "LIVE_RESUME_RANGE_INVALID",
                            "恢复位置不能早于断网缺口起点")
        session = self._session(principal, server_session_id)
        if session["status"] != "uploading":
            raise api_error(409, "SESSION_NOT_UPLOADING", "会话不能恢复实时上传")
        payload_hash = canonical_hash(body.model_dump())
        scope = f"device:{principal.device_id}:session:{server_session_id}:live-resume"
        replay = self._idempotency_lookup(scope, idempotency_key, payload_hash)
        if replay is not None:
            return replay | self._upload_ack(server_session_id)

        async with self._range_lock(server_session_id):
            current = self._live_progress(server_session_id)
            durable_tail = int(current["live_acknowledged_bytes"])
            gap_start = max(body.gap_start_bytes, durable_tail)
            if gap_start > body.resume_offset_bytes:
                raise api_error(409, "LIVE_RESUME_BEHIND_SERVER",
                                "服务器已接收的实时音频超过请求恢复位置",
                                extra={"live_acknowledged_bytes": durable_tail})
            existing = self.db.query_one(
                "SELECT epoch,start_seq,start_byte,gap_start_byte FROM device_live_epochs"
                " WHERE server_session_id=? AND start_byte=? AND gap_start_byte=?",
                (server_session_id, body.resume_offset_bytes, gap_start))
            if existing is None:
                await self._rotate_live(server_session_id)
                row = self.db.query_one(
                    "SELECT COALESCE(MAX(seq),-1) AS max_seq FROM device_audio_chunks"
                    " WHERE server_session_id=?", (server_session_id,))
                start_seq = int(row["max_seq"]) + 1
                row = self.db.query_one(
                    "SELECT COALESCE(MAX(epoch),0) AS max_epoch FROM device_live_epochs"
                    " WHERE server_session_id=?", (server_session_id,))
                epoch = int(row["max_epoch"]) + 1
                self.db.execute(
                    "INSERT INTO device_live_epochs(server_session_id,epoch,start_seq,start_byte,"
                    "gap_start_byte,created_at) VALUES(?,?,?,?,?,?)",
                    (server_session_id, epoch, start_seq, body.resume_offset_bytes,
                     gap_start, iso_now()))
                if body.resume_offset_bytes > gap_start:
                    self.storage.add_gap(
                        server_session_id,
                        gap_start // 2 * 1000 // 16000,
                        body.resume_offset_bytes // 2 * 1000 // 16000)
            self._live_disabled.discard(server_session_id)
            session = self._session(principal, server_session_id)
            await self._ensure_live_runtime(session)
            ack = self._upload_ack(server_session_id)
        response = {
            "server_session_id": server_session_id,
            "resumed": True,
            "gap_start_bytes": gap_start,
            "gap_end_bytes": body.resume_offset_bytes,
            "gap_pending": body.resume_offset_bytes > gap_start,
            **ack,
        }
        self._idempotency_store(scope, idempotency_key, payload_hash, response)
        logger.info(
            "device_live_resumed session=%s epoch=%d gap=[%d,%d) next_seq=%d",
            server_session_id, response["live_epoch"], gap_start,
            body.resume_offset_bytes, response["live_next_seq"])
        return response

    async def defer_session(self, server_session_id: str, body: SessionDeferInput,
                            principal: DevicePrincipal,
                            idempotency_key: str | None) -> dict[str, Any]:
        """Close realtime ASR while keeping missing audio for manual repair."""
        self._check_generation(principal, body.binding_generation)
        if body.total_bytes != body.total_samples * 2 or body.total_bytes % 2:
            raise api_error(422, "AUDIO_TOTAL_MISMATCH",
                            "total_bytes 必须等于 total_samples * 2")
        payload_hash = canonical_hash(body.model_dump())
        scope = f"device:{principal.device_id}:session:{server_session_id}:defer"
        replay = self._idempotency_lookup(scope, idempotency_key, payload_hash)
        if replay is not None:
            return replay
        session = self._session(principal, server_session_id)
        if session["status"] == "awaiting_repair":
            progress = self._range_progress(server_session_id, body.total_bytes)
            response = {"server_session_id": server_session_id,
                        "status": "awaiting_repair", **progress}
            self._idempotency_store(scope, idempotency_key, payload_hash, response)
            return response
        if session["status"] != "uploading":
            raise api_error(409, "SESSION_NOT_UPLOADING", "会话不能进入待补传状态")
        async with self._range_lock(server_session_id):
            self._materialize_live_chunks(server_session_id)
            progress = self._range_progress(server_session_id, body.total_bytes)
            await self._rotate_live(server_session_id)
            ended = parse_time(body.ended_at_utc)
            now = iso_now()
            end_ms = body.total_samples * 1000 // 16000
            mode = "repair" if progress["covered_bytes"] else "bulk"
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE device_sessions SET status='awaiting_repair',upload_mode=?,"
                    "ended_at_utc=?,canonical_total_bytes=?,expected_samples=?,updated_at=?"
                    " WHERE server_session_id=?",
                    (mode, ended, body.total_bytes, body.total_samples, now,
                     server_session_id))
                conn.execute(
                    "UPDATE meetings SET state='suspended',audio_end_ms=?,updated_at=?"
                    " WHERE session_id=?", (end_ms, now, server_session_id))
            covered = self._merged_coverage(self._range_rows(server_session_id),
                                             body.total_bytes)
            cursor = 0
            for start, end in covered + [(body.total_bytes, body.total_bytes)]:
                if start > cursor:
                    self.storage.add_gap(server_session_id,
                                         cursor // 2 * 1000 // 16000,
                                         start // 2 * 1000 // 16000)
                cursor = max(cursor, end)
        response = {"server_session_id": server_session_id,
                    "status": "awaiting_repair", **progress}
        self._idempotency_store(scope, idempotency_key, payload_hash, response)
        logger.info("device_session_deferred session=%s missing_bytes=%d",
                    server_session_id, progress["missing_bytes"])
        return response

    async def upload_plan(self, server_session_id: str, body: UploadPlanInput,
                          principal: DevicePrincipal) -> dict[str, Any]:
        self._check_generation(principal, body.binding_generation)
        if body.total_bytes != body.total_samples * 2 or body.total_bytes % 2:
            raise api_error(422, "AUDIO_TOTAL_MISMATCH",
                            "total_bytes 必须等于 total_samples * 2")
        session = self._session(principal, server_session_id)
        if session["status"] in {"processing", "done"}:
            return {"server_session_id": server_session_id, "mode": body.mode,
                    **self._range_progress(server_session_id, body.total_bytes),
                    "complete": True, "status": session["status"]}
        if session["status"] not in {"uploading", "awaiting_repair"}:
            raise api_error(409, "SESSION_NOT_UPLOADING", "会话不能进入批量上传")
        existing_total = session["canonical_total_bytes"]
        if existing_total is not None and int(existing_total) != body.total_bytes:
            raise api_error(409, "AUDIO_TOTAL_CONFLICT", "本地录音总长度与已有上传计划不一致")
        async with self._range_lock(server_session_id):
            self._materialize_live_chunks(server_session_id)
            self.db.execute(
                "UPDATE device_sessions SET upload_mode=?,canonical_total_bytes=?,expected_samples=?,"
                "updated_at=? WHERE server_session_id=?",
                (body.mode, body.total_bytes, body.total_samples, iso_now(), server_session_id))
            progress = self._range_progress(server_session_id, body.total_bytes)
        await self._notify_audio_range_committed(server_session_id)
        return {"server_session_id": server_session_id, "mode": body.mode,
                "status": "uploading", **progress}

    async def upload_range(self, server_session_id: str, request: Request,
                           content_sha256: str | None, byte_offset: int | None,
                           byte_count: int | None, principal: DevicePrincipal) -> dict[str, Any]:
        session = self._session(principal, server_session_id)
        if session["status"] not in {"uploading", "awaiting_repair"}:
            raise api_error(409, "SESSION_NOT_UPLOADING", "会话已结束，不能增加音频范围")
        total = session["canonical_total_bytes"]
        if total is None:
            raise api_error(409, "UPLOAD_PLAN_REQUIRED", "请先请求 upload-plan")
        total = int(total)
        if byte_offset is None or byte_offset < 0 or byte_offset % 2:
            raise api_error(400, "AUDIO_OFFSET_REQUIRED", "缺少有效的 X-Byte-Offset")
        if byte_count is None or byte_count <= 0 or byte_count > MAX_RANGE_BYTES or byte_count % 2:
            raise api_error(400, "AUDIO_BYTE_COUNT_INVALID", "范围长度必须为不超过10MiB的偶数")
        if byte_offset + byte_count > total:
            raise api_error(422, "AUDIO_RANGE_OUT_OF_BOUNDS", "音频范围超出本地录音总长度")
        if not content_sha256 or not SHA256_RE.fullmatch(content_sha256):
            raise api_error(400, "CONTENT_SHA256_REQUIRED", "缺少有效的 X-Content-SHA256")
        declared = request.headers.get("content-length")
        if declared is None or not declared.isdigit() or int(declared) != byte_count:
            raise api_error(411, "CONTENT_LENGTH_REQUIRED", "Content-Length 必须等于 X-Byte-Count")
        existing = self.db.query_one(
            "SELECT end_byte,sha256 FROM device_audio_ranges"
            " WHERE server_session_id=? AND start_byte=?",
            (server_session_id, byte_offset))
        if existing:
            if int(existing["end_byte"]) != byte_offset + byte_count or not hmac.compare_digest(
                    existing["sha256"], content_sha256.lower()):
                raise api_error(409, "AUDIO_RANGE_CONFLICT", "相同偏移已经对应不同音频内容")
            progress = self._range_progress(server_session_id, total)
            await self._notify_audio_range_committed(server_session_id)
            return {"accepted": True, "duplicate": True, "offset": byte_offset,
                    "length": byte_count, **progress}

        directory = self.range_root / server_session_id
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / f"{byte_offset:016x}-{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        received = 0
        try:
            with temp.open("wb") as output:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > byte_count:
                        raise api_error(413, "AUDIO_RANGE_TOO_LARGE", "请求正文超过 X-Byte-Count")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != byte_count:
                raise api_error(422, "AUDIO_BYTE_COUNT_MISMATCH", "上传范围长度不完整", retryable=True,
                                extra={"received_bytes": received})
            actual_sha = digest.hexdigest()
            if not hmac.compare_digest(actual_sha, content_sha256.lower()):
                raise api_error(422, "AUDIO_HASH_MISMATCH", "音频范围 SHA-256 校验失败")
            async with self._range_lock(server_session_id):
                session = self._session(principal, server_session_id)
                if session["status"] not in {"uploading", "awaiting_repair"}:
                    raise api_error(409, "SESSION_NOT_UPLOADING", "会话已结束")
                existing = self.db.query_one(
                    "SELECT end_byte,sha256 FROM device_audio_ranges"
                    " WHERE server_session_id=? AND start_byte=?",
                    (server_session_id, byte_offset))
                if existing:
                    if int(existing["end_byte"]) != byte_offset + byte_count or existing["sha256"] != actual_sha:
                        raise api_error(409, "AUDIO_RANGE_CONFLICT", "相同偏移已经对应不同音频内容")
                    duplicate = True
                else:
                    overlap = self.db.query_one(
                        "SELECT start_byte,end_byte FROM device_audio_ranges"
                        " WHERE server_session_id=? AND start_byte < ? AND end_byte > ?",
                        (server_session_id, byte_offset + byte_count, byte_offset))
                    if overlap:
                        raise api_error(409, "AUDIO_RANGE_OVERLAP", "上传范围与已有音频重叠")
                    self._copy_file_at(self._canonical_partial(server_session_id), temp, byte_offset)
                    with self.db.transaction() as conn:
                        conn.execute(
                            "INSERT INTO device_audio_ranges(server_session_id,start_byte,end_byte,sha256,"
                            "byte_count,created_at) VALUES(?,?,?,?,?,?)",
                            (server_session_id, byte_offset, byte_offset + byte_count,
                             actual_sha, byte_count, iso_now()))
                        conn.execute("UPDATE device_sessions SET updated_at=? WHERE server_session_id=?",
                                     (iso_now(), server_session_id))
                    duplicate = False
                progress = self._range_progress(server_session_id, total)
            await self._notify_audio_range_committed(server_session_id)
            return {"accepted": True, "duplicate": duplicate, "offset": byte_offset,
                    "length": byte_count, **progress}
        except ClientDisconnect:
            logger.warning(
                "device_range_client_disconnected session=%s offset=%d expected=%d received=%d",
                server_session_id, byte_offset, byte_count, received)
            raise api_error(
                408, "AUDIO_UPLOAD_INTERRUPTED", "上传连接在范围完整到达前中断",
                retryable=True, extra={"received_bytes": received,
                                       "expected_bytes": byte_count})
        finally:
            temp.unlink(missing_ok=True)

    async def complete_ranges(self, server_session_id: str, body: RangeCompleteInput,
                              principal: DevicePrincipal,
                              idempotency_key: str | None) -> dict[str, Any]:
        self._check_generation(principal, body.binding_generation)
        if body.total_bytes != body.total_samples * 2 or body.total_bytes % 2:
            raise api_error(422, "AUDIO_TOTAL_MISMATCH",
                            "total_bytes 必须等于 total_samples * 2")
        if body.file_sha256 and not SHA256_RE.fullmatch(body.file_sha256):
            raise api_error(400, "FILE_SHA256_INVALID", "file_sha256 必须为64位十六进制")
        payload_hash = canonical_hash(body.model_dump())
        scope = f"device:{principal.device_id}:session:{server_session_id}:range-complete"
        replay = self._idempotency_lookup(scope, idempotency_key, payload_hash)
        if replay is not None:
            return replay
        session = self._session(principal, server_session_id)
        if session["status"] in {"processing", "done", "failed"}:
            response = {"server_session_id": server_session_id, "status": session["status"],
                        "complete": True, "missing_ranges": [], "missing_bytes": 0,
                        "revision": int(session["revision"])}
            self._idempotency_store(scope, idempotency_key, payload_hash, response)
            return response
        if session["status"] not in {"uploading", "awaiting_repair"}:
            raise api_error(409, "SESSION_NOT_UPLOADING", "会话不能完成批量上传")
        async with self._range_lock(server_session_id):
            self._materialize_live_chunks(server_session_id)
            progress = self._range_progress(server_session_id, body.total_bytes)
            if progress["missing_bytes"]:
                raise api_error(409, "AUDIO_RANGES_MISSING", "完整录音仍有缺失范围", retryable=True,
                                extra={"missing_ranges": progress["missing_ranges"],
                                       "missing_bytes": progress["missing_bytes"],
                                       "missing_truncated": progress["missing_truncated"]})
            partial = self._canonical_partial(server_session_id)
            if not partial.exists() or partial.stat().st_size != body.total_bytes:
                raise api_error(409, "CANONICAL_AUDIO_INCOMPLETE", "权威音频文件长度不完整",
                                retryable=True)
            actual_file_sha = None
            if body.file_sha256:
                digest = hashlib.sha256()
                with partial.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                actual_file_sha = digest.hexdigest()
                if not hmac.compare_digest(actual_file_sha, body.file_sha256.lower()):
                    raise api_error(422, "FILE_HASH_MISMATCH", "完整录音 SHA-256 校验失败")
            await self._finish_live(server_session_id, body.total_samples)
            final = self._canonical_final(server_session_id)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(partial, final)
            ended = parse_time(body.ended_at_utc)
            now = iso_now()
            end_ms = body.total_samples * 1000 // 16000
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE device_sessions SET status='processing',ended_at_utc=?,expected_chunks=NULL,"
                    "expected_samples=?,canonical_total_bytes=?,canonical_sha256=?,updated_at=?"
                    " WHERE server_session_id=?",
                    (ended, body.total_samples, body.total_bytes,
                     actual_file_sha or body.file_sha256, now, server_session_id))
                conn.execute(
                    "UPDATE meetings SET state='finalizing',audio_end_ms=?,updated_at=? WHERE session_id=?",
                    (end_ms, now, server_session_id))
            self.cleanup_session_chunks(server_session_id)
            if self.on_session_complete is not None:
                try:
                    await self.on_session_complete(server_session_id, end_ms)
                except Exception:
                    logger.exception("device_range_enqueue_failed session=%s", server_session_id)
                    failed_at = iso_now()
                    with self.db.transaction() as conn:
                        conn.execute(
                            "UPDATE device_sessions SET status='failed',revision=revision+1,"
                            "failure_code='PROCESSING_ENQUEUE_FAILED',"
                            "failure_message='云端处理任务未能启动，录音文件仍保留',updated_at=?"
                            " WHERE server_session_id=? AND status='processing'",
                            (failed_at, server_session_id))
                        conn.execute("UPDATE meetings SET state='done',updated_at=? WHERE session_id=?",
                                     (failed_at, server_session_id))
        row = self.db.query_one("SELECT status,revision FROM device_sessions WHERE server_session_id=?",
                                (server_session_id,))
        response = {"server_session_id": server_session_id, "status": row["status"],
                    "complete": True, "missing_ranges": [], "missing_bytes": 0,
                    "revision": int(row["revision"])}
        self._idempotency_store(scope, idempotency_key, payload_hash, response)
        return response

    async def cancel_session(self, server_session_id: str, body: SessionCancelInput,
                             principal: DevicePrincipal) -> dict[str, Any]:
        self._check_generation(principal, body.binding_generation)
        async with self._range_lock(server_session_id):
            session = self._session(principal, server_session_id)
            if session["status"] in {"processing", "done"}:
                return {"server_session_id": server_session_id,
                        "status": session["status"], "cancelled": False,
                        "canonical_audio_accepted": True}
            if session["status"] == "cancelled":
                return {"server_session_id": server_session_id,
                        "status": "cancelled", "cancelled": True}
            await self._abort_live(server_session_id)
            self.cleanup_session_chunks(server_session_id)
            for path in (self._canonical_partial(server_session_id),
                         self._canonical_final(server_session_id)):
                path.unlink(missing_ok=True)
            directory = self.range_root / server_session_id
            if directory.exists():
                for path in directory.iterdir():
                    if path.is_file():
                        path.unlink(missing_ok=True)
                with suppress(OSError):
                    directory.rmdir()
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM device_audio_chunks WHERE server_session_id=?",
                             (server_session_id,))
                conn.execute("DELETE FROM device_audio_ranges WHERE server_session_id=?",
                             (server_session_id,))
                conn.execute("DELETE FROM meetings WHERE session_id=?", (server_session_id,))
                conn.execute(
                    "UPDATE device_sessions SET status='cancelled',failure_code='DEVICE_CANCELLED',"
                    "failure_message=?,revision=revision+1,updated_at=? WHERE server_session_id=?",
                    (body.reason, iso_now(), server_session_id))
            return {"server_session_id": server_session_id,
                    "status": "cancelled", "cancelled": True}

    def put_mark(self, server_session_id: str, client_mark_id: str, body: MarkInput,
                 principal: DevicePrincipal) -> dict[str, Any]:
        session = self._session(principal, server_session_id)
        if not CLIENT_ID_RE.fullmatch(client_mark_id):
            raise api_error(400, "INVALID_MARK_ID", "标记编号无效")
        existing = self.db.query_one(
            "SELECT * FROM device_session_marks WHERE server_session_id=? AND client_mark_id=?",
            (server_session_id, client_mark_id))
        if existing:
            if (int(existing["offset_samples"]) != body.offset_samples
                    or existing["kind"] != body.kind or existing["label"] != body.label):
                raise api_error(409, "MARK_CONFLICT", "相同标记编号对应了不同内容")
            revision = self.db.query_one(
                "SELECT revision FROM device_sessions WHERE server_session_id=?",
                (server_session_id,))["revision"]
            if int(revision) < 1:
                # Heal marks written by an older server build whose ACK cursor
                # incorrectly stayed at zero.
                self.db.execute(
                    "UPDATE device_sessions SET revision=1,updated_at=?"
                    " WHERE server_session_id=? AND revision<1",
                    (iso_now(), server_session_id))
                revision = self.db.query_one(
                    "SELECT revision FROM device_sessions WHERE server_session_id=?",
                    (server_session_id,))["revision"]
            return {"accepted": True, "duplicate": True, "revision": int(revision)}
        if session["status"] != "uploading":
            raise api_error(409, "SESSION_NOT_UPLOADING",
                            "会话已结束，只允许重放已有标记")
        now = iso_now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO device_session_marks(server_session_id,client_mark_id,offset_samples,kind,"
                "label,created_at) VALUES(?,?,?,?,?,?)",
                (server_session_id, client_mark_id, body.offset_samples, body.kind, body.label, now))
            conn.execute("UPDATE device_sessions SET revision=revision+1,updated_at=?"
                         " WHERE server_session_id=?", (now, server_session_id))
            revision = conn.execute("SELECT revision FROM device_sessions WHERE server_session_id=?",
                                    (server_session_id,)).fetchone()["revision"]
        if self.on_mark is not None:
            task = asyncio.create_task(self.on_mark(server_session_id))
            self._callback_tasks.add(task)
            task.add_done_callback(lambda completed: self._callback_tasks.discard(completed))
        return {"accepted": True, "duplicate": False, "revision": int(revision)}

    async def end_session(self, server_session_id: str, body: SessionEndInput,
                          principal: DevicePrincipal,
                          idempotency_key: str | None) -> dict[str, Any]:
        self._check_generation(principal, body.binding_generation)
        session = self._session(principal, server_session_id)
        payload_hash = canonical_hash(body.model_dump())
        scope = f"device:{principal.device_id}:session:{server_session_id}:end"
        replay = self._idempotency_lookup(scope, idempotency_key, payload_hash)
        if replay is not None:
            return replay
        rows = self.db.query(
            "SELECT * FROM device_audio_chunks WHERE server_session_id=? ORDER BY seq",
            (server_session_id,))
        by_seq = {int(row["seq"]): row for row in rows}
        missing, missing_count = self._missing_sequences(by_seq, body.total_chunks)
        if any(seq >= body.total_chunks for seq in by_seq):
            raise api_error(422, "AUDIO_CHUNK_OUT_OF_RANGE", "收到超出 total_chunks 的分片")
        now = iso_now()
        self.db.execute(
            "UPDATE device_sessions SET expected_chunks=?,expected_samples=?,updated_at=?"
            " WHERE server_session_id=?",
            (body.total_chunks, body.total_samples, now, server_session_id))
        if missing_count:
            raise api_error(409, "AUDIO_CHUNKS_MISSING", "音频仍有缺片", retryable=True,
                            extra={"missing_sequences": missing,
                                   "missing_count": missing_count,
                                   "missing_truncated": missing_count > len(missing)})
        cursor = 0
        for seq in range(body.total_chunks):
            row = by_seq[seq]
            if int(row["start_sample"]) != cursor:
                raise api_error(422, "AUDIO_LAYOUT_INVALID", "音频分片不连续",
                                extra={"sequence": seq, "expected_start_sample": cursor})
            cursor += int(row["sample_count"])
        if cursor != body.total_samples:
            raise api_error(422, "AUDIO_TOTAL_SAMPLES_MISMATCH", "总采样数与分片不一致",
                            extra={"received_samples": cursor})
        await self._finish_live(server_session_id, body.total_samples)
        if session["status"] == "uploading":
            output = self.storage.root / "audio_cache" / f"{server_session_id}.b.pcm"
            output.parent.mkdir(parents=True, exist_ok=True)
            temp = output.with_suffix(f".{uuid.uuid4().hex}.tmp")
            with temp.open("wb") as target:
                for seq in range(body.total_chunks):
                    with Path(by_seq[seq]["path"]).open("rb") as source:
                        while True:
                            block = source.read(256 * 1024)
                            if not block:
                                break
                            target.write(block)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, output)
            ended = parse_time(body.ended_at_utc)
            end_ms = body.total_samples * 1000 // 16000
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE device_sessions SET status='processing',ended_at_utc=?,expected_chunks=?,"
                    "expected_samples=?,updated_at=? WHERE server_session_id=?",
                    (ended, body.total_chunks, body.total_samples, now, server_session_id))
                conn.execute(
                    "UPDATE meetings SET state='finalizing',audio_end_ms=?,updated_at=? WHERE session_id=?",
                    (end_ms, now, server_session_id))
            self.cleanup_session_chunks(server_session_id)
            if self.on_session_complete is not None:
                try:
                    await self.on_session_complete(server_session_id, end_ms)
                except Exception:
                    # The complete PCM file is durable, but no processing job
                    # was durably accepted.  Return an explicit terminal state
                    # instead of stranding firmware in `processing` forever.
                    logger.exception("device_session_enqueue_failed session=%s",
                                     server_session_id)
                    failed_at = iso_now()
                    with self.db.transaction() as conn:
                        conn.execute(
                            "UPDATE device_sessions SET status='failed',revision=revision+1,"
                            "failure_code='PROCESSING_ENQUEUE_FAILED',"
                            "failure_message='云端处理任务未能启动，录音文件仍保留',updated_at=?"
                            " WHERE server_session_id=? AND status='processing'",
                            (failed_at, server_session_id))
                        conn.execute(
                            "UPDATE meetings SET state='done',updated_at=? WHERE session_id=?",
                            (failed_at, server_session_id))
        row = self.db.query_one("SELECT revision,status FROM device_sessions WHERE server_session_id=?",
                                (server_session_id,))
        response = {"server_session_id": server_session_id, "status": row["status"],
                    "missing_sequences": [], "missing_count": 0,
                    "missing_truncated": False, "revision": int(row["revision"])}
        self._idempotency_store(scope, idempotency_key, payload_hash, response)
        return response

    def session_state(self, server_session_id: str, principal: DevicePrincipal,
                      after_revision: int) -> dict[str, Any]:
        session = self._session(principal, server_session_id)
        meeting_state = self.storage.get_state(server_session_id)
        if meeting_state == "done" and session["status"] not in {"done", "failed"}:
            self.db.execute("UPDATE device_sessions SET status='done',revision=revision+1,updated_at=?"
                            " WHERE server_session_id=? AND status!='done'",
                            (iso_now(), server_session_id))
            session = self._session(principal, server_session_id)
        progress = self._upload_ack(server_session_id)
        expected_chunks = session["expected_chunks"]
        missing_all: list[int] = []
        missing_count = 0
        if expected_chunks is not None:
            present = {int(row["seq"]) for row in self.db.query(
                "SELECT seq FROM device_audio_chunks WHERE server_session_id=?", (server_session_id,))}
            missing_all, missing_count = self._missing_sequences(present, int(expected_chunks))
        missing = missing_all
        revision = int(session["revision"])
        changed = revision > max(0, after_revision)
        segments = ([seg for seg in self.storage.load_segments(server_session_id)
                     if int(seg.get("revision") or 0) > max(0, after_revision)][-16:]
                    if changed else [])
        captions = [{
            "seg_id": device_text(seg["seg_id"], 63),
            "start_ms": int(seg.get("start_ms") or 0),
            "end_ms": int(seg.get("end_ms") or 0), "text": device_text(seg.get("text"), 255),
            "speaker_label": device_text(seg.get("speaker_label"), 63) or None,
            "revision": int(seg.get("revision") or revision),
        } for seg in segments if seg.get("text")]
        translations = [{
            "seg_id": device_text(seg["seg_id"], 63),
            "source_text": device_text(seg.get("text"), 255),
            "translated_text": device_text(seg.get("translation"), 383),
            "source_language": device_text(session["source_language"] or "auto", 15),
            "target_language": device_text(session["target_language"] or "", 15),
            "revision": int(seg.get("revision") or revision),
        } for seg in segments if seg.get("translation")]
        marks = [{"id": row["client_mark_id"],
                  "at_ms": int(row["offset_samples"]) * 1000 // 16000,
                  "kind": row["kind"], "label": row["label"]}
                 for row in self.db.query(
                     "SELECT client_mark_id,offset_samples,kind,label FROM device_session_marks"
                     " WHERE server_session_id=? ORDER BY offset_samples DESC LIMIT 16",
                     (server_session_id,))]
        marks.reverse()
        summary = (self.storage.get_meeting(server_session_id) or {}).get("summary") or {}
        chapters = summary.get("timeline_chapters") or []
        latest_chapter = chapters[-1] if isinstance(chapters, list) and chapters else None
        timeline = None
        if isinstance(latest_chapter, dict):
            # The chapter grows as the discussion advances.  The recorder has
            # room for two bullets, so expose the two newest points instead of
            # pinning the display to the first two points for the whole chapter.
            all_points = [clean for point in (latest_chapter.get("items") or [])
                          if (clean := device_text(point, 159))]
            points = all_points[-2:]
            timeline = {
                "schema": int(summary.get("timeline_schema") or 1),
                "chapter_no": max(1, int(latest_chapter.get("chapter_no") or len(chapters))),
                "start_ms": max(0, int(latest_chapter.get("start_ms") or 0)),
                "end_ms": max(0, int(latest_chapter.get("end_ms") or 0)),
                "title": device_text(latest_chapter.get("title"), 95),
                "items": points,
                "status": device_text(latest_chapter.get("status"), 15) or "current",
                "mark_count": max(0, int(latest_chapter.get("mark_count") or 0)),
            }
        speaker_rows = self.db.query(
            "SELECT speaker_id,COUNT(*) AS n FROM segments WHERE session_id=?"
            " AND speaker_id IS NOT NULL GROUP BY speaker_id ORDER BY speaker_id",
            (server_session_id,))
        response = {
            "changed": changed,
            "client_session_id": session["client_session_id"],
            "server_session_id": server_session_id,
            "revision": revision, "status": session["status"], "scene": session["scene"],
            "title": device_text(session["title"], 127) or None,
            "source_language": device_text(session["source_language"] or "auto", 15),
            "target_language": device_text(session["target_language"] or "", 15),
            "upload": {**progress, "total_chunks": expected_chunks,
                       "total_samples": session["expected_samples"],
                       "missing_sequences": missing,
                       "missing_count": missing_count,
                       "missing_truncated": missing_count > len(missing)},
            "captions": captions, "translations": translations, "marks": marks,
            "timeline": timeline,
            "speaker": {
                "enabled": bool(session["speaker_diarization_enabled"]),
                "labeled_segments": sum(int(row["n"]) for row in speaker_rows),
                "speaker_count": len(speaker_rows),
                "labels": [str(row["speaker_id"]) for row in speaker_rows],
            },
            "cloud_processing": (self.offline_progress(server_session_id)
                                 if self.offline_progress is not None else None),
            "missing_sequences": missing, "missing_count": missing_count,
            "missing_truncated": missing_count > len(missing),
            "error": ({"code": device_text(session["failure_code"], 63),
                       "message": device_text(session["failure_message"], 191)}
                      if session["status"] == "failed" else None),
        }
        while len(json.dumps(response, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")) > 8191:
            if captions:
                # Drop the oldest caption and its translation as one display
                # unit, preserving complete newest pairs instead of returning
                # orphan translations.
                oldest = captions.pop(0)["seg_id"]
                translations[:] = [item for item in translations
                                   if item["seg_id"] != oldest]
            elif translations:
                translations.pop(0)
            elif missing:
                missing.pop()
            else:
                raise api_error(500, "SESSION_STATE_TOO_LARGE",
                                "会话状态超过设备缓冲区", retryable=True)
        return response

    def agenda_revision(self, owner_user_id: str) -> int:
        row = self.db.query_one("SELECT revision FROM agenda_revisions WHERE owner_user_id=?",
                                (owner_user_id,))
        return int(row["revision"]) if row else 0

    def bump_agenda_revision(self, owner_user_id: str) -> int:
        now = iso_now()
        self.db.execute(
            "INSERT INTO agenda_revisions(owner_user_id,revision,updated_at) VALUES(?,1,?)"
            " ON CONFLICT(owner_user_id) DO UPDATE SET revision=revision+1,updated_at=excluded.updated_at",
            (owner_user_id, now))
        return self.agenda_revision(owner_user_id)

    def agenda_snapshot(self, principal: DevicePrincipal, after_revision: int,
                        window_days: int) -> dict[str, Any]:
        days = max(1, min(31, int(window_days)))
        revision = self.agenda_revision(principal.owner_user_id)
        now = utcnow()
        base = {"revision": revision, "binding_generation": principal.binding_generation,
                "server_time_utc": int(now.timestamp()), "timezone_offset_minutes": 480,
                "timezone": DEFAULT_TZ}
        if revision <= max(0, after_revision):
            return base | {"items": []}
        store = AgendaStore(self.storage)
        local_tz = timezone(timedelta(hours=8))
        today = now.astimezone(local_tz).date()
        projected: dict[str, dict[str, Any]] = {}
        completed_event_ids = {row["source_event_id"] for row in self.db.query(
            "SELECT source_event_id FROM agenda_todos WHERE owner=? AND done=1"
            " AND source_event_id IS NOT NULL", (principal.owner_user_id,))}
        for offset in range(days):
            snapshot = store.today(principal.owner_user_id,
                                   (today + timedelta(days=offset)).isoformat(), DEFAULT_TZ)
            reminder_by_event = {item["event_id"]: item for item in snapshot["reminders"]}
            for event in snapshot["events"]:
                if event["id"] in completed_event_ids:
                    continue
                start = datetime.fromisoformat(event["start"]).astimezone(timezone.utc)
                if start < now - timedelta(minutes=5):
                    continue
                reminder = reminder_by_event.get(event["id"])
                reminder_epoch = 0
                if reminder and reminder.get("remind_at"):
                    reminder_epoch = int(datetime.fromisoformat(
                        reminder["remind_at"]).astimezone(timezone.utc).timestamp())
                    reminder_epoch = min(reminder_epoch, int(start.timestamp()))
                occurrence_key = f"{event['id']}:{event['start']}"
                item_id = event["id"] if not event.get("recurrence_rule") else hashlib.sha1(
                    occurrence_key.encode()).hexdigest()[:32]
                projected[occurrence_key] = {
                    "id": device_text(item_id, 47),
                    "title": device_text(event["title"], 71),
                    "display_time": start.astimezone(local_tz).strftime("%m-%d %H:%M"),
                    "start_utc": int(start.timestamp()),
                    "reminder_utc": reminder_epoch,
                    "has_time": True,
                }
        # 仅投影没有 source_event_id 的独立待办；事件派生待办已由 event 表示。
        # 无截止时间的语音待办也必须下发，否则它虽已入库却永远不会出现在设备上。
        for todo in self.db.query(
                "SELECT * FROM agenda_todos WHERE owner=?"
                " AND source_event_id IS NULL AND done=0"
                " ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,due_at,id",
                (principal.owner_user_id,)):
            due = todo["due_at"]
            start = datetime.fromisoformat(due).astimezone(timezone.utc) if due else None
            if start and (start < now - timedelta(minutes=5)
                          or start >= now + timedelta(days=days)):
                continue
            projected[f"todo:{todo['id']}"] = {
                "id": device_text(todo["id"], 47), "title": device_text(todo["text"], 71),
                "display_time": (start.astimezone(local_tz).strftime("%m-%d %H:%M")
                                 if start else "未定时间"),
                "start_utc": int(start.timestamp()) if start else 0,
                "reminder_utc": 0, "has_time": start is not None,
            }
        items = sorted(projected.values(),
                       key=lambda item: (not item["has_time"],
                                         item["reminder_utc"] or item["start_utc"],
                                         item["start_utc"], item["id"]))[:24]
        response = base | {"items": items}
        # 二次硬门禁：即使将来增加字段，也不允许压垮固件 8192B 缓冲区。
        while items and len(json.dumps(response, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8")) > 8191:
            items.pop()
        return response

    @staticmethod
    def _wav_pcm(data: bytes) -> bytes:
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return data
        offset = 12
        while offset + 8 <= len(data):
            name, size = struct.unpack_from("<4sI", data, offset)
            offset += 8
            if name == b"data":
                return data[offset:offset + size]
            offset += size + (size & 1)
        return b""

    def upload_todo(self, client_todo_id: str, data: bytes, content_sha256: str | None,
                    binding_generation: int, principal: DevicePrincipal,
                    background: BackgroundTasks) -> dict[str, Any]:
        self._check_generation(principal, binding_generation)
        if (not CLIENT_ID_RE.fullmatch(client_todo_id)
                or len(client_todo_id.encode("utf-8")) > 47):
            raise api_error(400, "INVALID_TODO_ID", "待办编号无效")
        if not content_sha256 or not SHA256_RE.fullmatch(content_sha256):
            raise api_error(400, "CONTENT_SHA256_REQUIRED", "缺少有效的 X-Content-SHA256")
        actual = hashlib.sha256(data).hexdigest()
        if len(data) > MAX_TODO_BYTES:
            raise api_error(413, "TODO_AUDIO_TOO_LARGE", "语音待办音频超过服务器上限")
        if not data:
            raise api_error(422, "TODO_AUDIO_EMPTY", "语音待办音频不能为空")
        if not hmac.compare_digest(actual, content_sha256.lower()):
            raise api_error(422, "TODO_AUDIO_HASH_MISMATCH", "语音待办音频校验失败")
        existing = self.db.query_one(
            "SELECT * FROM device_voice_todos WHERE device_id=? AND client_todo_id=?",
            (principal.device_id, client_todo_id))
        if existing:
            if (existing["owner_user_id"] != principal.owner_user_id
                    or int(existing["binding_generation"]) != principal.binding_generation):
                raise api_error(403, "TODO_BINDING_MISMATCH",
                                "该待办编号属于设备的旧绑定代次")
            if existing["audio_sha256"] != actual:
                raise api_error(409, "TODO_AUDIO_CONFLICT", "同一待办编号对应了不同音频")
            return {"accepted": True, "duplicate": True, "client_todo_id": client_todo_id,
                    "server_id": existing["server_todo_id"] or client_todo_id,
                    "status": existing["status"], "revision": int(existing["revision"])}
        directory = self.todo_root / principal.device_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{client_todo_id}.wav"
        durable_write(path, data)
        now = iso_now()
        server_todo_id = "lyt-" + uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO device_voice_todos(device_id,client_todo_id,server_todo_id,owner_user_id,binding_generation,"
            "audio_sha256,audio_path,audio_bytes,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?, 'received',?,?)",
            (principal.device_id, client_todo_id, server_todo_id,
             principal.owner_user_id,
             principal.binding_generation, actual, str(path), len(data), now, now))
        background.add_task(self.process_todo, principal.device_id, client_todo_id)
        return {"accepted": True, "duplicate": False, "client_todo_id": client_todo_id,
                "server_id": server_todo_id, "status": "received", "revision": 1}

    async def process_todo(self, device_id: str, client_todo_id: str) -> None:
        row = self.db.query_one(
            "SELECT * FROM device_voice_todos WHERE device_id=? AND client_todo_id=?",
            (device_id, client_todo_id))
        if not row or row["status"] not in {"received", "processing"}:
            return
        self.db.execute("UPDATE device_voice_todos SET status='processing',revision=revision+1,updated_at=?"
                        " WHERE device_id=? AND client_todo_id=?",
                        (iso_now(), device_id, client_todo_id))
        try:
            raw = Path(row["audio_path"]).read_bytes()
            pcm = self._wav_pcm(raw)
            segments = await FunASROfflineClient().transcribe(pcm, 0)
            transcript = "".join(seg.text for seg in segments).strip()
            if not transcript:
                raise ValueError("没有识别到语音")
            local_now = datetime.now(get_timezone(DEFAULT_TZ))
            model_result = await self._translation_llm.extract_todo(
                transcript, local_now.isoformat(), DEFAULT_TZ)
            fallback = extract_optional_voice_todo(transcript, local_now, DEFAULT_TZ)
            title = clean_voice_todo_content(str(
                (model_result or {}).get("content") or fallback["title"]))
            item_type = str((model_result or {}).get("type") or fallback["type"])
            if item_type not in {"meeting", "class", "todo", "reminder"}:
                item_type = "todo"
            due_at = None
            if model_result and model_result.get("has_time") is True:
                try:
                    due_at = parse_time(str(model_result.get("due_at") or ""),
                                        fallback_now=False)
                except HTTPException:
                    due_at = None
            if due_at is None and fallback["start"] is not None:
                due_at = fallback["start"].astimezone(timezone.utc).isoformat()
            result = {"title": title, "type": item_type,
                      "due_at_utc": due_at, "transcript": transcript,
                      "parser": "deepseek" if model_result else "deterministic"}
            self.db.execute(
                "UPDATE device_voice_todos SET status='ready',transcript=?,result_json=?,"
                "revision=revision+1,updated_at=? WHERE device_id=? AND client_todo_id=?",
                (transcript, json.dumps(result, ensure_ascii=False), iso_now(),
                 device_id, client_todo_id))
        except Exception as exc:
            logger.exception("voice_todo_processing_failed device=%s todo=%s", device_id, client_todo_id)
            result = {"title": "", "type": "todo", "due_at_utc": None,
                      "transcript": "", "error": str(exc)[:200]}
            self.db.execute(
                "UPDATE device_voice_todos SET status='failed',result_json=?,revision=revision+1,"
                "updated_at=? WHERE device_id=? AND client_todo_id=?",
                (json.dumps(result, ensure_ascii=False), iso_now(), device_id, client_todo_id))

    def _todo(self, identifier: str, principal: DevicePrincipal) -> Any:
        if not CLIENT_ID_RE.fullmatch(identifier):
            raise api_error(400, "INVALID_TODO_ID", "待办编号无效")
        row = self.db.query_one(
            "SELECT * FROM device_voice_todos WHERE device_id=?"
            " AND (client_todo_id=? OR server_todo_id=?)",
            (principal.device_id, identifier, identifier))
        if not row or row["owner_user_id"] != principal.owner_user_id or int(
                row["binding_generation"]) != principal.binding_generation:
            raise api_error(404, "TODO_NOT_FOUND", "语音待办不存在")
        return row

    def todo_result(self, todo_identifier: str, principal: DevicePrincipal,
                    after_revision: int) -> tuple[int, dict[str, Any] | None]:
        row = self._todo(todo_identifier, principal)
        client_todo_id = row["client_todo_id"]
        revision = int(row["revision"])
        if revision <= max(0, after_revision):
            return 204, None
        if row["status"] in {"received", "processing"}:
            return 202, None
        result = json.loads(row["result_json"] or "{}")
        due = result.get("due_at_utc")
        due_epoch = None
        display_time = ""
        if due:
            parsed = datetime.fromisoformat(str(due).replace("Z", "+00:00")).astimezone(timezone.utc)
            due_epoch = int(parsed.timestamp())
            display_time = parsed.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
        public_status = {"ready": "needs_confirmation", "confirmed": "created",
                         "failed": "failed", "cancelled": "failed"}.get(row["status"], "failed")
        return 200, {
            "todo_id": device_text(client_todo_id, 47),
            "server_id": device_text(row["server_todo_id"] or client_todo_id, 71),
            "transcript": device_text(row["transcript"] or result.get("transcript"), 127),
            "title": device_text(result.get("title"), 71),
            "due_at_utc": due_epoch,
            "display_time": device_text(display_time, 23),
            "binding_generation": principal.binding_generation,
            "status": public_status,
            "revision": revision,
        }

    def todo_action(self, todo_identifier: str, body: TodoActionInput,
                    principal: DevicePrincipal, idempotency_key: str | None) -> dict[str, Any]:
        action = body.action.strip().lower()
        if action not in {"confirm", "cancel"}:
            raise api_error(400, "TODO_ACTION_INVALID", "action 必须是 confirm 或 cancel")
        row = self._todo(todo_identifier, principal)
        client_todo_id = row["client_todo_id"]
        payload_hash = canonical_hash(body.model_dump())
        scope = f"device:{principal.device_id}:todo:{client_todo_id}:action"
        replay = self._idempotency_lookup(scope, idempotency_key, payload_hash)
        if replay is not None:
            return replay
        if int(row["revision"]) != body.revision:
            raise api_error(409, "TODO_REVISION_MISMATCH", "语音待办解析结果已更新，请先重新读取",
                            extra={"current_revision": int(row["revision"])})
        if row["status"] in {"confirmed", "cancelled"}:
            expected = "confirmed" if action == "confirm" else "cancelled"
            if row["status"] != expected:
                raise api_error(409, "TODO_ACTION_CONFLICT", "语音待办已执行另一操作")
            response = {"client_todo_id": client_todo_id, "status": row["status"],
                        "duplicate": True, "agenda_todo_id": row["agenda_todo_id"],
                        "revision": int(row["revision"])}
            self._idempotency_store(scope, idempotency_key, payload_hash, response)
            return response
        if row["status"] != "ready":
            raise api_error(409, "TODO_NOT_READY", "语音待办尚未解析完成", retryable=True)
        agenda_todo_id = None
        now = iso_now()
        result = json.loads(row["result_json"] or "{}")
        title = str(result.get("title") or "语音待办").strip()
        due = result.get("due_at_utc")
        event_id = reminder_id = None
        if action == "confirm":
            agenda_todo_id = uuid.uuid4().hex
            if due:
                due = parse_time(str(due), fallback_now=False)
                event_id, reminder_id = uuid.uuid4().hex, uuid.uuid4().hex
            status = "confirmed"
        else:
            status = "cancelled"
        response = {"client_todo_id": client_todo_id, "status": status, "duplicate": False,
                    "agenda_todo_id": agenda_todo_id, "revision": body.revision + 1}
        # 确认、账号待办/事件、agenda revision 与幂等回放同一事务；
        # 服务器在任意一条 SQL 后掉电都不会生成重复待办。
        with self.db.transaction() as conn:
            current = conn.execute(
                "SELECT status,revision FROM device_voice_todos WHERE device_id=? AND client_todo_id=?",
                (principal.device_id, client_todo_id)).fetchone()
            if not current or current["status"] != "ready" or int(current["revision"]) != body.revision:
                raise api_error(409, "TODO_REVISION_MISMATCH",
                                "语音待办解析结果已更新，请先重新读取",
                                extra={"current_revision": int(current["revision"]) if current else 0})
            if action == "confirm":
                if due:
                    event_type = result.get("type") if result.get("type") in {
                        "meeting", "class", "todo", "reminder"} else "todo"
                    conn.execute(
                        "INSERT INTO agenda_events(id,owner,type,title,start_at,end_at,recurrence_rule,"
                        "source,linked_meeting_id,created_at) VALUES(?,?,?,?,?,NULL,NULL,'voice',NULL,?)",
                        (event_id, principal.owner_user_id, event_type, title, due, now))
                    conn.execute(
                        "INSERT INTO agenda_reminders(id,event_id,remind_at,channel)"
                        " VALUES(?,?,?,'screen')", (reminder_id, event_id, due))
                conn.execute(
                    "INSERT INTO agenda_todos(id,owner,text,due_at,done,source_event_id)"
                    " VALUES(?,?,?,?,0,?)",
                    (agenda_todo_id, principal.owner_user_id, title, due, event_id))
                conn.execute(
                    "INSERT INTO agenda_revisions(owner_user_id,revision,updated_at) VALUES(?,1,?)"
                    " ON CONFLICT(owner_user_id) DO UPDATE SET revision=revision+1,"
                    " updated_at=excluded.updated_at", (principal.owner_user_id, now))
            conn.execute(
                "UPDATE device_voice_todos SET status=?,agenda_todo_id=?,revision=revision+1,updated_at=?"
                " WHERE device_id=? AND client_todo_id=?",
                (status, agenda_todo_id, now, principal.device_id, client_todo_id))
            conn.execute(
                "INSERT INTO api_idempotency(scope,idempotency_key,request_hash,status_code,response_json,"
                "created_at) VALUES(?,?,?,?,?,?)",
                (scope, idempotency_key, payload_hash, 200,
                 json.dumps(response, ensure_ascii=False, sort_keys=True), now))
        return response


def create_device_v2_router(storage: Any, *,
                            on_session_complete: Callable[[str, int], Awaitable[None]] | None = None,
                            on_audio_range_committed: Callable[[str], Awaitable[None]] | None = None,
                            offline_progress: Callable[[str], dict[str, Any]] | None = None,
                            on_live_caption: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
                            on_mark: Callable[[str], Awaitable[None]] | None = None
                            ) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["luoye-device-v2"])
    service = DeviceService(storage, on_session_complete=on_session_complete,
                            on_audio_range_committed=on_audio_range_committed,
                            offline_progress=offline_progress,
                            on_live_caption=on_live_caption, on_mark=on_mark)

    @router.get("/build-info")
    async def build_info():
        return {
            "product": "ClearMeeting",
            "server_version": "2.0.0",
            "server_release": SERVER_RELEASE,
            "api_contract": API_CONTRACT,
            "protocol_version": API_CONTRACT,
            "minimum_firmware": "0.9.3",
            "device_auth_profile": DEVICE_AUTH_PROFILE,
            "range_block_bytes": RANGE_BLOCK_BYTES,
            "capabilities": ["device_pairing", "idempotent_upload", "session_state",
                             "agenda_sync", "voice_todo", "storage_management",
                             "network_scheduler", "bulk_upload_10mib",
                             "range_repair", "streaming_request_body",
                             "session_cancel", "live_epoch_resume",
                             "manual_gap_repair", "independent_sd_delete",
                             "transcript_only_live_v1", "template_minutes_v1",
                             "editable_meeting_speakers_v1", "meeting_memory_v1",
                             "on_demand_minutes_v1", "timeline_marks",
                             "semantic_timeline_v2", "semantic_timeline_v3_anchored", "per_device_speaker_diarization",
                             "speaker_backend_readiness", "offline_asr_pipeline_v1",
                             "canonical_offline_diarization_v2"],
            "server_time": iso_now(),
        }

    @router.post("/device/pair/start")
    async def pair_start(body: PairStartInput, response: Response,
                         x_luoye_protocol: str | None = Header(
                             default=None, alias="X-Luoye-Protocol"),
                         x_luoye_firmware: str | None = Header(
                             default=None, alias="X-Luoye-Firmware"),
                         x_luoye_device: str | None = Header(
                             default=None, alias="X-Luoye-Device")):
        service.validate_pair_headers(
            body.device_id, body.firmware_version, body.protocol_version,
            x_luoye_protocol, x_luoye_firmware, x_luoye_device)
        response.headers["Cache-Control"] = "no-store"
        return service.start_pairing(body)

    @router.post("/device/pair/status")
    async def pair_status(body: PairStatusInput, response: Response,
                          x_luoye_protocol: str | None = Header(
                              default=None, alias="X-Luoye-Protocol"),
                          x_luoye_firmware: str | None = Header(
                              default=None, alias="X-Luoye-Firmware"),
                          x_luoye_device: str | None = Header(
                              default=None, alias="X-Luoye-Device")):
        service.validate_pair_headers(
            body.device_id, None, None, x_luoye_protocol,
            x_luoye_firmware, x_luoye_device)
        response.headers["Cache-Control"] = "no-store"
        return service.pairing_status(body)

    @router.get("/me/devices")
    async def devices(user: CurrentUser = Depends(require_auth)):
        return service.list_devices(user)

    @router.post("/me/devices/claim")
    async def claim(body: ClaimInput, request: Request,
                    user: CurrentUser = Depends(require_auth)):
        forwarded = request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1].strip()
        client_ip = forwarded or (request.client.host if request.client else "unknown")
        identity = f"{user.id}:{client_ip}"
        service.check_claim_rate(identity)
        try:
            response = service.claim(body, user)
        except HTTPException:
            service.record_claim_failure(identity)
            raise
        service.clear_claim_failures(identity)
        return response

    @router.patch("/me/devices/{device_id}")
    async def update_device(device_id: str, body: DeviceUpdateInput,
                            user: CurrentUser = Depends(require_auth)):
        return service.update_device(device_id, body, user)

    @router.get("/device/config")
    async def device_config(principal: DevicePrincipal = Depends(service.require_device)):
        return service.device_config(principal)

    @router.delete("/me/devices/{device_id}/binding")
    async def unbind(device_id: str, user: CurrentUser = Depends(require_auth)):
        return service.unbind(device_id, user)

    @router.get("/me/devices/{device_id}/storage")
    async def device_storage(device_id: str, user: CurrentUser = Depends(require_auth)):
        return service.get_storage(device_id, user)

    @router.post("/me/devices/{device_id}/storage/commands")
    async def create_storage_command(device_id: str, body: StorageCommandInput,
                                     user: CurrentUser = Depends(require_auth)):
        return service.create_storage_command(device_id, body, user)

    @router.put("/device/storage/snapshot")
    async def storage_snapshot(body: StorageSnapshotInput,
                               principal: DevicePrincipal = Depends(service.require_device)):
        return service.storage_snapshot(body, principal)

    @router.post("/device/storage/commands/{command_id}/ack")
    async def storage_command_ack(command_id: str, body: StorageCommandAckInput,
                                  principal: DevicePrincipal = Depends(service.require_device)):
        return service.storage_command_ack(command_id, body, principal)

    @router.post("/device/sessions")
    async def create_session(body: SessionCreateInput,
                             principal: DevicePrincipal = Depends(service.require_device),
                             idempotency_key: str | None = Header(default=None,
                                                                 alias="Idempotency-Key")):
        return service.create_session(body, principal, idempotency_key)

    @router.put("/device/sessions/{server_session_id}/audio/{seq}")
    async def upload_audio(server_session_id: str, seq: int, request: Request,
                           principal: DevicePrincipal = Depends(service.require_device),
                           x_content_sha256: str | None = Header(default=None),
                           x_byte_offset: int | None = Header(default=None),
                           x_byte_count: int | None = Header(default=None)):
        require_media_type(request, "audio/L16")
        data = await read_bounded_body(
            request, MAX_CHUNK_BYTES, code="AUDIO_CHUNK_TOO_LARGE",
            message="音频分片超过服务器上限")
        return await service.upload_chunk(server_session_id, seq, data,
                                          x_content_sha256, x_byte_offset, x_byte_count, principal)

    @router.post("/device/sessions/{server_session_id}/live-resume")
    async def resume_live_audio(
            server_session_id: str, body: LiveResumeInput,
            principal: DevicePrincipal = Depends(service.require_device),
            idempotency_key: str | None = Header(default=None,
                                                  alias="Idempotency-Key")):
        return await service.resume_live(server_session_id, body, principal,
                                         idempotency_key)

    @router.post("/device/sessions/{server_session_id}/defer")
    async def defer_device_session(
            server_session_id: str, body: SessionDeferInput,
            principal: DevicePrincipal = Depends(service.require_device),
            idempotency_key: str | None = Header(default=None,
                                                  alias="Idempotency-Key")):
        return await service.defer_session(server_session_id, body, principal,
                                           idempotency_key)

    @router.post("/device/sessions/{server_session_id}/upload-plan")
    async def upload_plan(server_session_id: str, body: UploadPlanInput,
                          principal: DevicePrincipal = Depends(service.require_device)):
        return await service.upload_plan(server_session_id, body, principal)

    @router.put("/device/sessions/{server_session_id}/audio-range")
    async def upload_audio_range(server_session_id: str, request: Request,
                                 principal: DevicePrincipal = Depends(service.require_device),
                                 x_content_sha256: str | None = Header(default=None),
                                 x_byte_offset: int | None = Header(default=None),
                                 x_byte_count: int | None = Header(default=None)):
        require_media_type(request, "audio/L16")
        return await service.upload_range(server_session_id, request, x_content_sha256,
                                          x_byte_offset, x_byte_count, principal)

    @router.post("/device/sessions/{server_session_id}/complete")
    async def complete_audio_ranges(
            server_session_id: str, body: RangeCompleteInput,
            principal: DevicePrincipal = Depends(service.require_device),
            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await service.complete_ranges(server_session_id, body, principal, idempotency_key)

    @router.post("/device/sessions/{server_session_id}/cancel")
    async def cancel_device_session(
            server_session_id: str, body: SessionCancelInput,
            principal: DevicePrincipal = Depends(service.require_device)):
        return await service.cancel_session(server_session_id, body, principal)

    @router.put("/device/sessions/{server_session_id}/marks/{client_mark_id}")
    async def put_mark(server_session_id: str, client_mark_id: str, body: MarkInput,
                       principal: DevicePrincipal = Depends(service.require_device)):
        return service.put_mark(server_session_id, client_mark_id, body, principal)

    @router.post("/device/sessions/{server_session_id}/end")
    async def end_session(server_session_id: str, body: SessionEndInput,
                          principal: DevicePrincipal = Depends(service.require_device),
                          idempotency_key: str | None = Header(default=None,
                                                              alias="Idempotency-Key")):
        return await service.end_session(server_session_id, body, principal, idempotency_key)

    @router.get("/device/sessions/{server_session_id}/state")
    async def session_state(server_session_id: str, after_revision: int = Query(default=0, ge=0),
                            principal: DevicePrincipal = Depends(service.require_device)):
        return service.session_state(server_session_id, principal, after_revision)

    @router.get("/device/agenda")
    async def agenda(after_revision: int = Query(default=0, ge=0),
                     window_days: int = Query(default=7, ge=1, le=31),
                     principal: DevicePrincipal = Depends(service.require_device)):
        return service.agenda_snapshot(principal, after_revision, window_days)

    @router.put("/device/todos/{client_todo_id}/audio")
    async def todo_audio(client_todo_id: str, request: Request, background_tasks: BackgroundTasks,
                         binding_generation: int = Query(ge=1),
                         principal: DevicePrincipal = Depends(service.require_device),
                         x_content_sha256: str | None = Header(default=None)):
        require_media_type(request, "audio/wav")
        data = await read_bounded_body(
            request, MAX_TODO_BYTES, code="TODO_AUDIO_TOO_LARGE",
            message="语音待办音频超过服务器上限")
        return service.upload_todo(client_todo_id, data, x_content_sha256,
                                   binding_generation, principal, background_tasks)

    @router.get("/device/todos/{client_todo_id}/result")
    async def todo_result(client_todo_id: str, after_revision: int = Query(default=0, ge=0),
                          principal: DevicePrincipal = Depends(service.require_device)):
        status_code, body = service.todo_result(client_todo_id, principal, after_revision)
        if body is None:
            return Response(status_code=status_code)
        return body

    @router.post("/device/todos/{client_todo_id}/actions")
    async def todo_action(client_todo_id: str, body: TodoActionInput,
                          principal: DevicePrincipal = Depends(service.require_device),
                          idempotency_key: str | None = Header(default=None,
                                                              alias="Idempotency-Key")):
        return service.todo_action(client_todo_id, body, principal, idempotency_key)

    router.device_service = service
    return router
