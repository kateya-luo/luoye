import importlib
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agenda import create_agenda_router
from app.history_api import create_history_router
from app.storage import Storage

class TestAccountIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = Storage(Path(self.temp))
        import app.auth as auth
        auth.configure_auth(self.storage)
        api = FastAPI()
        api.include_router(auth.router)
        api.include_router(create_history_router(self.storage))
        api.include_router(create_agenda_router(self.storage))
        self.client = TestClient(api)
        self.auth = auth

    def tearDown(self):
        self.storage.db.close()
        importlib.reload(self.auth)

    def _login(self, username: str) -> str:
        response = self.client.post("/api/v1/auth/login", json={
            "username": username, "password": "123456",
        })
        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    def test_five_seeded_accounts_use_password_hashes(self):
        rows = self.storage.db.query("SELECT username,password_hash FROM users ORDER BY username")
        self.assertEqual([r["username"] for r in rows], [f"TEST{i}" for i in range(1, 6)])
        self.assertTrue(all(r["password_hash"].startswith("scrypt$") for r in rows))
        self.assertTrue(all("123456" not in r["password_hash"] for r in rows))
        for i in range(1, 6):
            self._login(f"test{i}")  # 用户名不区分大小写

    def test_login_token_contains_identity(self):
        token = self._login("TEST3")
        response = self.client.get("/api/v1/auth/status", headers={"Authorization": f"Bearer {token}"})
        self.assertTrue(response.json()["authenticated"])
        self.assertEqual(response.json()["user"]["username"], "TEST3")

    def test_user_can_change_only_own_password_and_old_token_is_revoked(self):
        old_token = self._login("TEST3")
        response = self.client.post(
            "/api/v1/auth/change-password",
            headers={"Authorization": f"Bearer {old_token}"},
            json={"current_password": "123456", "new_password": "new-password-3"},
        )
        self.assertEqual(response.status_code, 200)
        new_token = response.json()["token"]
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(self.client.get(
            "/api/v1/auth/verify", headers={"Authorization": f"Bearer {old_token}"}).status_code, 401)
        self.assertEqual(self.client.get(
            "/api/v1/auth/verify", headers={"Authorization": f"Bearer {new_token}"}).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={
            "username": "TEST3", "password": "123456",
        }).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={
            "username": "TEST3", "password": "new-password-3",
        }).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={
            "username": "TEST2", "password": "123456",
        }).status_code, 200)

    def test_change_password_rejects_wrong_current_and_short_new_password(self):
        token = self._login("TEST4")
        headers = {"Authorization": f"Bearer {token}"}
        wrong = self.client.post("/api/v1/auth/change-password", headers=headers, json={
            "current_password": "wrong", "new_password": "new-password-4",
        })
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.json()["detail"], "当前密码错误")
        short = self.client.post("/api/v1/auth/change-password", headers=headers, json={
            "current_password": "123456", "new_password": "1234567",
        })
        self.assertEqual(short.status_code, 422)

    def test_meeting_list_detail_export_delete_and_audio_are_isolated(self):
        self.storage.create_meeting("meeting-a", owner_user_id="TEST1")
        self.storage.save_transcript("meeting-a", ["TEST1 的内容"])
        self.storage.set_title("meeting-a", "设备方案：评审会", "TEST1")
        self.storage.create_meeting("meeting-b", owner_user_id="TEST2")
        self.storage.save_transcript("meeting-b", ["TEST2 的内容"])
        audio = Path(self.temp) / "audio_cache" / "meeting-a.b.pcm"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"\0\0" * 100)

        token1, token2 = self._login("TEST1"), self._login("TEST2")
        h1, h2 = {"Authorization": f"Bearer {token1}"}, {"Authorization": f"Bearer {token2}"}
        self.assertEqual([m["session_id"] for m in self.client.get("/api/v1/meetings", headers=h1).json()["meetings"]],
                         ["meeting-a"])
        self.assertEqual([m["session_id"] for m in self.client.get("/api/v1/meetings", headers=h2).json()["meetings"]],
                         ["meeting-b"])
        self.assertEqual(self.client.get("/api/v1/meetings/meeting-a", headers=h2).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/meetings/meeting-a/export", headers=h2).status_code, 404)
        own_export = self.client.get("/api/v1/meetings/meeting-a/export", headers=h1)
        self.assertEqual(own_export.status_code, 200)
        self.assertIn("filename*=UTF-8''%E8%AE%BE%E5%A4%87%E6%96%B9%E6%A1%88-%E8%AF%84%E5%AE%A1%E4%BC%9A.md",
                      own_export.headers["content-disposition"])
        self.assertEqual(self.client.get(f"/api/v1/meetings/meeting-a/audio?token={token2}").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/meetings/meeting-a/audio", headers=h2).status_code, 404)
        self.assertEqual(self.client.delete("/api/v1/meetings/meeting-a", headers=h2).status_code, 404)
        self.assertIsNotNone(self.storage.get_meeting("meeting-a"))

    def test_agenda_and_todos_are_isolated(self):
        token1, token2 = self._login("TEST1"), self._login("TEST2")
        h1, h2 = {"Authorization": f"Bearer {token1}"}, {"Authorization": f"Bearer {token2}"}
        common = {"type": "todo", "start": "2026-07-13T21:00:00+08:00"}
        self.assertEqual(self.client.post("/api/v1/agenda/events", headers=h1,
                                          json=common | {"title": "TEST1 待办"}).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/agenda/events", headers=h2,
                                          json=common | {"title": "TEST2 待办"}).status_code, 200)
        data1 = self.client.get("/api/v1/agenda/today?date=2026-07-13", headers=h1).json()
        data2 = self.client.get("/api/v1/agenda/today?date=2026-07-13", headers=h2).json()
        self.assertEqual([e["title"] for e in data1["events"]], ["TEST1 待办"])
        self.assertEqual([e["title"] for e in data2["events"]], ["TEST2 待办"])


if __name__ == "__main__":
    unittest.main()
