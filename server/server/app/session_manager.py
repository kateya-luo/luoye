import asyncio
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from .audio_buffer import AudioBuffer
from .funasr_client import FunASRClient
from .segments import Timeline
from .speaker_diarizer import SpeakerDiarizer

logger = logging.getLogger("ai_recorder.session")


class SessionInUseError(Exception):
    """另一活跃连接正占用同一 session id 时抛出，拒绝抢占。"""


class SessionOwnershipError(Exception):
    """同一 session id 属于另一个账号。"""


@dataclass
class Session:
    id: str
    audio: AudioBuffer
    owner_user_id: str = "TEST1"
    asr: FunASRClient = field(default_factory=FunASRClient)
    transcript: list[str] = field(default_factory=list)
    transcript_segments: list[dict[str, Any]] = field(default_factory=list)
    pending_speaker_audio: bytearray = field(default_factory=bytearray)
    speaker: SpeakerDiarizer = field(default_factory=SpeakerDiarizer)
    current_speaker_id: str | None = None
    audio_bytes_total: int = 0
    sequence: int = 0
    last_summarized_count: int = 0
    summary_task: asyncio.Task | None = None
    latest_summary: dict[str, Any] | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    language: str = "auto"
    summary_language: str = "auto"
    translate_to: str = ""       # 空=不翻译；"en"/"zh"/... = 每句终句翻译后推 translation
    translator: Any = None       # SessionTranslator（v2 翻译管道：保序队列+上下文+同语跳过）
    asr_started: bool = False
    speaker_enabled: bool = True
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    observers: set = field(default_factory=set)  # set[WebSocket]
    disconnected_at: float | None = None  # 挂起时间戳；None 表示当前在线
    live_ws: Any = None        # 当前活跃 WebSocket，供重连抢占时强制关闭旧连接
    released: Any = None        # asyncio.Event，处理协程结束时置位，供抢占方等待释放
    timeline: Timeline = field(default_factory=Timeline)  # 时间锚定分段集，离线补洞在此原位插入
    pending_gaps: list = field(default_factory=list)  # 已注册未转写的断网洞 [(start_ms,end_ms)]，音频补齐即转
    meeting_ended: bool = False  # meeting_end 已开始处理（WS 或 HTTP 兜底任一路径），保证结束只处理一次

    def append_speaker_audio(self, chunk: bytes):
        self.audio_bytes_total += len(chunk)
        self.pending_speaker_audio.extend(chunk)
        max_bytes = max(16000 * 2, int(float(os.getenv("SPEAKER_MAX_SEGMENT_SECONDS", "30")) * 16000 * 2))
        if len(self.pending_speaker_audio) > max_bytes:
            del self.pending_speaker_audio[:-max_bytes]

    def advance_timeline_from_offset(self, offset_ms) -> tuple[int, int] | None:
        """按客户端真实录音偏移推进服务端时钟；返回新发现的断网洞。"""
        if offset_ms is None:
            return None
        target = max(0, int(float(offset_ms) * 16000 * 2 / 1000))
        if target <= self.audio_bytes_total:
            return None
        gap = (int(self.audio_bytes_total / 32), int(target / 32))
        self.audio_bytes_total = target
        return gap

    async def broadcast(self, payload: dict) -> None:
        dead: set = set()
        for ws in self.observers:
            with suppress(Exception):
                await ws.send_json(payload)
                continue
            dead.add(ws)
        self.observers -= dead


class SessionManager:
    def __init__(self, audio_root: Path, resume_window_seconds: int = 7200):
        self.audio_root = audio_root
        self.resume_window_seconds = resume_window_seconds
        self.sessions: dict[str, Session] = {}      # 在线（已连接）
        self.suspended: dict[str, Session] = {}     # 已断开、等待重连恢复

    def create(self, session_id: str, owner_user_id: str = "TEST1") -> tuple[Session, bool]:
        # 同一 id 已有活跃连接：先校验账号归属，再进入同账号的僵尸连接抢占流程。
        active = self.sessions.get(session_id)
        if active is not None and active.owner_user_id != owner_user_id:
            raise SessionOwnershipError(session_id)
        if active is not None:
            raise SessionInUseError(session_id)
        # 窗口内的挂起会话：原样恢复，续写音频与 transcript
        suspended = self.suspended.get(session_id)
        if suspended is not None and suspended.owner_user_id != owner_user_id:
            raise SessionOwnershipError(session_id)
        suspended = self.suspended.pop(session_id, None)
        if suspended is not None:
            age = time.time() - (suspended.disconnected_at or 0.0)
            if age <= self.resume_window_seconds:
                suspended.audio = AudioBuffer(self.audio_root, session_id)  # 追加模式重开
                suspended.disconnected_at = None
                self.sessions[session_id] = suspended
                return suspended, True
            # 超过窗口：丢弃旧状态，全新开始
            suspended.audio.close()
        session = Session(session_id, AudioBuffer(self.audio_root, session_id, truncate=True),
                          owner_user_id=owner_user_id)
        self.sessions[session_id] = session
        return session, False

    def suspend(self, session_id: str, session: Session) -> None:
        session.disconnected_at = time.time()
        self.sessions.pop(session_id, None)
        self.suspended[session_id] = session

    def get(self, session_id: str) -> "Session | None":
        return self.sessions.get(session_id)

    def pop(self, session_id: str) -> "Session | None":
        self.suspended.pop(session_id, None)
        return self.sessions.pop(session_id, None)

    def drop_suspended(self, session_id: str) -> "Session | None":
        return self.suspended.pop(session_id, None)

    def active_list(self, owner_user_id: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "session_id": s.id,
                "started_at": s.started_at,
                "segment_count": len(s.transcript),
                "observer_count": len(s.observers),
                "language": s.language,
            }
            for s in self.sessions.values()
            if owner_user_id is None or s.owner_user_id == owner_user_id
        ]
