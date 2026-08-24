"""通道 B：可靠音频分片上传（设计文档 §5）。

HTTP 分片 POST，天然断点续传：客户端/录音卡按 seq 上传，记录已 ack 的 seq，
未 ack 的重传。后端把分片按 start_ms 落盘并维护"已覆盖区间"，
当某段连续区间补齐时登记离线转写任务（gap / bulk / finalize）。

覆盖区间登记、分片落盘以及 lifecycle/offline_jobs 补洞调度均已接通。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import CurrentUser, require_auth
from .storage import SESSION_ID

logger = logging.getLogger("ai_recorder.audio_upload")

BYTES_PER_MS = 16000 * 2 / 1000  # 16k/16bit/mono


def write_chunk_at(path: Path, offset: int, data: bytes) -> None:
    """把分片写到文件的指定字节偏移（稀疏写）。断网缺口处先留空（零填充），重传到了再补上。"""
    if not data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    with path.open("r+b") as f:
        f.seek(offset)
        f.write(data)


class CoverageTracker:
    """维护每个会议"已收到音频"的时间区间集合，用于检测缺口与触发离线任务。
    传入 storage 时写透持久化（server 重启后恢复，录音卡隔天补传也能触发补洞）。"""

    def __init__(self, storage=None) -> None:
        self.storage = storage
        self._intervals: dict[str, list[tuple[int, int]]] = (
            storage.load_all_coverage() if storage is not None else {})

    def add(self, session_id: str, start_ms: int, end_ms: int) -> None:
        ivs = self._intervals.setdefault(session_id, [])
        ivs.append((start_ms, end_ms))
        self._intervals[session_id] = self._merge(ivs)
        if self.storage is not None:
            self.storage.save_coverage(session_id, self._intervals[session_id])

    def covered(self, session_id: str) -> list[tuple[int, int]]:
        return list(self._intervals.get(session_id, []))

    def contains(self, session_id: str, start_ms: int, end_ms: int) -> bool:
        """[start_ms,end_ms) 是否已被某个连续覆盖区间完整包含（判断断网洞的音频是否补齐）。"""
        return any(s <= start_ms and end_ms <= e for s, e in self.covered(session_id))

    def gaps(self, session_id: str, total_ms: int) -> list[tuple[int, int]]:
        """返回 [0,total_ms] 内尚未覆盖的缺口区间。"""
        out: list[tuple[int, int]] = []
        cursor = 0
        for s, e in self.covered(session_id):
            if s > cursor:
                out.append((cursor, s))
            cursor = max(cursor, e)
        if cursor < total_ms:
            out.append((cursor, total_ms))
        return out

    @staticmethod
    def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
        intervals = sorted(intervals)
        merged: list[tuple[int, int]] = []
        for s, e in intervals:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return merged


def create_audio_upload_router(audio_root: Path, coverage: CoverageTracker,
                               on_progress=None, on_audio_complete=None, *,
                               prefix: str = "/api/v1/sessions") -> APIRouter:
    """on_progress(session_id): 每片上传后回调（生命周期协调器据此检查断网洞是否补齐→只转洞）。
    on_audio_complete(session_id, end_ms): final=1 音频传完回调（触发会议定稿调度）。"""
    router = APIRouter(prefix=prefix, tags=["audio"])

    @router.post("/{session_id}/audio")
    async def upload_chunk(session_id: str, request: Request, seq: int, start_ms: int, final: int = 0,
                           user: CurrentUser = Depends(require_auth)):
        if not SESSION_ID.fullmatch(session_id):
            return {"error": "invalid session id"}
        if coverage.storage is not None and not coverage.storage.user_owns_meeting(session_id, user.id):
            raise HTTPException(status_code=404, detail="会议不存在")
        body = await request.body()
        # 通道B的完整音频写到独立的 .b.pcm（不碰实时流写的 .pcm），按 start_ms 偏移稀疏写入
        write_chunk_at(audio_root / f"{session_id}.b.pcm", int(start_ms * BYTES_PER_MS), body)
        end_ms = start_ms + int(len(body) / BYTES_PER_MS)
        coverage.add(session_id, start_ms, end_ms)
        logger.info("audio_chunk_uploaded session_id=%s seq=%d range=[%d,%d) final=%d bytes=%d",
                    session_id, seq, start_ms, end_ms, final, len(body))
        if on_progress:
            await on_progress(session_id)
        if final and on_audio_complete:
            await on_audio_complete(session_id, end_ms)
        return {"acked_seq": seq, "next_expected_seq": seq + 1}

    @router.get("/{session_id}/coverage")
    async def get_coverage(session_id: str, user: CurrentUser = Depends(require_auth)):
        if coverage.storage is not None and not coverage.storage.user_owns_meeting(session_id, user.id):
            raise HTTPException(status_code=404, detail="会议不存在")
        return {"covered": coverage.covered(session_id)}

    @router.post("/{session_id}/offline-transcribe")
    async def offline_transcribe(session_id: str, user: CurrentUser = Depends(require_auth)):
        """P2 调试：对 {id}.b.pcm 跑一遍离线转写，返回带时间戳的分段，验证离线链路。
        需 ASR_MODE=funasr（真实转写）。"""
        from .funasr_offline_client import FunASROfflineClient
        if not SESSION_ID.fullmatch(session_id):
            return {"error": "invalid session id"}
        if coverage.storage is not None and not coverage.storage.user_owns_meeting(session_id, user.id):
            raise HTTPException(status_code=404, detail="会议不存在")
        path = audio_root / f"{session_id}.b.pcm"
        if not path.exists():
            return {"error": f"{path.name} 不存在，先用通道B录一段"}
        pcm = path.read_bytes()
        segs = await FunASROfflineClient().transcribe(pcm, 0)
        return {"bytes": len(pcm), "count": len(segs), "segments": [s.to_dict() for s in segs]}

    return router
