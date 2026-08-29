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


class DeviceLiveTranscriptCoordinator:
    """Transcript-only observer hub for recorder-card HTTP sessions.

    The historic rolling-summary class lived here.  V0.21 deliberately keeps
    only caption forwarding; no caption, mark or timer can invoke an LLM.
    """

    def __init__(self, storage: Any, summarizer: Summarizer | None = None, *,
                 min_segments: int = 2, max_wait_seconds: float = 20.0) -> None:
        self.storage = storage
        self.summarizer = None
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
        # Caption forwarding is the complete behavior.  Do not schedule work.

    async def finish_input(self, session_id: str) -> None:
        """Close caption input for a completed recorder session."""
        state = self._states.get(session_id)
        if state is None:
            return
        state.accepting = False
        state.event.set()
        state.runner = None

    async def on_mark(self, session_id: str) -> None:
        """Marks are persisted by the device API; no AI side effect is allowed."""
        return

    async def publish_final(self, session_id: str, result: dict[str, Any] | None = None) -> None:
        await self._broadcast(session_id, event(
            MessageType.MEETING_RESULT,
            result={"state": "transcript_ready"},
            final=True,
            summary_stage="disabled",
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
        return event(
            "observer_catchup",
            segments=meeting.get("segments") or [],
            transcript=meeting.get("transcript") or [],
            summary=None if meeting.get("summary_pending") else meeting.get("summary"),
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


# Import compatibility for older tests and code; behavior is transcript-only.
DeviceRollingSummaryCoordinator = DeviceLiveTranscriptCoordinator
