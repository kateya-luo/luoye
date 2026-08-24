import asyncio
import unittest

from app.audio_upload_api import CoverageTracker
from app.lifecycle import MeetingLifecycle


class FakeSession:
    def __init__(self, sid):
        self.id = sid
        self.pending_gaps = []
        self.timeline = None


class FakeSessions:
    def __init__(self):
        self.live = {}
        self.suspended = {}

    def get(self, sid):
        return self.live.get(sid)


class FakeQueue:
    def __init__(self):
        self.jobs = []
        self._busy = False

    async def enqueue(self, sid, start_ms, end_ms, reason):
        self.jobs.append((sid, start_ms, end_ms, reason))

    def busy(self, sid):
        return self._busy


class FakeStorage:
    def __init__(self):
        self.states = {}

    def unfinished_meetings(self):
        return [{"session_id": "m1", "state": "suspended"}]

    def list_gaps(self, sid):
        return [(sid, 10000, 20000)]

    def set_state(self, sid, state):
        self.states[sid] = state


def make():
    sessions, coverage, queue = FakeSessions(), CoverageTracker(), FakeQueue()
    lc = MeetingLifecycle(sessions, coverage, queue, min_gap_ms=1000)
    return sessions, coverage, queue, lc


class LifecycleTest(unittest.TestCase):
    def test_tiny_gap_not_registered(self):
        sessions, coverage, queue, lc = make()
        s = FakeSession("m1")
        lc.register_gap(s, 1000, 1500)  # 0.5s < min_gap_ms
        self.assertEqual(s.pending_gaps, [])

    def test_gap_transcribed_only_when_audio_covered(self):
        sessions, coverage, queue, lc = make()
        s = FakeSession("m1")
        sessions.live["m1"] = s
        lc.register_gap(s, 15000, 25000)

        # 音频还没补齐：不入队
        coverage.add("m1", 0, 15000)
        asyncio.run(lc.on_upload_progress("m1"))
        self.assertEqual(queue.jobs, [])

        # 断网段音频补齐：立即只转这个洞（录制中）
        coverage.add("m1", 15000, 26000)
        asyncio.run(lc.on_upload_progress("m1"))
        self.assertEqual(queue.jobs, [("m1", 15000, 25000, "gap")])
        self.assertEqual(s.pending_gaps, [])

    def test_inline_finish_requires_audio_complete_and_no_gaps(self):
        sessions, coverage, queue, lc = make()
        s = FakeSession("m1")
        self.assertFalse(lc.can_finish_inline("m1", s))          # 音频未完整
        asyncio.run(lc.on_audio_complete("m1", 60000))
        self.assertTrue(lc.can_finish_inline("m1", s))            # 完整且无洞
        s.pending_gaps.append((10000, 20000))
        self.assertFalse(lc.can_finish_inline("m1", s))           # 有洞
        s.pending_gaps.clear()
        queue._busy = True
        self.assertFalse(lc.can_finish_inline("m1", s))           # 队列忙（在跑的补洞任务）

    def test_deferred_finalize_orders_gaps_before_sentinel(self):
        sessions, coverage, queue, lc = make()
        s = FakeSession("m1")
        s.pending_gaps.append((15000, 25000))
        coverage.add("m1", 0, 60000)

        # 会议先结束、音频后到齐：defer 时不调度
        asyncio.run(lc.defer_finalize("m1", s))
        self.assertEqual(queue.jobs, [])
        # final=1 到达：残余洞 + summarize 哨兵按序入队
        asyncio.run(lc.on_audio_complete("m1", 60000))
        self.assertEqual(queue.jobs, [("m1", 15000, 25000, "gap"), ("m1", 0, 0, "summarize")])
        # 哨兵取回 finalizing 会话并清理状态
        self.assertIs(lc.pop_finalizing("m1"), s)
        self.assertNotIn("m1", lc.audio_complete)

    def test_audio_complete_before_end_then_defer_schedules_immediately(self):
        sessions, coverage, queue, lc = make()
        s = FakeSession("m1")
        asyncio.run(lc.on_audio_complete("m1", 60000))
        asyncio.run(lc.defer_finalize("m1", s))
        self.assertEqual(queue.jobs, [("m1", 0, 0, "summarize")])

    def test_wait_audio_complete(self):
        sessions, coverage, queue, lc = make()
        asyncio.run(lc.on_audio_complete("m1", 1000))
        self.assertTrue(asyncio.run(lc.wait_audio_complete("m1", timeout=0.01)))
        self.assertFalse(asyncio.run(lc.wait_audio_complete("m2", timeout=0.01)))

    def test_recovery_waits_for_uncovered_gap_then_summarizes(self):
        sessions, coverage, queue, _ = make()
        storage = FakeStorage()
        lc = MeetingLifecycle(sessions, coverage, queue, storage=storage, min_gap_ms=1000)

        asyncio.run(lc.recover_unfinished(delay=0))
        self.assertEqual(queue.jobs, [])
        self.assertEqual(lc.recovering_gaps["m1"], {(10000, 20000)})
        self.assertEqual(storage.states["m1"], "finalizing")

        coverage.add("m1", 0, 20000)
        asyncio.run(lc.on_upload_progress("m1"))
        self.assertEqual(queue.jobs, [
            ("m1", 10000, 20000, "gap"),
            ("m1", 0, 0, "summarize"),
        ])
        self.assertNotIn("m1", lc.recovering_gaps)


class CoverageContainsTest(unittest.TestCase):
    def test_contains(self):
        c = CoverageTracker()
        c.add("s", 0, 15000)
        c.add("s", 25000, 30000)
        self.assertTrue(c.contains("s", 0, 15000))
        self.assertFalse(c.contains("s", 15000, 25000))   # 洞未补
        c.add("s", 15000, 25000)
        self.assertTrue(c.contains("s", 15000, 25000))    # 补齐（区间合并后包含）


if __name__ == "__main__":
    unittest.main()
