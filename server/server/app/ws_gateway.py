import asyncio
import json
import logging
import os
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from .audio_upload_api import CoverageTracker
from .auth import CurrentUser, authenticated_user, require_auth
from .deepseek_client import DeepSeekClient
from .device_rolling_summary import DeviceLiveTranscriptCoordinator
from .funasr_client import FunASRClient
from .lifecycle import MeetingLifecycle
from .meeting_memory import MeetingMemory
from .offline_jobs import OfflineJob, OfflineJobQueue
from .translator import SessionTranslator
from .protocol import MessageType, event, normalize_language
from .segments import Segment, Timeline
from .session_manager import Session, SessionInUseError, SessionManager, SessionOwnershipError
from .storage import SESSION_ID, Storage

logger = logging.getLogger("ai_recorder.websocket")
router = APIRouter()
data = Path(os.getenv("DATA_DIR", "server/data"))
resume_window_seconds = max(0, int(os.getenv("SESSION_RESUME_WINDOW_SECONDS", "7200")))
sessions = SessionManager(data / "audio_cache", resume_window_seconds=resume_window_seconds)
storage = Storage(data)
llm = DeepSeekClient()
coverage = CoverageTracker(storage)   # 通道B音频覆盖区间（写透 SQLite，重启可恢复）
meeting_end_audio_wait = float(os.getenv("MEETING_END_AUDIO_WAIT_SECONDS", "10"))
meeting_memory = MeetingMemory(storage)
device_live = DeviceLiveTranscriptCoordinator(storage)
# Internal compatibility name; this object no longer generates rolling minutes.
device_rolling = device_live


@router.websocket("/ws/{session_id}")
async def gateway(ws: WebSocket, session_id: str):
    client = ws.client.host if ws.client else "unknown"
    user = authenticated_user(ws.query_params.get("token"))
    if not SESSION_ID.fullmatch(session_id) or user is None:
        logger.warning("websocket_rejected session_id=%s client=%s", session_id, client)
        await ws.close(code=1008, reason="unauthorized")
        return
    await ws.accept()
    acquired = await _acquire_session(ws, session_id, client, user.id)
    if acquired is None:
        return
    session, resumed = acquired
    session.live_ws = ws
    session.released = asyncio.Event()
    logger.info("websocket_connected session_id=%s client=%s resumed=%s", session_id, client, resumed)
    if resumed:
        storage.set_state(session_id, "recording")
    else:
        storage.create_meeting(session_id, owner_user_id=user.id)
        # server 重启后的重连：库里已有本会议分段 → 恢复时间轴与时钟，
        # 断档会由随后的 timeline_advance 注册成洞、离线补齐（崩溃自愈闭环）
        prior = storage.load_segments(session_id)
        if prior:
            session.timeline = Timeline.from_list(prior)
            last_end = max((seg.get("end_ms") or 0) for seg in prior)
            session.audio_bytes_total = int(last_end * 32)
            logger.info("timeline_restored session_id=%s segments=%d last_end_ms=%d",
                        session_id, len(prior), last_end)
    if resumed:
        # 重连：把已累积的字幕/纪要补发给客户端，避免新打开的页面丢上下文
        with suppress(Exception):
            await ws.send_json(event(
                "session_resumed",
                segments=session.transcript_segments,
                transcript=session.transcript,
                summary=session.latest_summary,
            ))
    ended = False
    try:
        while True:
            message = await ws.receive()
            if chunk := message.get("bytes"):
                await ensure_asr_started(session)
                session.sequence += 1
                session.audio.append(chunk)
                session.append_speaker_audio(chunk)
                if session.sequence == 1 or session.sequence % 10 == 0:
                    logger.info(
                        "audio_chunk session_id=%s count=%d bytes=%d",
                        session_id, session.sequence, len(chunk),
                    )
                for result in await session.asr.send_audio(chunk):
                    await emit_asr_result(ws, session, result)
            elif raw := message.get("text"):
                payload = json.loads(raw)
                message_type = payload.get("type")
                if message_type == "start_session":
                    if session.sequence:
                        logger.warning("language_change_ignored session_id=%s audio_chunks=%d", session_id, session.sequence)
                    else:
                        session.language = normalize_language(payload.get("language"))
                        session.summary_language = normalize_language(payload.get("summary_language"), summary=True)
                        session.speaker_enabled = bool(payload.get("enable_speaker", True))
                        storage.set_speaker_diarization(session_id, session.speaker_enabled)
                        session.translate_to = str(payload.get("translate_to", "") or "").strip()
                        if session.translate_to and session.translator is None:
                            session.translator = SessionTranslator(
                                session.translate_to, llm, _make_translation_sink(session))
                    # 每次 start_session 都处理真实音频偏移。不能只看 sequence：server 重启后会从
                    # SQLite 恢复时间轴，但新 Session 的 sequence 仍为 0，停机期间的洞也必须登记。
                    gap = session.advance_timeline_from_offset(payload.get("offset_ms"))
                    if gap is not None:
                        gap_start_ms, gap_end_ms = gap
                        logger.info("timeline_advance session_id=%s gap=[%d,%d]ms",
                                    session_id, gap_start_ms, gap_end_ms)
                        lifecycle.register_gap(session, gap_start_ms, gap_end_ms)
                        with suppress(Exception):
                            await send_json(ws, session, event(
                                MessageType.GAP_MARKER, start_ms=gap_start_ms,
                                end_ms=gap_end_ms, state="filling"))
                    await ensure_asr_started(session)
                    logger.info("start_session session_id=%s language=%s summary_language=%s speaker_enabled=%s",
                                session_id, session.language, session.summary_language, session.speaker_enabled)
                elif message_type == MessageType.MEETING_END:
                    session.meeting_ended = True   # 标记"结束已在处理"，HTTP 兜底见到即幂等返回
                    await ensure_asr_started(session)
                    logger.info(
                        "meeting_end session_id=%s audio_chunks=%d final_segments=%d",
                        session_id, session.sequence, len(session.transcript),
                    )
                    for result in await session.asr.finish():
                        await emit_asr_result(ws, session, result, schedule_update=False)
                    # 等音频尾片（常见情况 final 分片几百毫秒内已到；超时则走延迟定稿）
                    await lifecycle.wait_audio_complete(session_id, timeout=meeting_end_audio_wait)
                    # 先落盘（时间轴为准，含录制中已补的洞）——任何后续补洞任务的持久化都有文件可写，消除竞态
                    save_transcript_from_timeline(session)
                    await send_json(ws, session, event(
                        MessageType.MEETING_RESULT,
                        result={"state": "finalizing"},
                        summary_stage="disabled",
                        pending=True,
                    ))
                    storage.set_state(session_id, "finalizing")
                    storage.set_meta_info(session_id, session.language, session.summary_language,
                                          session.speaker.summary())
                    await lifecycle.defer_finalize(session_id, session, canonical=True)
                    ended = True
                    break
    except WebSocketDisconnect:
        logger.info("websocket_disconnected session_id=%s audio_chunks=%d", session_id, session.sequence)
    except Exception as exc:
        logger.exception("websocket_error session_id=%s error=%s", session_id, exc)
        with suppress(Exception):
            await send_json(ws, session, event(MessageType.ERROR, message="服务器处理失败，请查看服务端日志"))
    finally:
        with suppress(Exception):
            await session.asr.close()
        session.audio.close()
        session.live_ws = None
        if ended:
            sessions.pop(session_id)
            if session.translator is not None:
                asyncio.create_task(session.translator.close())   # 排空剩余译句后退出（落库不受连接关闭影响）
                session.translator = None
            logger.info("session_closed session_id=%s audio_chunks=%d", session_id, session.sequence)
        else:
            # 非正常断开：挂起会话，窗口内重连可续；ASR 连接随旧 WS 失效，重置等待重启
            session.asr = FunASRClient()
            session.asr_started = False
            sessions.suspend(session_id, session)
            storage.set_state(session_id, "suspended")
            logger.info("session_suspended session_id=%s audio_chunks=%d segments=%d window=%ds",
                        session_id, session.sequence, len(session.transcript), resume_window_seconds)
            asyncio.create_task(_finalize_when_expired(session_id, session))
        # 唤醒可能正在等待本连接释放的抢占方
        if session.released is not None:
            session.released.set()


async def _acquire_session(ws: WebSocket, session_id: str, client: str, owner_user_id: str):
    """获取/恢复会话。若同一 id 仍被僵尸旧连接占用，则抢占：关闭旧连接、等其释放后恢复。"""
    stored_owner = storage.meeting_owner(session_id)
    if stored_owner is not None and stored_owner != owner_user_id:
        logger.warning("session_owner_rejected session_id=%s client=%s", session_id, client)
        await ws.close(code=1008, reason="meeting not found")
        return None
    try:
        return sessions.create(session_id, owner_user_id)
    except SessionOwnershipError:
        await ws.close(code=1008, reason="meeting not found")
        return None
    except SessionInUseError:
        pass
    old = sessions.get(session_id)
    if old is not None and old.live_ws is not None:
        logger.info("session_takeover session_id=%s client=%s", session_id, client)
        with suppress(Exception):
            await old.live_ws.close(code=4001, reason="superseded")
        if old.released is not None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(old.released.wait(), timeout=5)
    try:
        return sessions.create(session_id, owner_user_id)
    except (SessionInUseError, SessionOwnershipError):
        logger.warning("session_takeover_failed session_id=%s client=%s", session_id, client)
        with suppress(Exception):
            await ws.send_json(event(MessageType.ERROR, message="该会议已在另一连接中进行"))
        await ws.close(code=1008, reason="session in use")
        return None


async def _finalize_when_expired(session_id: str, session: Session):
    # 等过了恢复窗口仍未重连，则落盘保存并清理，避免数据丢失与内存泄漏。
    await asyncio.sleep(resume_window_seconds + 5)
    if sessions.suspended.get(session_id) is not session:
        return  # 已重连或已被替换/清理
    sessions.drop_suspended(session_id)
    if session.translator is not None:
        asyncio.create_task(session.translator.close())
        session.translator = None
    if session.transcript or session.timeline.ordered():
        with suppress(Exception):
            save_transcript_from_timeline(session)
            storage.set_state(session_id, "transcript_ready")
            storage.cleanup_live_audio(session_id)
    logger.info("suspended_session_finalized session_id=%s segments=%d", session_id, len(session.transcript))


async def send_json(ws: WebSocket, session: Session, payload: dict):
    async with session.send_lock:
        await ws.send_json(payload)


def save_transcript_from_timeline(session: Session) -> None:
    """以时间轴为唯一来源落盘（含实时段的说话人 + 录制中已补的洞）。"""
    path = storage.save_transcript(session.id, session.timeline.to_transcript_lines(), session.timeline.to_list())
    logger.info("transcript_saved session_id=%s path=%s", session.id, path.resolve())


async def emit_asr_result(
    ws: WebSocket,
    session: Session,
    result: dict,
    *,
    schedule_update: bool = True,
):
    text = result["text"].strip()
    if not text:
        return
    is_final = result.get("is_final", False)
    speaker_id = session.current_speaker_id
    speaker_label = session.speaker.label(speaker_id)
    seg_id = start_ms = end_ms = None  # P0：时间锚 + 稳定 id，下发给前端按时间排序/补洞替换
    if is_final and (not session.transcript_segments or session.transcript_segments[-1].get("text") != text):
        segment_audio = bytes(session.pending_speaker_audio)
        session.pending_speaker_audio.clear()
        if session.speaker_enabled:
            speaker_id = await session.speaker.assign(segment_audio)
            if speaker_id:
                session.current_speaker_id = speaker_id
            speaker_label = session.speaker.label(speaker_id)
        else:
            speaker_id = None
            speaker_label = None
        end_ms = round(session.audio_bytes_total * 1000 / (16000 * 2))
        duration_ms = round(len(segment_audio) * 1000 / (16000 * 2))
        start_ms = max(0, end_ms - duration_ms)
        seg_id = uuid.uuid4().hex
        segment = {
            "seg_id": seg_id,
            "seq": session.sequence,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "speaker_id": speaker_id,
            "speaker_label": speaker_label,
            "text": text,
            "state": "provisional",
        }
        session.transcript_segments.append(segment)
        session.transcript.append(f"[{speaker_label}] {text}" if speaker_label else text)
        # 同步进时间轴（离线补洞按时间重叠判定）+ 逐条落库（server 崩溃不丢字幕）
        session.timeline.upsert_live(Segment(
            start_ms=start_ms, end_ms=end_ms, text=text,
            speaker_id=speaker_id, speaker_label=speaker_label,
            source="live", state="provisional", seg_id=seg_id,
        ))
        with suppress(Exception):
            storage.upsert_segment(session.id, segment)
    payload = event(
        MessageType.ASR_RESULT,
        text=text,
        seq=session.sequence,
        mode=result.get("mode", "unknown"),
        is_final=is_final,
        language=result.get("language") or session.language,
        speaker_id=speaker_id,
        speaker_label=speaker_label,
        seg_id=seg_id,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    await send_json(ws, session, payload)
    await session.broadcast(payload)
    logger.info(
        "asr_result session_id=%s seq=%d mode=%s is_final=%s speaker_id=%s text=%s",
        session.id, session.sequence, result.get("mode"), is_final, speaker_id, text,
    )
    # 实时翻译（v2 管道）：终句入串行队列——保序、带上下文、同语言跳过，绝不阻塞字幕链
    if is_final and session.translator is not None and seg_id:
        await session.translator.enqueue(seg_id, text)


def _make_translation_sink(session: Session):
    """翻译管道的出口（通道A=会议对照模式）：同语言跳过的句子不推；译文推客户端+observers并落库。"""
    async def sink(r: dict) -> None:
        if r.get("skipped") or not r.get("text"):
            return
        storage.set_segment_translation(session.id, r["seg_id"], r["text"])
        msg = event(MessageType.TRANSLATION, seg_id=r["seg_id"], text=r["text"],
                    lang=r["lang"], src=r["src"])
        if session.live_ws is not None:
            with suppress(Exception):
                await send_json(session.live_ws, session, msg)
        await session.broadcast(msg)
    return sink


async def ensure_asr_started(session: Session):
    if session.asr_started:
        return
    await session.asr.start(session.id, session.language)
    session.asr_started = True


@router.websocket("/ws/observe/{session_id}")
async def observe(ws: WebSocket, session_id: str):
    client = ws.client.host if ws.client else "unknown"
    user = authenticated_user(ws.query_params.get("token"))
    if not SESSION_ID.fullmatch(session_id) or user is None:
        logger.warning("observer_rejected session_id=%s client=%s", session_id, client)
        await ws.close(code=1008, reason="unauthorized")
        return

    session = sessions.get(session_id)
    if session is None:
        device_owner = device_rolling.live_owner(session_id)
        if device_owner != user.id:
            await ws.accept()
            await ws.send_json(event("error", message="会议不存在或尚未开始"))
            await ws.close()
            return
        await ws.accept()
        device_rolling.subscribe(session_id, ws)
        logger.info("device_observer_connected session_id=%s client=%s", session_id, client)
        try:
            await ws.send_json(device_rolling.catchup(session_id))
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except Exception:
            pass
        finally:
            device_rolling.unsubscribe(session_id, ws)
            logger.info("device_observer_disconnected session_id=%s client=%s",
                        session_id, client)
        return
    if session.owner_user_id != user.id:
        await ws.accept()
        await ws.send_json(event("error", message="会议不存在或尚未开始"))
        await ws.close()
        return

    await ws.accept()
    session.observers.add(ws)
    logger.info("observer_connected session_id=%s client=%s total=%d", session_id, client, len(session.observers))

    # Send current state so observer can catch up immediately.
    if session.transcript_segments:
        await ws.send_json(event(
            "observer_catchup",
            segments=session.transcript_segments,
            transcript=session.transcript,
            summary=session.latest_summary,
        ))

    try:
        # Keep connection open; observer sends no audio, just pings or nothing.
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
    except Exception:
        pass
    finally:
        session.observers.discard(ws)
        logger.info("observer_disconnected session_id=%s client=%s remaining=%d", session_id, client, len(session.observers))


# ── 离线补洞任务队列 + 生命周期协调（架构评审决策 1 的触发模型）──
def _get_timeline(session_id: str) -> Timeline | None:
    """活跃会话用内存 timeline；已结束等待定稿的用 finalizing 里挂着的；否则从 storage 加载。"""
    s = sessions.get(session_id) or lifecycle.finalizing.get(session_id)
    if s is not None:
        return s.timeline
    meeting = storage.get_meeting(session_id)
    if meeting is None:
        return None
    return Timeline.from_list(meeting.get("segments") or [])


async def _on_offline_applied(session_id: str, timeline: Timeline, patch, reason: str) -> None:
    """补洞只更新可靠转写和客户端字幕，绝不触发模型调用。"""
    # 1) 增量持久化（只动 patch 涉及的分段，不整表重写——不会误删内存 timeline 里没有的旧段）
    storage.apply_patch(session_id, patch.to_dict())
    # Device session revision is a semantic result cursor, not an upload
    # counter.  Stamp the newly visible segments with the same revision so
    # after_revision can return a bounded true delta.
    now = datetime.now(timezone.utc).isoformat()
    with storage.db.transaction() as conn:
        row = conn.execute(
            "SELECT revision FROM device_sessions WHERE server_session_id=?",
            (session_id,)).fetchone()
        if row is not None:
            revision = int(row["revision"]) + 1
            conn.execute(
                "UPDATE device_sessions SET revision=?,updated_at=? WHERE server_session_id=?",
                (revision, now, session_id))
            for segment in patch.added:
                conn.execute(
                    "UPDATE segments SET revision=? WHERE session_id=? AND seg_id=?",
                    (revision, session_id, segment.seg_id))
    s = sessions.get(session_id)
    live = s is not None and s.live_ws is not None and session_id not in lifecycle.ended
    # 补洞内容也进翻译管道（否则双语记录在断网段有洞）。上下文顺序略受影响，可接受；
    # 会话已结束、翻译器已关闭时 enqueue 为 no-op（迟到补洞不译，属已知限制）。
    translator = getattr(s, "translator", None) if s is not None else None
    if translator is not None:
        for p in patch.to_dict().get("patches", []):
            if p.get("text") and p.get("seg_id"):
                await translator.enqueue(p["seg_id"], p["text"])
    # 2) 客户端还连着就原位推送补洞分段（替换前端的 filling 占位）
    if s is not None and s.live_ws is not None:
        evt = event(MessageType.SEGMENTS_PATCH, **patch.to_dict())
        with suppress(Exception):
            await send_json(s.live_ws, s, evt)
        await s.broadcast(evt)
    # 已完全关闭时追加发布屏障；已挂在 finalizing 的会议由 lifecycle 自己排队。
    if (not live and s is None and patch.added
          and session_id not in sessions.suspended
          and session_id not in lifecycle.finalizing
          and storage.db.query_one(
              "SELECT 1 FROM device_sessions WHERE server_session_id=?", (session_id,)) is None):
        await offline_queue.enqueue(session_id, 0, 0, "publish")


def _overlap_ms(left: dict, right: dict) -> int:
    return max(0, min(int(left.get("end_ms") or 0), int(right.get("end_ms") or 0))
               - max(int(left.get("start_ms") or 0), int(right.get("start_ms") or 0)))


def _align_canonical_speakers(canonical: list[dict], existing: list[dict]) -> list[dict]:
    """Map whole-meeting clusters onto trustworthy live IDs without imposing K."""
    anchors = [item for item in existing
               if item.get("source") == "live" and item.get("speaker_id")]
    canonical_ids = []
    for item in canonical:
        speaker_id = item.get("speaker_id")
        if speaker_id and speaker_id not in canonical_ids:
            canonical_ids.append(speaker_id)
    votes = {}
    for item in canonical:
        canonical_id = item.get("speaker_id")
        if not canonical_id:
            continue
        for anchor in anchors:
            overlap = _overlap_ms(item, anchor)
            if overlap:
                key = (canonical_id, str(anchor["speaker_id"]))
                votes[key] = votes.get(key, 0) + overlap
    mapping = {}
    used_anchor_ids = set()
    for (canonical_id, anchor_id), overlap in sorted(
            votes.items(), key=lambda pair: (-pair[1], pair[0])):
        if overlap < 1000 or canonical_id in mapping or anchor_id in used_anchor_ids:
            continue
        mapping[canonical_id] = anchor_id
        used_anchor_ids.add(anchor_id)
    reserved = {str(item["speaker_id"]) for item in anchors}
    reserved.update(mapping.values())
    next_number = 1
    for canonical_id in canonical_ids:
        if canonical_id in mapping:
            continue
        while f"spk_{next_number:02d}" in reserved:
            next_number += 1
        mapping[canonical_id] = f"spk_{next_number:02d}"
        reserved.add(mapping[canonical_id])
        next_number += 1
    aligned = []
    for item in canonical:
        copy = dict(item)
        speaker_id = mapping.get(copy.get("speaker_id"))
        copy["speaker_id"] = speaker_id
        copy["speaker_label"] = (
            f"说话人 {int(speaker_id.rsplit('_', 1)[1])}" if speaker_id else None)
        copy["speaker_final"] = True
        copy["source"] = "offline_canonical"
        copy["state"] = "final"
        aligned.append(copy)
    return aligned


async def _on_canonical_finalized(session_id: str, payload: dict) -> None:
    row = storage.db.query_one(
        "SELECT canonical_sha256,expected_samples,speaker_diarization_enabled FROM device_sessions"
        " WHERE server_session_id=?", (session_id,))
    expected_sha256 = str(row["canonical_sha256"] or "").lower() if row else ""
    if expected_sha256 and str(payload.get("canonical_sha256") or "").lower() != expected_sha256:
        raise RuntimeError("canonical finalizer SHA-256 does not match device metadata")
    if row:
        duration_ms = int(row["expected_samples"] or 0) * 1000 // 16000
        speaker_enabled = bool(row["speaker_diarization_enabled"])
    else:
        meeting_row = storage.db.query_one(
            "SELECT audio_end_ms,speaker_diarization_enabled FROM meetings WHERE session_id=?",
            (session_id,))
        if meeting_row is None:
            raise RuntimeError("meeting is missing during canonical finalization")
        duration_ms = int(meeting_row["audio_end_ms"] or 0)
        speaker_enabled = bool(meeting_row["speaker_diarization_enabled"])
    raw_segments = payload.get("segments") or []
    if not raw_segments:
        raise RuntimeError("canonical finalizer returned an empty timeline")
    canonical = []
    previous_start = -1
    for item in raw_segments:
        start_ms = max(0, int(item.get("start_ms") or 0))
        end_ms = max(start_ms, int(item.get("end_ms") or start_ms))
        text = str(item.get("text") or "").strip()
        if not text or end_ms <= start_ms or (duration_ms and end_ms > duration_ms + 1000):
            raise RuntimeError("canonical finalizer returned an invalid segment")
        if start_ms < previous_start:
            raise RuntimeError("canonical finalizer returned an unsorted timeline")
        previous_start = start_ms
        canonical.append({**item, "start_ms": start_ms, "end_ms": end_ms, "text": text})
    session = sessions.get(session_id) or lifecycle.finalizing.get(session_id)
    existing = session.timeline.to_list() if session is not None else (
        (storage.get_meeting(session_id) or {}).get("segments") or [])
    canonical = _align_canonical_speakers(canonical, existing)
    if not speaker_enabled:
        for item in canonical:
            item["speaker_id"] = None
            item["speaker_label"] = None
    speaker_counts = {}
    for item in canonical:
        speaker_id = item.get("speaker_id")
        if speaker_id:
            speaker_counts[speaker_id] = speaker_counts.get(speaker_id, 0) + 1
    payload["speaker_count"] = len(speaker_counts)
    payload["speakers"] = [
        {"speaker_id": speaker_id,
         "label": f"说话人 {int(speaker_id.rsplit('_', 1)[1])}",
         "segment_count": count}
        for speaker_id, count in sorted(speaker_counts.items())]
    revision = storage.replace_canonical_segments(session_id, canonical, payload)
    await meeting_memory.match_session(session_id)
    canonical = storage.load_segments(session_id)
    timeline = Timeline.from_list(canonical)
    if session is not None:
        session.timeline = timeline
        session.transcript_segments = canonical
        session.transcript = timeline.to_transcript_lines()
    logger.info(
        "canonical_timeline_committed session_id=%s revision=%d segments=%d speakers=%d pipeline=%s",
        session_id, revision, len(canonical), len(speaker_counts),
        str(payload.get("pipeline_version") or ""))


async def _on_finalize_summary(session_id: str) -> None:
    """完成屏障：发布完整转写并停止，不自动生成任何会议纪要。"""
    session = lifecycle.pop_finalizing(session_id)
    if session is not None:
        save_transcript_from_timeline(session)
    else:
        meeting = storage.get_meeting(session_id)
        if meeting is None:
            logger.warning("publish_transcript_no_meeting session_id=%s", session_id)
            if storage.db.query_one(
                    "SELECT 1 FROM device_sessions WHERE server_session_id=?"
                    " AND status NOT IN ('done','cancelled')", (session_id,)) is not None:
                raise RuntimeError("meeting metadata is missing during transcript publication")
            return
    storage.set_state(session_id, "transcript_ready")
    storage.cleanup_live_audio(session_id)
    logger.info("transcript_ready session_id=%s", session_id)
    storage.db.execute(
        "UPDATE device_sessions SET status='done',revision=revision+1,updated_at=?"
        " WHERE server_session_id=? AND status NOT IN ('done','failed')",
        (datetime.now(timezone.utc).isoformat(), session_id))


async def _on_offline_give_up(job: OfflineJob, exc: Exception) -> None:
    """Turn an exhausted durable device job into a visible terminal failure."""
    now = datetime.now(timezone.utc).isoformat()
    with storage.db.transaction() as conn:
        row = conn.execute(
            "SELECT status FROM device_sessions WHERE server_session_id=?",
            (job.session_id,)).fetchone()
        if row is None or row["status"] in {"done", "failed"}:
            return
        conn.execute(
            "UPDATE device_sessions SET status='failed',revision=revision+1,failure_code=?,"
            "failure_message=?,updated_at=? WHERE server_session_id=?",
            (f"OFFLINE_{job.reason.upper()}_FAILED",
             "云端处理重试耗尽，录音文件仍保留", now, job.session_id))
        conn.execute(
            "UPDATE meetings SET state='done',updated_at=? WHERE session_id=?",
            (now, job.session_id))
    logger.error("device_session_terminal_failure session_id=%s reason=%s error=%s",
                 job.session_id, job.reason, type(exc).__name__)


offline_queue = OfflineJobQueue(data / "audio_cache", _get_timeline, _on_offline_applied,
                                on_summarize=_on_finalize_summary,
                                on_canonical=_on_canonical_finalized,
                                on_gap_done=lambda sid, s, e: storage.delete_gap(sid, s),
                                on_give_up=_on_offline_give_up,
                                db=storage.db)
lifecycle = MeetingLifecycle(sessions, coverage, offline_queue, storage=storage)


@router.post("/api/v1/sessions/{session_id}/end")
async def end_meeting_http(session_id: str, user: CurrentUser = Depends(require_auth)):
    """meeting_end 的 HTTP 兜底（幂等）。

    背景：结束信号原本只走 WS——WS 恰好死了/僵死时信号丢失，会话被当成"意外断开"
    挂起等 2 小时恢复窗口才出纪要。客户端现在结束时 WS+HTTP 双发；本接口保证：
    - WS 路径已在处理（session.meeting_ended）→ 直接返回，不重复；
    - 僵尸连接 → 关闭旧 WS 令其挂起，再立即走定稿；
    - 已挂起 → 立即定稿（转写落盘 + 内联出纪要或挂入 finalizing），不再等 2 小时；
    - 内存无会话（如 server 重启过）→ 库里未完成则走哨兵兜底出纪要。
    """
    if not SESSION_ID.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    owner = storage.meeting_owner(session_id)
    if owner is not None and owner != user.id:
        raise HTTPException(status_code=404, detail="会议不存在")
    # 1) 活跃连接：WS 路径已在处理 → 幂等返回；僵尸连接 → 关掉让它挂起，落入下面的挂起分支
    s = sessions.get(session_id)
    if s is not None:
        if s.owner_user_id != user.id:
            raise HTTPException(status_code=404, detail="会议不存在")
        if s.meeting_ended:
            return {"ok": True, "state": "ws_handling"}
        s.meeting_ended = True
        if s.live_ws is not None:
            with suppress(Exception):
                await s.live_ws.close(code=4002, reason="ended via http")
        if s.released is not None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(s.released.wait(), timeout=5)
    # 2) 挂起会话：立即定稿（等音频尾片 → 转写落盘 → 内联出纪要 / 挂入 finalizing）
    session = sessions.drop_suspended(session_id)
    if session is not None:
        if session.owner_user_id != user.id:
            sessions.suspended[session_id] = session
            raise HTTPException(status_code=404, detail="会议不存在")
        session.meeting_ended = True
        await lifecycle.wait_audio_complete(session_id, timeout=meeting_end_audio_wait)
        save_transcript_from_timeline(session)
        storage.set_state(session_id, "finalizing")
        storage.set_meta_info(session_id, session.language, session.summary_language,
                              session.speaker.summary())
        await lifecycle.defer_finalize(session_id, session, canonical=True)
        if session.translator is not None:
            asyncio.create_task(session.translator.close())   # 排空已入队译句后退出，不泄漏 worker
            session.translator = None
        logger.info("meeting_end_via_http session_id=%s", session_id)
        return {"ok": True, "state": "finalizing"}
    # 3) 内存无会话：库里查状态；未完成 → 哨兵兜底出纪要（重启恢复同款路径）
    state = storage.get_state(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    if state in {"done", "transcript_ready"}:
        return {"ok": True, "state": state}
    storage.set_state(session_id, "finalizing")
    if storage.needs_canonical_finalization(session_id):
        await offline_queue.enqueue(session_id, 0, 0, "canonical")
    await offline_queue.enqueue(session_id, 0, 0, "publish")
    logger.info("meeting_end_via_http_recovered session_id=%s prev_state=%s", session_id, state)
    return {"ok": True, "state": "finalizing"}
