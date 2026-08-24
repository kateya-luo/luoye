from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .protocol import MessageType, event
from .timeline_chapters import enrich_summary_timeline, transcript_with_time_and_marks

logger = logging.getLogger("ai_recorder.device_rolling")

Summarizer = Callable[..., Awaitable[dict[str, Any]]]


@dataclass
class _RollingState:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    runner: asyncio.Task | None = None
    first_pending_at: float | None = None
    last_summarized_count: int = 0
    accepting: bool = True
    observers: set[Any] = field(default_factory=set)


class DeviceRollingSummaryCoordinator:
    """Non-blocking rolling minutes for recorder-card HTTP sessions."""

    def __init__(self, storage: Any, summarizer: Summarizer, *,
                 min_segments: int = 2, max_wait_seconds: float = 20.0) -> None:
        self.storage = storage
        self.summarizer = summarizer
        self.min_segments = max(1, int(min_segments))
        self.max_wait_seconds = max(0.05, float(max_wait_seconds))
        self._states: dict[str, _RollingState] = {}

    def _state(self, session_id: str) -> _RollingState:
        state = self._states.get(session_id)
        if state is not None:
            return state
        meeting = self.storage.get_meeting(session_id)
        summary = (meeting or {}).get("summary") or {}
        prior_count = int(summary.get("rolling_segment_count") or 0)
        state = _RollingState(last_summarized_count=max(0, prior_count))
        self._states[session_id] = state
        return state

    async def on_caption(self, session_id: str, caption: dict[str, Any]) -> None:
        """Publish one final caption and wake the per-session summary worker."""
        state = self._state(session_id)
        if caption.get("update_kind") == "speaker":
            await self._broadcast(session_id, event(
                "segment_update",
                update_kind="speaker",
                seg_id=caption.get("seg_id"),
                speaker_id=caption.get("speaker_id"),
                speaker_label=caption.get("speaker_label"),
                speaker_revision=int(caption.get("revision") or 0),
            ))
            return
        await self._broadcast(session_id, event(
            MessageType.ASR_RESULT,
            text=str(caption.get("text") or ""),
            seq=int(caption.get("revision") or 0),
            mode="device-live",
            is_final=True,
            language=str(caption.get("language") or "auto"),
            speaker_id=caption.get("speaker_id"),
            speaker_label=caption.get("speaker_label"),
            seg_id=caption.get("seg_id"),
            start_ms=int(caption.get("start_ms") or 0),
            end_ms=int(caption.get("end_ms") or 0),
        ))
        if not state.accepting:
            return
        if state.first_pending_at is None:
            state.first_pending_at = time.monotonic()
        state.event.set()
        if state.runner is None or state.runner.done():
            state.runner = asyncio.create_task(
                self._run(session_id, state),
                name=f"device-rolling-{session_id[:32]}",
            )

    async def on_partial(self, session_id: str, partial: dict[str, Any]) -> None:
        """Replace (never append) the recorder's current hypothesis for observers."""
        self._state(session_id)
        await self._broadcast(session_id, event(
            MessageType.ASR_RESULT,
            text=str(partial.get("text") or ""),
            seq=int(partial.get("display_revision") or 0),
            mode="device-live-partial",
            is_final=False,
            partial_replace=True,
            active=bool(partial.get("active")),
            display_revision=int(partial.get("display_revision") or 0),
            start_ms=int(partial.get("start_ms") or 0),
            end_ms=int(partial.get("end_ms") or 0),
        ))

    async def _run(self, session_id: str, state: _RollingState) -> None:
        try:
            while state.accepting:
                await state.event.wait()
                state.event.clear()
                while state.accepting:
                    segments = [item for item in self.storage.load_segments(session_id)
                                if str(item.get("text") or "").strip()]
                    count = len(segments)
                    pending = count - state.last_summarized_count
                    if pending <= 0:
                        state.first_pending_at = None
                        break
                    if state.first_pending_at is None:
                        state.first_pending_at = time.monotonic()
                    elapsed = time.monotonic() - state.first_pending_at
                    if pending < self.min_segments and elapsed < self.max_wait_seconds:
                        try:
                            await asyncio.wait_for(
                                state.event.wait(),
                                timeout=self.max_wait_seconds - elapsed,
                            )
                            state.event.clear()
                            continue
                        except asyncio.TimeoutError:
                            pass

                    meta = self.storage.get_meta_info(session_id)
                    meeting = self.storage.get_meeting(session_id) or {}
                    transcript = transcript_with_time_and_marks(
                        segments, meeting.get("marks") or [])
                    result = await self.summarizer(
                        transcript,
                        rolling=True,
                        source_language=meta["language"],
                        output_language=meta["summary_language"],
                    )
                    if not state.accepting:
                        break
                    result = enrich_summary_timeline(
                        self.storage, session_id, dict(result), rolling=True)
                    result["summary_stage"] = "rolling"
                    result["rolling_segment_count"] = count
                    self.storage.save_summary_draft(session_id, result)
                    self.storage.db.execute(
                        "UPDATE device_sessions SET revision=revision+1,"
                        "summary_revision=revision+1,updated_at=CURRENT_TIMESTAMP"
                        " WHERE server_session_id=?",
                        (session_id,),
                    )
                    state.last_summarized_count = count
                    state.first_pending_at = None
                    await self._broadcast(session_id, event(
                        MessageType.MEETING_UPDATE,
                        result=result,
                        summary=result.get("summary", ""),
                        decisions=result.get("decisions", []),
                        action_items=result.get("action_items", []),
                        final=False,
                        summary_stage="rolling",
                        source_language=meta["language"],
                        summary_language=meta["summary_language"],
                    ))
                    logger.info(
                        "device_rolling_summary_sent session_id=%s final_segments=%d",
                        session_id, count,
                    )
                    current_count = sum(
                        1 for item in self.storage.load_segments(session_id)
                        if str(item.get("text") or "").strip()
                    )
                    if current_count > count:
                        state.first_pending_at = time.monotonic()
                        state.event.set()
                        continue
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("device_rolling_summary_worker_failed session_id=%s", session_id)
        finally:
            state.runner = None

    async def finish_input(self, session_id: str) -> None:
        """Stop rolling work before the authoritative final-summary job starts."""
        state = self._states.get(session_id)
        if state is None:
            return
        state.accepting = False
        state.event.set()
        task = state.runner
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        state.runner = None

    async def on_mark(self, session_id: str) -> None:
        """Attach a newly uploaded MARK without waiting for another LLM cycle."""
        meeting = self.storage.get_meeting(session_id) or {}
        current = meeting.get("summary") or {}
        if not current:
            return
        result = enrich_summary_timeline(
            self.storage, session_id, current,
            rolling=current.get("summary_stage") != "final")
        self.storage.save_summary_draft(session_id, result)
        self.storage.db.execute(
            "UPDATE device_sessions SET revision=revision+1,"
            "summary_revision=revision+1,updated_at=CURRENT_TIMESTAMP"
            " WHERE server_session_id=?",
            (session_id,),
        )
        meta = self.storage.get_meta_info(session_id)
        await self._broadcast(session_id, event(
            MessageType.MEETING_UPDATE,
            result=result,
            summary=result.get("summary", ""),
            decisions=result.get("decisions", []),
            action_items=result.get("action_items", []),
            final=False,
            summary_stage=result.get("summary_stage", "rolling"),
            source_language=meta["language"],
            summary_language=meta["summary_language"],
        ))

    async def publish_final(self, session_id: str, result: dict[str, Any]) -> None:
        await self._broadcast(session_id, event(
            MessageType.MEETING_RESULT,
            result=result,
            summary=result.get("summary", ""),
            decisions=result.get("decisions", []),
            action_items=result.get("action_items", []),
            final=True,
            summary_stage="final",
        ))

    def live_owner(self, session_id: str) -> str | None:
        row = self.storage.db.query_one(
            "SELECT owner_user_id FROM device_sessions WHERE server_session_id=?"
            " AND status IN ('uploading','processing')",
            (session_id,),
        )
        return str(row["owner_user_id"]) if row else None

    def catchup(self, session_id: str) -> dict[str, Any]:
        meeting = self.storage.get_meeting(session_id) or {}
        row = self.storage.db.query_one(
            "SELECT partial_caption,partial_start_ms,partial_end_ms,display_revision"
            " FROM device_sessions WHERE server_session_id=?", (session_id,))
        return event(
            "observer_catchup",
            segments=meeting.get("segments") or [],
            transcript=meeting.get("transcript") or [],
            summary=None if meeting.get("summary_pending") else meeting.get("summary"),
            partial=({
                "active": bool(row["partial_caption"]),
                "text": str(row["partial_caption"] or ""),
                "start_ms": int(row["partial_start_ms"] or 0),
                "end_ms": int(row["partial_end_ms"] or 0),
                "display_revision": int(row["display_revision"] or 0),
            } if row is not None else None),
        )

    def subscribe(self, session_id: str, ws: Any) -> None:
        self._state(session_id).observers.add(ws)

    def unsubscribe(self, session_id: str, ws: Any) -> None:
        state = self._states.get(session_id)
        if state is None:
            return
        state.observers.discard(ws)
        if not state.accepting and not state.observers and state.runner is None:
            self._states.pop(session_id, None)

    async def _broadcast(self, session_id: str, payload: dict[str, Any]) -> None:
        state = self._states.get(session_id)
        if state is None or not state.observers:
            return
        dead: set[Any] = set()
        for ws in tuple(state.observers):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        state.observers -= dead

    async def shutdown(self) -> None:
        tasks = []
        for state in self._states.values():
            state.accepting = False
            state.event.set()
            if state.runner is not None and not state.runner.done():
                state.runner.cancel()
                tasks.append(state.runner)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
