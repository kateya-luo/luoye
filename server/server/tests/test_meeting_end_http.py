import asyncio
import os
import tempfile
import unittest

# 必须在 import ws_gateway 前设好（模块级 Storage/SessionManager 按 DATA_DIR 初始化）
os.environ.setdefault("ASR_MODE", "mock")
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from fastapi import FastAPI                 # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

from app import ws_gateway                  # noqa: E402


def _client():
    app = FastAPI()
    app.include_router(ws_gateway.router)
    return TestClient(app)


class TestMeetingEndHttp(unittest.TestCase):
    """meeting_end 的 HTTP 兜底（欠账三）：WS 死了结束信号也能送达，不再等 2 小时。"""

    def setUp(self):
        self.client = _client()

    def test_unknown_session_404(self):
        r = self.client.post("/api/v1/sessions/deadbeefdeadbeefdeadbeefdeadbeef/end")
        self.assertEqual(r.status_code, 404)

    def test_invalid_session_id_400(self):
        r = self.client.post("/api/v1/sessions/../etc/end")
        self.assertIn(r.status_code, (400, 404))   # 路径非法：拒绝

    def test_unfinished_meeting_goes_finalizing(self):
        """server 重启后内存无会话：库里未完成的会议 → 置 finalizing + 哨兵出纪要。"""
        sid = "a1b2c3d4e5f60718a1b2c3d4e5f60718"
        ws_gateway.storage.create_meeting(sid)
        ws_gateway.storage.set_state(sid, "suspended")
        r = self.client.post(f"/api/v1/sessions/{sid}/end")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "finalizing")
        self.assertEqual(ws_gateway.storage.get_state(sid), "finalizing")

    def test_done_meeting_idempotent(self):
        sid = "b1b2c3d4e5f60718a1b2c3d4e5f60718"
        ws_gateway.storage.create_meeting(sid)
        ws_gateway.storage.save_summary(sid, {"summary": "x", "decisions": [],
                                              "action_items": [], "mindmap": {}})
        r = self.client.post(f"/api/v1/sessions/{sid}/end")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "done")
        self.assertEqual(ws_gateway.storage.get_state(sid), "done")   # 不被改回 finalizing

    def test_repeat_end_stays_finalizing(self):
        """重复调用幂等：不 404、不报错。"""
        sid = "c1b2c3d4e5f60718a1b2c3d4e5f60718"
        ws_gateway.storage.create_meeting(sid)
        ws_gateway.storage.set_state(sid, "recording")
        r1 = self.client.post(f"/api/v1/sessions/{sid}/end")
        r2 = self.client.post(f"/api/v1/sessions/{sid}/end")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["state"], "finalizing")

    def test_browser_canonical_result_does_not_require_device_session(self):
        sid = "d1b2c3d4e5f60718a1b2c3d4e5f60718"
        ws_gateway.storage.create_meeting(sid)
        ws_gateway.storage.set_audio_end(sid, 2000)
        ws_gateway.storage.set_speaker_diarization(sid, False)
        asyncio.run(ws_gateway._on_canonical_finalized(sid, {
            "pipeline_version": "test-canonical-v2",
            "canonical_sha256": "",
            "segments": [{"start_ms": 0, "end_ms": 1800, "text": "网页在线会议定稿",
                          "speaker_id": "speaker_0"}],
        }))
        segments = ws_gateway.storage.load_segments(sid)
        self.assertEqual([item["text"] for item in segments], ["网页在线会议定稿"])
        self.assertIsNone(segments[0]["speaker_id"])
        self.assertEqual(segments[0]["source"], "offline_canonical")


if __name__ == "__main__":
    unittest.main()
