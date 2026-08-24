"""会议生命周期协调器：补洞触发模型（架构评审决策 1）。

修复的旧问题：
- finalize(整场重转) 由 final=1 直接触发，与 meeting_end 的落盘赛跑——短会议时
  补洞成果会被随后的 save_transcript 覆盖（竞态丢数据）；
- 整场重转浪费：fill_gaps 丢弃与实时段重叠的绝大多数结果，且整场按字数摊
  时间戳在长会议（含静默）严重漂移；
- 录制中补洞未接线：打电话回来要等会议结束才见到洞被填上；
- 每场会议最终纪要 DeepSeek 算两次。

新模型：
- 录制中：重连时 register_gap() 注册断网洞；通道B每片上传后 on_upload_progress()
  检查洞的音频是否补齐（CoverageTracker.contains），补齐立即只转该洞
  （reason="gap"，apply_offline 原位替换，base_offset 精确）。
- 会议结束：meeting_end 先落盘 transcript，再问 can_finish_inline()——
  「音频完整 && 无未处理洞 && 队列空闲」→ 内联出一次最终纪要（常见路径，与旧 UX 一致）；
  否则 defer_finalize()：会话挂入 finalizing，等音频完整后仅转残余洞，
  队列尾部的 summarize 哨兵（FIFO 保证在补洞之后）出唯一一次最终纪要。
- 竞态消除：transcript 先落盘、任何任务后入队；单 worker FIFO 保证补洞→纪要顺序。
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from typing import Any

logger = logging.getLogger("ai_recorder.lifecycle")


class MeetingLifecycle:
    def __init__(self, sessions: Any, coverage: Any, queue: Any, storage: Any = None,
                 min_gap_ms: int = 1000, orphan_timeout: float = 600) -> None:
        self.sessions = sessions          # SessionManager
        self.coverage = coverage          # CoverageTracker
        self.queue = queue                # OfflineJobQueue
        self.storage = storage            # 持久化断网洞/会议状态（None=纯内存，测试用）
        self.min_gap_ms = min_gap_ms      # 小于此值的洞不值得开转写任务
        self.orphan_timeout = orphan_timeout  # 会议结束后 final 分片迟迟不到的兜底时限
        self.audio_complete: dict[str, int] = {}          # sid -> 音频总时长 end_ms
        self._audio_events: dict[str, asyncio.Event] = {}
        self.ended: set[str] = set()
        self.finalizing: dict[str, Any] = {}              # 已结束、等残余补洞+最终纪要的 Session
        # server 重启后没有内存 Session；这里保留尚未收到音频的持久化洞，迟到分片到齐后继续补洞。
        self.recovering_gaps: dict[str, set[tuple[int, int]]] = {}
        self.recovery_fallback_seconds = max(60, int(os.getenv("RECOVERY_GAP_FALLBACK_SECONDS", "86400")))
        self._recovery_fallback_sent: set[str] = set()

    # ---- 录制中 ----
    def register_gap(self, session: Any, start_ms: int, end_ms: int) -> None:
        """重连时注册断网洞（由 ws_gateway 的 timeline_advance 调用）。"""
        if end_ms - start_ms >= self.min_gap_ms:
            session.pending_gaps.append((int(start_ms), int(end_ms)))
            if self.storage is not None:
                self.storage.add_gap(session.id, start_ms, end_ms)   # 持久化：重启后仍可恢复补洞
            logger.info("gap_registered session_id=%s range=[%d,%d)", session.id, start_ms, end_ms)

    def _find_session(self, session_id: str):
        return (self.sessions.get(session_id)
                or self.sessions.suspended.get(session_id)
                or self.finalizing.get(session_id))

    async def on_upload_progress(self, session_id: str) -> None:
        """通道B每片上传后：断网洞的音频补齐了就立即只转这个洞（录制中原位填）。"""
        s = self._find_session(session_id)
        if s is not None and s.pending_gaps:
            for gap in [g for g in s.pending_gaps if self.coverage.contains(session_id, *g)]:
                s.pending_gaps.remove(gap)
                await self.queue.enqueue(session_id, gap[0], gap[1], "gap")

        # 重启恢复没有内存 Session，不能因为当前找不到会话就丢掉迟到音频。
        pending = self.recovering_gaps.get(session_id)
        if pending:
            for gap in [g for g in pending if self.coverage.contains(session_id, *g)]:
                pending.remove(gap)
                await self.queue.enqueue(session_id, gap[0], gap[1], "gap")
            if not pending:
                self.recovering_gaps.pop(session_id, None)
                await self.queue.enqueue(session_id, 0, 0, "summarize")

    # ---- 音频完整（final=1）----
    async def on_audio_complete(self, session_id: str, end_ms: int) -> None:
        self.audio_complete[session_id] = end_ms
        self._audio_events.setdefault(session_id, asyncio.Event()).set()
        await self.on_upload_progress(session_id)   # 尾片也可能正好补齐某个洞
        await self._maybe_schedule_residual(session_id)

    async def wait_audio_complete(self, session_id: str, timeout: float) -> bool:
        """meeting_end 短暂等音频尾片（常见情况几百毫秒内已到），超时走延迟定稿。"""
        if session_id in self.audio_complete:
            return True
        ev = self._audio_events.setdefault(session_id, asyncio.Event())
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ev.wait(), timeout)
        return session_id in self.audio_complete

    # ---- 会议结束 ----
    def can_finish_inline(self, session_id: str, session: Any) -> bool:
        """音频完整 && 无未处理洞 && 队列无本会话任务 → 可以当场出最终纪要。"""
        return (session_id in self.audio_complete
                and not session.pending_gaps
                and not self.queue.busy(session_id))

    def finish_inline(self, session_id: str) -> None:
        self._cleanup(session_id)

    async def defer_finalize(self, session_id: str, session: Any) -> None:
        """挂入 finalizing：等音频完整后转残余洞，再由哨兵出最终纪要。"""
        self.ended.add(session_id)
        self.finalizing[session_id] = session
        await self._maybe_schedule_residual(session_id)
        if session_id not in self.audio_complete:
            asyncio.create_task(self._force_finalize_later(session_id))

    async def _maybe_schedule_residual(self, session_id: str) -> None:
        if session_id not in self.ended or session_id not in self.audio_complete:
            return
        s = self.finalizing.get(session_id)
        if s is not None:
            for gap in list(s.pending_gaps):   # 音频已完整，所有注册过的洞都可转
                s.pending_gaps.remove(gap)
                await self.queue.enqueue(session_id, gap[0], gap[1], "gap")
        await self.queue.enqueue(session_id, 0, 0, "summarize")

    async def _force_finalize_later(self, session_id: str) -> None:
        """兜底：客户端结束后死掉、final 分片永远不来 → 超时后用现有内容出纪要，不让历史页卡在'生成中'。"""
        await asyncio.sleep(self.orphan_timeout)
        if session_id in self.finalizing and session_id not in self.audio_complete:
            logger.warning("finalize_forced session_id=%s (final chunk never arrived)", session_id)
            await self.queue.enqueue(session_id, 0, 0, "summarize")

    def pop_finalizing(self, session_id: str):
        s = self.finalizing.pop(session_id, None)
        self._cleanup(session_id)
        return s

    def _cleanup(self, session_id: str) -> None:
        self.audio_complete.pop(session_id, None)
        self._audio_events.pop(session_id, None)
        self.ended.discard(session_id)
        self.recovering_gaps.pop(session_id, None)

    # ---- 重启恢复（server 崩溃/重启后由 startup 调度）----
    async def recover_unfinished(self, delay: float = 90) -> None:
        """扫描库里未完成的会议：洞已覆盖的重新入队补洞，然后补出最终纪要。
        延迟启动，给活跃客户端重连留时间（重连路径会自己恢复时间轴与洞）。"""
        if self.storage is None:
            return
        await asyncio.sleep(delay)
        for m in self.storage.unfinished_meetings():
            sid = m["session_id"]
            if self.sessions.get(sid) or sid in self.sessions.suspended or sid in self.finalizing:
                continue   # 有活体在管，别插手
            waiting: set[tuple[int, int]] = set()
            for _, s, e in self.storage.list_gaps(sid):
                if self.coverage.contains(sid, s, e):
                    await self.queue.enqueue(sid, s, e, "gap")
                else:
                    waiting.add((s, e))
            if waiting:
                # 音频尚未覆盖的洞继续等迟到分片；不能提前把会议总结成 done。
                self.recovering_gaps[sid] = waiting
                asyncio.create_task(self._summarize_recovery_after_timeout(sid))
            else:
                await self.queue.enqueue(sid, 0, 0, "summarize")
            self.storage.set_state(sid, "finalizing")
            logger.info("recovered_unfinished session_id=%s prev_state=%s waiting_gaps=%d",
                        sid, m["state"], len(waiting))

    async def _summarize_recovery_after_timeout(self, session_id: str) -> None:
        """迟到音频超过兜底时限仍未覆盖：先产出带洞纪要；以后音频到齐仍会补洞并重出定稿。"""
        await asyncio.sleep(self.recovery_fallback_seconds)
        if self.recovering_gaps.get(session_id) and session_id not in self._recovery_fallback_sent:
            self._recovery_fallback_sent.add(session_id)
            logger.warning("recovery_gap_timeout session_id=%s pending=%d",
                           session_id, len(self.recovering_gaps[session_id]))
            await self.queue.enqueue(session_id, 0, 0, "summarize")
