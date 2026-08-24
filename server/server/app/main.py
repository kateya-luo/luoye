import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import asyncio

from .audio_upload_api import create_audio_upload_router
from .agenda import create_agenda_router
from .auth import CurrentUser, configure_auth, require_auth, router as auth_router
from .device_api_v1 import create_device_v2_router
from .device_offline_pipeline import ready_asr_windows
from .history_api import create_history_router
from .sessions_api import create_sessions_router
from .speaker_diarizer import probe_speaker_backend
from .ws_gateway import (router as ws_router, sessions, offline_queue, lifecycle,
                         coverage, storage, device_rolling)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="ClearMeeting API", version="1.0.1")
configure_auth(storage)  # 绑定用户库并幂等创建 TEST1–TEST5
_cors_origins = [origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
                 if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["Authorization", "Content-Type", "Idempotency-Key",
                                  "X-Content-SHA256", "X-Byte-Offset", "X-Byte-Count",
                                  "X-Battery-Percent", "X-Luoye-Protocol",
                                  "X-Luoye-Firmware", "X-Luoye-Device"])
app.include_router(ws_router)
app.include_router(auth_router)
app.include_router(create_history_router(storage, prefix="/api/v1/meetings"))
app.include_router(create_sessions_router(sessions, storage=storage,
                                          prefix="/api/v1/sessions"))

# 通道B：可靠音频分片上传 + 生命周期协调（每片上传→查洞补齐即转洞；final=1→定稿调度）
_data_dir = Path(os.getenv("DATA_DIR", "server/data"))
_offline_window_ms = max(
    60_000, int(os.getenv("DEVICE_OFFLINE_ASR_WINDOW_MS", str(5 * 60 * 1000))))
app.include_router(create_audio_upload_router(_data_dir / "audio_cache", coverage,
                                              on_progress=lifecycle.on_upload_progress,
                                              on_audio_complete=lifecycle.on_audio_complete,
                                              prefix="/api/v1/sessions"))

app.include_router(create_agenda_router(storage, prefix="/api/v1/agenda"))


def _covered_byte_ranges(session_id: str, total_bytes: int) -> list[tuple[int, int]]:
    rows = storage.db.query(
        "SELECT start_byte,end_byte FROM device_audio_ranges WHERE server_session_id=?"
        " ORDER BY start_byte", (session_id,))
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


def _ensure_device_meeting(session_id: str) -> bool:
    """Rebuild a deleted meeting row when the recorder still owns authoritative audio."""
    if storage.get_state(session_id) is not None:
        return True
    row = storage.db.query_one(
        "SELECT owner_user_id,started_at_utc,title,source_language,target_language,status,"
        "expected_samples,sample_rate FROM device_sessions WHERE server_session_id=?",
        (session_id,))
    if row is None:
        return False
    storage.create_meeting(
        session_id, language=str(row["source_language"] or "auto"),
        summary_language=str(row["target_language"] or "auto"),
        owner_user_id=str(row["owner_user_id"]))
    status = str(row["status"])
    meeting_state = "done" if status in {"done", "cancelled"} else (
        "finalizing" if status in {"processing", "failed"} else "recording")
    end_ms = (int(row["expected_samples"] or 0) * 1000
              // max(1, int(row["sample_rate"] or 16000)))
    with storage.db.transaction() as conn:
        conn.execute(
            "UPDATE meetings SET created_at=?,title=?,state=?,audio_end_ms=?,updated_at=?"
            " WHERE session_id=?",
            (str(row["started_at_utc"]), row["title"], meeting_state,
             end_ms or None, datetime.now(timezone.utc).isoformat(), session_id))
    logging.getLogger("ai_recorder.device_pipeline").warning(
        "device_meeting_metadata_rebuilt session_id=%s state=%s", session_id, meeting_state)
    return True


async def _schedule_device_asr(session_id: str, *, sealed: bool = False,
                               recreate_missing: bool = False) -> None:
    """把已校验字节覆盖的 5 分钟窗口投入持久队列。

    未 /complete 时只投完整窗口；最后不足 5 分钟的尾片必须等 complete 封口，避免
    服务器把尚未最终确认的尾部误当成完整录音。
    """
    if storage.get_state(session_id) is None and not (
            recreate_missing and _ensure_device_meeting(session_id)):
        logging.getLogger("ai_recorder.device_pipeline").warning(
            "device_asr_skip_missing_meeting session_id=%s", session_id)
        return
    row = storage.db.query_one(
        "SELECT canonical_total_bytes,expected_samples,sample_rate,bits_per_sample,channels,"
        "created_at FROM device_sessions WHERE server_session_id=?", (session_id,))
    if row is None or row["canonical_total_bytes"] is None:
        return
    total_bytes = int(row["canonical_total_bytes"] or 0)
    sample_rate = int(row["sample_rate"] or 16000)
    bytes_per_sample = max(1, int(row["bits_per_sample"] or 16) // 8) * max(
        1, int(row["channels"] or 1))
    total_samples = int(row["expected_samples"] or (total_bytes // bytes_per_sample))
    total_ms = total_samples * 1000 // sample_rate
    if total_ms <= 0:
        return
    covered = _covered_byte_ranges(session_id, total_bytes)
    order_key = str(row["created_at"] or "")
    for chunk_index, start_ms, stop_ms in ready_asr_windows(
            total_ms=total_ms, total_bytes=total_bytes, sample_rate=sample_rate,
            bytes_per_sample=bytes_per_sample, window_ms=_offline_window_ms,
            covered=covered, sealed=sealed):
        await offline_queue.enqueue(
            session_id, start_ms, stop_ms, "bulk",
            order_key=order_key, chunk_index=chunk_index)


async def _device_range_committed(session_id: str) -> None:
    await _schedule_device_asr(session_id, sealed=False, recreate_missing=True)


async def _device_session_complete(session_id: str, end_ms: int) -> None:
    """封闭尾片并追加持久化完成屏障；不再等待整场录音才开始转写。"""
    if storage.get_state(session_id) is None and not _ensure_device_meeting(session_id):
        now = datetime.now(timezone.utc).isoformat()
        storage.db.execute(
            "UPDATE device_sessions SET status='failed',revision=revision+1,"
            "failure_code='MEETING_METADATA_MISSING',"
            "failure_message='会议元数据缺失，录音文件仍保留',updated_at=?"
            " WHERE server_session_id=? AND status NOT IN ('done','failed','cancelled')",
            (now, session_id))
        logging.getLogger("ai_recorder.device_pipeline").error(
            "device_complete_missing_meeting session_id=%s", session_id)
        return
    await device_rolling.finish_input(session_id)
    coverage.add(session_id, 0, end_ms)
    await _schedule_device_asr(session_id, sealed=True, recreate_missing=True)
    row = storage.db.query_one(
        "SELECT created_at FROM device_sessions WHERE server_session_id=?", (session_id,))
    await offline_queue.enqueue(session_id, 0, 0, "summarize",
                                order_key=str(row["created_at"] if row else "~"),
                                chunk_index=2_147_483_647)


device_v2_router = create_device_v2_router(
    storage,
    on_session_complete=_device_session_complete,
    on_audio_range_committed=_device_range_committed,
    offline_progress=lambda sid: {
        **offline_queue.progress(sid), "slice_ms": _offline_window_ms},
    on_live_caption=device_rolling.on_caption,
    on_live_partial=device_rolling.on_partial,
    on_mark=device_rolling.on_mark,
)
device_v2_service = device_v2_router.device_service
app.include_router(device_v2_router)


@app.exception_handler(HTTPException)
async def _v1_http_error(_request: Request, exc: HTTPException):
    # v1 设备错误不包外层 detail，便于嵌入式客户端稳定解析 error.code。
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail,
                            headers=exc.headers)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                        headers=exc.headers)


@app.on_event("startup")
async def _start_offline_worker():
    # v0.20.1/0.20.2 曾把“设备会话仍在、meetings 行已被历史页删除”的完整录音
    # 标为失败。权威 .b.pcm 仍在时自动重建元数据并重新投递全部切片。
    for row in storage.db.query(
            "SELECT server_session_id FROM device_sessions WHERE status='failed'"
            " AND failure_code='MEETING_METADATA_MISSING'"):
        session_id = row["server_session_id"]
        audio_path = _data_dir / "audio_cache" / f"{session_id}.b.pcm"
        if not audio_path.exists() or not _ensure_device_meeting(session_id):
            continue
        now = datetime.now(timezone.utc).isoformat()
        with storage.db.transaction() as conn:
            conn.execute(
                "DELETE FROM offline_asr_jobs WHERE session_id=?", (session_id,))
            conn.execute(
                "UPDATE device_sessions SET status='processing',failure_code=NULL,"
                "failure_message=NULL,updated_at=? WHERE server_session_id=?",
                (now, session_id))
            conn.execute(
                "UPDATE meetings SET state='finalizing',updated_at=? WHERE session_id=?",
                (now, session_id))
        logging.getLogger("ai_recorder.device_pipeline").warning(
            "device_missing_meeting_recovery_queued session_id=%s", session_id)
    offline_queue.start()
    # 上传中的长录音可能在进程重启前已经具备多个完整 5 分钟窗口。
    for row in storage.db.query(
            "SELECT server_session_id FROM device_sessions"
            " WHERE status IN ('uploading','awaiting_repair') AND canonical_total_bytes IS NOT NULL"):
        await _schedule_device_asr(row["server_session_id"], sealed=False)
    # 设备 end 和队列调度之间如果掉电，重启后从 SQLite 恢复整段 ASR + 纪要。
    for row in storage.db.query(
            "SELECT server_session_id,expected_samples FROM device_sessions WHERE status='processing'"):
        device_v2_service.cleanup_session_chunks(row["server_session_id"])
        end_ms = int(row["expected_samples"] or 0) * 1000 // 16000
        await _device_session_complete(row["server_session_id"], end_ms)
    for row in storage.db.query(
            "SELECT server_session_id FROM device_sessions WHERE status='done'"):
        device_v2_service.cleanup_session_chunks(row["server_session_id"])
    # 语音待办也是持久任务；进程中断后不需要用户重录。
    for row in storage.db.query(
            "SELECT device_id,client_todo_id FROM device_voice_todos"
            " WHERE status IN ('received','processing')"):
        asyncio.create_task(device_v2_service.process_todo(row["device_id"], row["client_todo_id"]))
    # 重启恢复：库里未完成的会议（崩溃/重启遗留）延迟扫描，补洞+补出纪要
    asyncio.create_task(lifecycle.recover_unfinished())


@app.on_event("shutdown")
async def _stop_device_rolling_workers():
    await device_v2_service.shutdown()
    await device_rolling.shutdown()
    await offline_queue.stop()

@app.get("/health")
async def health(): return {"status": "ok"}


@app.get("/health/ready")
async def readiness(response: Response):
    """Deployment/readiness check that includes the configured CAM++ backend."""
    speaker = await probe_speaker_backend()
    if not speaker["ready"]:
        response.status_code = 503
    return {"status": "ok" if speaker["ready"] else "degraded", "speaker": speaker}

@app.post("/api/v1/client-log")
async def client_log(body: dict, user: CurrentUser = Depends(require_auth)):
    import logging as _log
    level = body.get("level", "error")
    msg = body.get("msg", "")
    ua = body.get("ua", "")
    _log.getLogger("client").error("CLIENT_%s user=%s ua=%s msg=%s",
                                   level.upper(), user.id, ua[:80], msg[:2000])
    return {"ok": True}
