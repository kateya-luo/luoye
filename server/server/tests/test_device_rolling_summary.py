import asyncio
import tempfile
import unittest
from pathlib import Path

from app.device_rolling_summary import DeviceRollingSummaryCoordinator
from app.storage import Storage


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


class DeviceRollingSummaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name))
        self.storage.create_meeting("ly-test", owner_user_id="TEST1")

    async def asyncTearDown(self):
        self.storage.db.close()
        self.temp.cleanup()

    def add_segment(self, index, text):
        self.storage.upsert_segment("ly-test", {
            "seg_id": f"seg-{index}", "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000, "text": text,
            "source": "live", "state": "provisional", "revision": index + 1,
        })

    async def wait_until(self, predicate, timeout=1.0):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_two_final_segments_persist_and_publish_rolling_minutes(self):
        calls = []

        async def summarize(transcript, **kwargs):
            calls.append((transcript, kwargs))
            return {"summary": "滚动摘要", "decisions": [], "action_items": [],
                    "mindmap": {"title": "重点", "branches": []}}

        coordinator = DeviceRollingSummaryCoordinator(
            self.storage, summarize, min_segments=2, max_wait_seconds=10)
        ws = FakeWebSocket()
        coordinator.subscribe("ly-test", ws)
        self.add_segment(0, "第一句")
        await coordinator.on_caption("ly-test", {"text": "第一句", "revision": 1})
        self.add_segment(1, "第二句")
        await coordinator.on_caption("ly-test", {"text": "第二句", "revision": 2})
        await self.wait_until(lambda: len(calls) == 1)
        await self.wait_until(lambda: any(m["type"] == "meeting_update" for m in ws.messages))

        meeting = self.storage.get_meeting("ly-test")
        self.assertEqual(meeting["summary"]["summary"], "滚动摘要")
        self.assertEqual(meeting["summary"]["summary_stage"], "rolling")
        self.assertEqual(meeting["summary"]["rolling_segment_count"], 2)
        self.assertEqual(meeting["summary"]["timeline_schema"], 3)
        self.assertTrue(calls[0][1]["rolling"])
        self.assertIn("第一句", calls[0][0])
        self.assertIn("第二句", calls[0][0])
        await coordinator.finish_input("ly-test")

    async def test_one_segment_flushes_at_max_wait(self):
        calls = []

        async def summarize(transcript, **kwargs):
            calls.append(transcript)
            return {"summary": transcript, "decisions": [], "action_items": [],
                    "mindmap": {"title": "重点", "branches": []}}

        coordinator = DeviceRollingSummaryCoordinator(
            self.storage, summarize, min_segments=2, max_wait_seconds=0.05)
        self.add_segment(0, "只有一句")
        await coordinator.on_caption("ly-test", {"text": "只有一句", "revision": 1})
        await self.wait_until(lambda: len(calls) == 1)
        self.assertEqual(calls, ["[anchor=S0001 t=0ms] 只有一句"])
        await coordinator.finish_input("ly-test")

    async def test_captions_arriving_during_llm_call_are_not_lost(self):
        calls = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def summarize(transcript, **kwargs):
            calls.append(transcript)
            if len(calls) == 1:
                first_started.set()
                await release_first.wait()
            return {"summary": transcript, "decisions": [], "action_items": [],
                    "mindmap": {"title": "重点", "branches": []}}

        coordinator = DeviceRollingSummaryCoordinator(
            self.storage, summarize, min_segments=2, max_wait_seconds=10)
        for index in range(2):
            self.add_segment(index, f"句子{index}")
            await coordinator.on_caption("ly-test", {"text": f"句子{index}",
                                                       "revision": index + 1})
        await asyncio.wait_for(first_started.wait(), timeout=1)
        for index in range(2, 4):
            self.add_segment(index, f"句子{index}")
            await coordinator.on_caption("ly-test", {"text": f"句子{index}",
                                                       "revision": index + 1})
        release_first.set()
        await self.wait_until(lambda: len(calls) == 2)
        self.assertNotIn("句子3", calls[0])
        self.assertIn("句子3", calls[1])
        await coordinator.finish_input("ly-test")

    async def test_partial_replaces_observer_text_without_waking_summary(self):
        calls = []

        async def summarize(transcript, **kwargs):
            calls.append(transcript)
            return {"summary": transcript, "decisions": [], "action_items": [],
                    "mindmap": {"title": "重点", "branches": []}}

        coordinator = DeviceRollingSummaryCoordinator(
            self.storage, summarize, min_segments=1, max_wait_seconds=0.05)
        ws = FakeWebSocket()
        coordinator.subscribe("ly-test", ws)

        await coordinator.on_partial("ly-test", {
            "active": True,
            "text": "现在我们讨论上传速度",
            "display_revision": 7,
            "start_ms": 1200,
            "end_ms": 2400,
        })
        await asyncio.sleep(0.08)

        self.assertEqual(calls, [])
        self.assertEqual(len(ws.messages), 1)
        message = ws.messages[0]
        self.assertEqual(message["type"], "asr_result")
        self.assertFalse(message["is_final"])
        self.assertTrue(message["partial_replace"])
        self.assertEqual(message["text"], "现在我们讨论上传速度")
        self.assertEqual(message["display_revision"], 7)
        await coordinator.shutdown()

    async def test_speaker_update_is_upsert_event_not_second_final(self):
        calls = []

        async def summarize(transcript, **kwargs):
            calls.append(transcript)
            return {"summary": transcript, "decisions": [], "action_items": [],
                    "mindmap": {"title": "重点", "branches": []}}

        coordinator = DeviceRollingSummaryCoordinator(
            self.storage, summarize, min_segments=1, max_wait_seconds=0.05)
        ws = FakeWebSocket()
        coordinator.subscribe("ly-test", ws)

        await coordinator.on_caption("ly-test", {
            "update_kind": "speaker",
            "seg_id": "seg-1",
            "speaker_id": "SPEAKER_00",
            "speaker_label": "说话人1",
            "revision": 9,
        })
        await asyncio.sleep(0.08)

        self.assertEqual(calls, [])
        self.assertEqual(len(ws.messages), 1)
        self.assertEqual(ws.messages[0]["type"], "segment_update")
        self.assertEqual(ws.messages[0]["seg_id"], "seg-1")
        self.assertEqual(ws.messages[0]["speaker_revision"], 9)
        await coordinator.shutdown()


if __name__ == "__main__":
    unittest.main()
