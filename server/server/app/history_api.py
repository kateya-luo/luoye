import re
import struct
import unicodedata
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .auth import CurrentUser, require_auth
from .storage import Storage


def _wav_header(data_len: int, sample_rate: int = 16000, channels: int = 1, bits: int = 16) -> bytes:
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_len, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate,
        sample_rate * channels * bits // 8, channels * bits // 8, bits,
        b"data", data_len,
    )


class TitleUpdate(BaseModel):
    title: str


def _meeting_export_name(meeting: dict, extension: str) -> str:
    summary = meeting.get("summary") if isinstance(meeting.get("summary"), dict) else {}
    title = (meeting.get("title") or summary.get("title")
             or (summary.get("mindmap") or {}).get("title")
             or summary.get("summary") or "未命名会议")
    title = unicodedata.normalize("NFKC", str(title))
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "-", title)
    title = re.sub(r"\s+", " ", title).strip().rstrip(". ") or "未命名会议"
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", title):
        title += "-会议"
    filename = f'{title[:80]}.{extension}'
    return f'attachment; filename="meeting.{extension}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def create_history_router(storage: Storage, *, prefix: str = "/api/v1/meetings") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["meetings"])

    @router.get("")
    async def list_meetings(user: CurrentUser = Depends(require_auth)):
        return {"meetings": storage.list_meetings(user.id)}

    @router.get("/{session_id}")
    async def get_meeting(session_id: str, user: CurrentUser = Depends(require_auth)):
        meeting = storage.get_meeting(session_id, user.id)
        if meeting is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会议不存在")
        return meeting

    @router.get("/{session_id}/processing")
    async def get_processing(session_id: str, user: CurrentUser = Depends(require_auth)):
        if not storage.user_owns_meeting(session_id, user.id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会议不存在")
        processing = storage.get_processing_status(session_id)
        if processing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会议不存在")
        return processing

    @router.delete("/{session_id}")
    async def delete_meeting(session_id: str, user: CurrentUser = Depends(require_auth)):
        if not storage.delete_meeting(session_id, user.id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会议不存在")
        return {"deleted": True, "session_id": session_id}

    @router.patch("/{session_id}/title")
    async def update_title(session_id: str, body: TitleUpdate,
                           user: CurrentUser = Depends(require_auth)):
        try:
            storage.set_title(session_id, body.title, user.id)
        except FileNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会议不存在")
        return {"ok": True}

    @router.get("/{session_id}/audio")
    async def get_audio(session_id: str, user: CurrentUser = Depends(require_auth)):
        if not storage.user_owns_meeting(session_id, user.id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="录音文件不存在")
        # 优先通道B完整音频 .b.pcm（无断网洞、与字幕时间轴同源 → 点击字幕跳转精确对齐）；
        # 旧会议（通道B之前录的）回落实时流 .pcm。流式响应，避免大会议整文件占内存。
        base = storage.root / "audio_cache"
        pcm_path = base / f"{session_id}.b.pcm"
        if not pcm_path.exists() or pcm_path.stat().st_size == 0:
            pcm_path = base / f"{session_id}.pcm"
        if not pcm_path.exists() or pcm_path.stat().st_size == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="录音文件不存在")
        data_len = pcm_path.stat().st_size

        def stream():
            yield _wav_header(data_len)
            with pcm_path.open("rb") as f:
                while chunk := f.read(256 * 1024):
                    yield chunk

        return StreamingResponse(
            stream(),
            media_type="audio/wav",
            headers={
                "Content-Length": str(44 + data_len),
                "Content-Disposition": f'inline; filename="meeting-{session_id}.wav"',
            },
        )

    @router.get("/{session_id}/export")
    async def export_meeting(
        session_id: str,
        format: str = Query(default="markdown", pattern="^(markdown|txt|json)$"),
        user: CurrentUser = Depends(require_auth),
    ):
        meeting = storage.get_meeting(session_id, user.id)
        if meeting is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会议不存在")
        exported = storage.export_meeting(session_id, format, user.id)
        if exported is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会议不存在")
        content, media_type, extension = exported
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": _meeting_export_name(meeting, extension)},
        )

    return router
