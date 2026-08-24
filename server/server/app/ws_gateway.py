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
from .device_rolling_summary import DeviceRollingSummaryCoordinator
from .funasr_client import FunASRClient
from .lifecycle import MeetingLifecycle
from .offline_jobs import OfflineJob, OfflineJobQueue
from .post_meeting_diarizer import PostMeetingDiarizer
from .translator import SessionTranslator
from .timeline_chapters import enrich_summary_timeline, transcript_with_time_and_marks
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
rolling_min_segments = max(1, int(os.getenv("ROLLING_SUMMARY_MIN_SEGMENTS", "2")))
device_rolling_max_wait = max(
    0.05, float(os.getenv("DEVICE_ROLLING_SUMMARY_MAX_WAIT_SECONDS", "20")))
meeting_end_audio_wait = float(os.getenv("MEETING_END_AUDIO_WAIT_SECONDS", "10"))
post_meeting_diarizer = PostMeetingDiarizer()


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
                    if session.summary_task and not session.summary_task.done():
                        with suppress(Exception):
                            await session.summary_task
                    # 等音频尾片（常见情况 final 分片几百毫秒内已到；超时则走延迟定稿）
                    await lifecycle.wait_audio_complete(session_id, timeout=meeting_end_audio_wait)
                    # 先落盘（时间轴为准，含录制中已补的洞）——任何后续补洞任务的持久化都有文件可写，消除竞态
                    save_transcript_from_timeline(session)
                    # ① 秒回初稿：滚动纪要立即作为结果返回（标签 draft），用户结束会议零等待；
                    #    定稿（全文一致性整理）随后进行，完成后自动替换为最终版。
                    inline = lifecycle.can_finish_inline(session_id, session)
                    draft = dict(session.latest_summary or {})
                    draft.setdefault("summary", "会议已结束，纪要整理中…")
                    draft.setdefault("decisions", [])
                    draft.setdefault("action_items", [])
                    draft.setdefault("mindmap", {"title": "会议重点", "branches": []})
                    draft["summary_stage"] = "draft"
                    await send_json(ws, session, event(
                        MessageType.MEETING_RESULT,
                        result=draft,
                        summary=draft["summary"],
                        decisions=draft["decisions"],
                        action_items=draft["action_items"],
                        summary_stage="draft",
                        pending=not inline,
                    ))
                    if inline:
                        # ② 音频完整、无洞：初稿已在手，现在后台出最终版；出完趁连接还开着推送升级
                        lifecycle.finish_inline(session_id)
                        result = await finish_summary(session)
                        await send_json(ws, session, event(
                            MessageType.MEETING_RESULT,
                            result=result,
                            summary=result["summary"],
                            decisions=result["decisions"],
                            action_items=result["action_items"],
                            summary_stage="final",
                        ))
                    else:
                        # ③ 有残余洞/音频未传完：挂入 finalizing。补录段到达后增量并入纪要
                        #    （_on_offline_applied → merge_gap），全部补齐后哨兵出最终版。
                        storage.set_state(session_id, "finalizing")
                        storage.set_meta_info(session_id, session.language, session.summary_language,
                                              session.speaker.summary())   # 重启后恢复出纪要所需
                        storage.save_summary_draft(session_id, draft)      # 历史页立即有初稿可看（不动 finalizing 状态）
                        await lifecycle.defer_finalize(session_id, session)
                    ended = True
                    break
    except WebSocketDisconnect:
        logger.info("websocket_disconnected session_id=%s audio_chunks=%d", session_id, session.sequence)
    except Exception as exc:
        logger.exception("websocket_error session_id=%s error=%s", session_id, exc)
        with suppress(Exception):
            await send_json(ws, session, event(MessageType.ERROR, message="服务器处理失败，请查看服务端日志"))
    finally:
        if session.summary_task and not session.summary_task.done():
            session.summary_task.cancel()
            session.summary_task = None
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
            await finish_summary(session)
    logger.info("suspended_session_finalized session_id=%s segments=%d", session_id, len(session.transcript))


async def send_json(ws: WebSocket, session: Session, payload: dict):
    async with session.send_lock:
        await ws.send_json(payload)


def save_transcript_from_timeline(session: Session) -> None:
    """以时间轴为唯一来源落盘（含实时段的说话人 + 录制中已补的洞）。"""
    path = storage.save_transcript(session.id, session.timeline.to_transcript_lines(), session.timeline.to_list())
    logger.info("transcript_saved session_id=%s path=%s", session.id, path.resolve())


async def finish_summary(session: Session) -> dict:
    """出最终纪要（每场会议整个生命周期只应发生一次：内联路径或哨兵任务，二选一）。"""
    stored = storage.get_meeting(session.id) or {}
    transcript = transcript_with_time_and_marks(
        session.timeline.to_list(), stored.get("marks") or [])
    result = await summarize_safely(transcript, rolling=False, source_language=session.language,
                                    output_language=session.summary_language)
    result = enrich_summary_timeline(storage, session.id, result, rolling=False)
    result["source_language"] = session.language
    result["summary_language"] = session.summary_language
    result["speakers"] = session.speaker.summary()
    result["summary_stage"] = "final"
    summary_path = storage.save_summary(session.id, result)
    storage.cleanup_live_audio(session.id)   # 定稿后：.b.pcm 完整则删实时流 .pcm（播放已切 .b.pcm）
    logger.info("summary_saved session_id=%s path=%s", session.id, summary_path.resolve())
    return result


async def summarize_safely(transcript: str, *, rolling: bool, source_language: str = "auto",
                           output_language: str = "auto") -> dict:
    try:
        return await llm.summarize(transcript, rolling=rolling, source_language=source_language,
                                   output_language=output_language)
    except Exception as exc:
        logger.exception("deepseek_error rolling=%s error=%s", rolling, exc)
        return {
            "summary": "智能纪要暂时生成失败，最终转写已安全保存。",
            "decisions": [],
            "action_items": [],
            "mindmap": {"title": "会议重点", "branches": []},
        }


device_rolling = DeviceRollingSummaryCoordinator(
    storage,
    summarize_safely,
    min_segments=rolling_min_segments,
    max_wait_seconds=device_rolling_max_wait,
)


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
        "asr_result session_id=%s seq=%d mode=%s is_final=%s speaker_id=%s chars=%d bytes=%d",
        session.id, session.sequence, result.get("mode"), is_final, speaker_id,
        len(text), len(text.encode("utf-8")),
    )
    # 实时翻译（v2 管道）：终句入串行队列——保序、带上下文、同语言跳过，绝不阻塞字幕链
    if is_final and session.translator is not None and seg_id:
        await session.translator.enqueue(seg_id, text)
    if schedule_update and is_final:
        maybe_schedule_rolling_update(ws, session)


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


def maybe_schedule_rolling_update(ws: WebSocket, session: Session):
    enough_new_text = len(session.transcript) - session.last_summarized_count >= rolling_min_segments
    task_running = session.summary_task and not session.summary_task.done()
    if not enough_new_text or task_running:
        return
    snapshot = list(session.transcript)
    session.last_summarized_count = len(snapshot)
    session.summary_task = asyncio.create_task(send_rolling_update(ws, session, snapshot))


async def send_rolling_update(ws: WebSocket, session: Session, transcript: list[str]):
    result = await summarize_safely("\n".join(transcript), rolling=True,
                                    source_language=session.language,
                                    output_language=session.summary_language)
    result = enrich_summary_timeline(storage, session.id, result, rolling=True)
    session.latest_summary = result
    payload = event(
        MessageType.MEETING_UPDATE,
        result=result,
        summary=result["summary"],
        decisions=result["decisions"],
        action_items=result["action_items"],
        final=False,
        source_language=session.language,
        summary_language=session.summary_language,
    )
    await send_json(ws, session, payload)
    await session.broadcast(payload)
    logger.info(
        "rolling_summary_sent session_id=%s final_segments=%d",
        session.id, len(transcript),
    )


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
    """补洞（gap/bulk）应用后的回调：持久化 + 原位推送；录制中另刷滚动纪要。最终纪要不在这里出。"""
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
                "UPDATE device_sessions SET revision=?,caption_revision=?,updated_at=?"
                " WHERE server_session_id=?",
                (revision, revision, now, session_id))
            for segment in patch.added:
                conn.execute(
                    "UPDATE segments SET revision=?,caption_revision=?"
                    " WHERE session_id=? AND seg_id=?",
                    (revision, revision, session_id, segment.seg_id))
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
    # 3) 录制中：把补回的内容并入滚动纪要（用户核心场景：打电话回来洞填上、纪要跟着更新）
    if live:
        result = await summarize_safely("\n".join(timeline.to_transcript_lines()), rolling=True,
                                        source_language=s.language, output_language=s.summary_language)
        result["summary_stage"] = "rolling"
        s.latest_summary = result
        upd = event(MessageType.MEETING_UPDATE, result=result, summary=result["summary"],
                    decisions=result["decisions"], action_items=result["action_items"], final=False,
                    summary_stage="rolling")
        with suppress(Exception):
            await send_json(s.live_ws, s, upd)
        await s.broadcast(upd)
        return
    # 4) 已结束、挂在 finalizing 等补录的会议：补录段"适当总结插入"现有纪要
    #    （增量小调用，不重发全文；历史页立即可看，结果页还开着就同步推送升级）
    fs = lifecycle.finalizing.get(session_id)
    if fs is not None and patch.added:
        gap_text = "\n".join(p.get("text", "") for p in patch.to_dict().get("patches", []) if p.get("text"))
        merged = await llm.merge_gap(fs.latest_summary or {}, gap_text,
                                     source_language=fs.language, output_language=fs.summary_language)
        merged["summary_stage"] = "gap_merged"
        fs.latest_summary = merged
        storage.save_summary_draft(session_id, merged)
        logger.info("gap_summary_merged session_id=%s reason=%s", session_id, reason)
        upd = event(MessageType.MEETING_UPDATE, result=merged, summary=merged["summary"],
                    decisions=merged["decisions"], action_items=merged["action_items"], final=False,
                    summary_stage="gap_merged")
        if fs.live_ws is not None:
            with suppress(Exception):
                await send_json(fs.live_ws, fs, upd)
        await fs.broadcast(upd)
    # 5) 会议已完全关闭（罕见：补洞任务重试成功晚于最终纪要，如离线服务恢复后）→ 重出一次纪要覆盖
    elif (s is None and patch.added
          and session_id not in sessions.suspended
          and session_id not in lifecycle.finalizing
          # 设备录音由持久队列的 summarize 完成屏障负责定稿。范围上传期间的
          # 5 分钟预转写绝不能在 /complete 之前触发最终纪要。
          and storage.db.query_one(
              "SELECT 1 FROM device_sessions WHERE server_session_id=?", (session_id,)) is None):
        await offline_queue.enqueue(session_id, 0, 0, "summarize")


async def _on_finalize_summary(session_id: str) -> None:
    """summarize 哨兵：本会话补洞任务（FIFO 在前）已全部应用，出唯一一次最终纪要。"""
    session = lifecycle.pop_finalizing(session_id)
    if session is not None:
        result = await finish_summary(session)
    else:
        # 兜底（server 重启恢复 / finalizing 状态丢失）：从库读转写与元数据出纪要
        meeting = storage.get_meeting(session_id)
        if meeting is None:
            logger.warning("finalize_summary_no_meeting session_id=%s", session_id)
            if storage.db.query_one(
                    "SELECT 1 FROM device_sessions WHERE server_session_id=?"
                    " AND status NOT IN ('done','cancelled')", (session_id,)) is not None:
                raise RuntimeError("meeting metadata is missing during finalization")
            return
        device_session = storage.db.query_one(
            "SELECT speaker_diarization_enabled FROM device_sessions WHERE server_session_id=?",
            (session_id,))
        diarization = None
        if device_session is not None and bool(device_session["speaker_diarization_enabled"]):
            audio_path = data / "audio_cache" / f"{session_id}.b.pcm"
            diarization = await post_meeting_diarizer.correct(
                audio_path, meeting.get("segments") or [])
            if diarization.get("status") == "corrected":
                storage.replace_segments(session_id, diarization["segments"])
                # Post-meeting diarization changes speaker metadata only.  Give
                # it a speaker-channel stamp without making old firmware fetch
                # the same caption text as a new caption.
                with storage.db.transaction() as conn:
                    row = conn.execute(
                        "SELECT revision FROM device_sessions WHERE server_session_id=?",
                        (session_id,)).fetchone()
                    if row is not None:
                        speaker_revision = int(row["revision"]) + 1
                        conn.execute(
                            "UPDATE device_sessions SET revision=?,speaker_revision=?,updated_at=?"
                            " WHERE server_session_id=?",
                            (speaker_revision, speaker_revision,
                             datetime.now(timezone.utc).isoformat(), session_id))
                        conn.execute(
                            "UPDATE segments SET revision=?,speaker_revision=?"
                            " WHERE session_id=?",
                            (speaker_revision, speaker_revision, session_id))
                meeting = storage.get_meeting(session_id) or meeting
        meta = storage.get_meta_info(session_id)
        result = await summarize_safely(transcript_with_time_and_marks(
            meeting.get("segments") or [], meeting.get("marks") or []), rolling=False,
                                        source_language=meta["language"],
                                        output_language=meta["summary_language"])
        result = enrich_summary_timeline(storage, session_id, result, rolling=False)
        result["summary_stage"] = "final"
        result.setdefault("speakers", (diarization or {}).get("speakers") or meta["speakers"])
        storage.merge_summary(session_id, result)
        storage.cleanup_live_audio(session_id)
    if storage.db.query_one(
            "SELECT 1 FROM device_sessions WHERE server_session_id=?", (session_id,)):
        await device_rolling.publish_final(session_id, result)
    logger.info("finalize_summary_done session_id=%s", session_id)
    storage.db.execute(
        "UPDATE device_sessions SET status='done',revision=revision+1,"
        "summary_revision=revision+1,updated_at=?"
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
                                on_gap_done=lambda sid, s, e: storage.delete_gap(sid, s),
                                on_give_up=_on_offline_give_up,
                                db=storage.db)
lifecycle = MeetingLifecycle(sessions, coverage, offline_queue, storage=storage)


async def _finish_summary_logged(session: Session) -> None:
    try:
        await finish_summary(session)
    except Exception:
        logger.exception("http_end_summary_failed session_id=%s", session.id)


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
        if lifecycle.can_finish_inline(session_id, session):
            lifecycle.finish_inline(session_id)
            asyncio.create_task(_finish_summary_logged(session))   # 纪要后台出，HTTP 秒回
        else:
            storage.set_state(session_id, "finalizing")
            storage.set_meta_info(session_id, session.language, session.summary_language,
                                  session.speaker.summary())
            draft = dict(session.latest_summary or {})
            draft.setdefault("summary", "会议已结束，纪要整理中…")
            draft.setdefault("decisions", [])
            draft.setdefault("action_items", [])
            draft.setdefault("mindmap", {"title": "会议重点", "branches": []})
            draft["summary_stage"] = "draft"
            storage.save_summary_draft(session_id, draft)
            await lifecycle.defer_finalize(session_id, session)
        if session.translator is not None:
            asyncio.create_task(session.translator.close())   # 排空已入队译句后退出，不泄漏 worker
            session.translator = None
        logger.info("meeting_end_via_http session_id=%s", session_id)
        return {"ok": True, "state": "finalizing"}
    # 3) 内存无会话：库里查状态；未完成 → 哨兵兜底出纪要（重启恢复同款路径）
    state = storage.get_state(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    if state == "done":
        return {"ok": True, "state": "done"}
    storage.set_state(session_id, "finalizing")
    await offline_queue.enqueue(session_id, 0, 0, "summarize")
    logger.info("meeting_end_via_http_recovered session_id=%s prev_state=%s", session_id, state)
    return {"ok": True, "state": "finalizing"}
