import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.session_manager import SessionManager
from app.sessions_api import create_sessions_router
from app.storage import Storage


class DeviceActiveSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.storage = Storage(root)
        auth.configure_auth(self.storage)
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(create_sessions_router(
            SessionManager(root / "audio_cache"), storage=self.storage,
            prefix="/api/v1/sessions"))
        self.client = TestClient(app)
        login = self.client.post("/api/v1/auth/login",
                                 json={"username": "TEST1", "password": "123456"})
        self.token = login.json()["token"]

    def tearDown(self):
        self.storage.db.close()
        self.temp.cleanup()

    def test_active_device_session_is_visible_to_its_owner(self):
        now = datetime.now(timezone.utc).isoformat()
        self.storage.db.execute(
            "INSERT INTO devices(device_id,owner_user_id,display_name,binding_generation,"
            "capabilities_json,created_at,updated_at) VALUES(?,?,?,1,'[]',?,?)",
            ("LY-TEST", "TEST1", "落叶", now, now))
        self.storage.create_meeting("ly-device-live", owner_user_id="TEST1")
        self.storage.db.execute(
            "INSERT INTO device_sessions(server_session_id,client_session_id,device_id,"
            "owner_user_id,binding_generation,started_at_utc,codec,sample_rate,channels,"
            "bits_per_sample,source_language,upload_mode,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ly-device-live", "local-1", "LY-TEST", "TEST1", 1, now, "pcm_s16le",
             16000, 1, 16, "zh", "live", "uploading", now, now))
        self.storage.db.execute(
            "INSERT INTO device_audio_chunks(server_session_id,seq,sha256,start_sample,"
            "sample_count,byte_count,path,created_at) VALUES(?,0,?,0,160,320,?,?)",
            ("ly-device-live", "0" * 64, "unused-test-path", now))
        response = self.client.get(
            "/api/v1/sessions/active",
            headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200, response.text)
        active = response.json()["sessions"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["session_id"], "ly-device-live")
        self.assertEqual(active[0]["source"], "device")

    def test_stale_uploading_device_session_is_not_reported_as_live(self):
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.storage.db.execute(
            "INSERT INTO devices(device_id,owner_user_id,display_name,binding_generation,"
            "capabilities_json,created_at,updated_at) VALUES(?,?,?,1,'[]',?,?)",
            ("LY-STALE", "TEST1", "旧设备", stale, stale))
        self.storage.create_meeting("ly-stale", owner_user_id="TEST1")
        self.storage.db.execute(
            "INSERT INTO device_sessions(server_session_id,client_session_id,device_id,"
            "owner_user_id,binding_generation,started_at_utc,codec,sample_rate,channels,"
            "bits_per_sample,source_language,upload_mode,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ly-stale", "local-stale", "LY-STALE", "TEST1", 1, stale, "pcm_s16le",
             16000, 1, 16, "zh", "live", "uploading", stale, stale))
        response = self.client.get(
            "/api/v1/sessions/active",
            headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sessions"], [])


if __name__ == "__main__":
    unittest.main()
