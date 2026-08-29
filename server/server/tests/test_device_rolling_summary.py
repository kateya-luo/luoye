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


class DeviceTranscriptOnlyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name))
        self.storage.create_meeting("ly-test", owner_user_id="TEST1")

    async def asyncTearDown(self):
        self.storage.db.close()
        self.temp.cleanup()

    async def test_caption_is_forwarded_without_any_llm_call(self):
        calls = []

        async def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("recording must never call a minutes model")

        coordinator = DeviceRollingSummaryCoordinator(self.storage, forbidden)
        ws = FakeWebSocket()
        coordinator.subscribe("ly-test", ws)
        await coordinator.on_caption("ly-test", {
            "text": "实时字幕", "revision": 1, "seg_id": "s1",
            "start_ms": 0, "end_ms": 1000,
        })
        self.assertEqual(calls, [])
        self.assertEqual(len(ws.messages), 1)
        self.assertEqual(ws.messages[0]["type"], "asr_result")
        self.assertEqual(ws.messages[0]["text"], "实时字幕")

    async def test_mark_and_finish_never_create_summary(self):
        calls = []

        async def forbidden(*args, **kwargs):
            calls.append(1)

        coordinator = DeviceRollingSummaryCoordinator(self.storage, forbidden)
        await coordinator.on_mark("ly-test")
        await coordinator.finish_input("ly-test")
        self.assertEqual(calls, [])
        meeting = self.storage.get_meeting("ly-test")
        self.assertEqual(meeting["minutes_status"], "not_created")
        self.assertEqual(meeting["summary"], {})


if __name__ == "__main__":
    unittest.main()
