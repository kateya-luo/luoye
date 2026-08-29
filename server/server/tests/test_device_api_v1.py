import hashlib
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("ASR_MODE", "mock")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import auth
from app.agenda import AgendaStore, create_agenda_router
from app.device_api_v1 import (MAX_CHUNK_BYTES, MAX_TODO_BYTES,
                               RANGE_BLOCK_BYTES, create_device_v2_router)
from app.storage import Storage


class DeviceV1Test(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = Storage(Path(self.temp))
        auth.configure_auth(self.storage)
        app = FastAPI()
        app.include_router(auth.router)
        self.completed = []
        self.range_committed = []

        async def completed(session_id, end_ms):
            self.completed.append((session_id, end_ms))

        async def range_committed(session_id):
            self.range_committed.append(session_id)

        self.router = create_device_v2_router(
            self.storage, on_session_complete=completed,
            on_audio_range_committed=range_committed)
        app.include_router(self.router)
        app.include_router(create_agenda_router(self.storage, prefix="/api/v1/agenda"))

        @app.exception_handler(HTTPException)
        async def v1_error(_request: Request, exc: HTTPException):
            content = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail \
                else {"detail": exc.detail}
            return JSONResponse(status_code=exc.status_code, content=content)

        self.client = TestClient(app)
        self.client.headers.update({
            "X-Luoye-Protocol": "luoye-device-api/2",
            "X-Luoye-Firmware": "0.6.1",
            "X-Luoye-Device": "LY-AABBCCDDEEFF",
        })
        self.token1 = self._login("TEST1")
        self.token2 = self._login("TEST2")

    def tearDown(self):
        self.storage.db.close()
        importlib.reload(auth)

    def _login(self, username):
        response = self.client.post("/api/v1/auth/login",
                                    json={"username": username, "password": "123456"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    @staticmethod
    def _pair_body(code="123456", nonce="0123456789abcdef0123456789abcdef"):
        return {
            "device_id": "LY-AABBCCDDEEFF", "pairing_code": code, "nonce": nonce,
            "firmware_version": "0.6.1", "hardware_revision": "LY-HW-ENG",
            "capabilities": ["fixed_sd", "offline_upload"],
            "protocol_version": "luoye-device-api/2",
        }

    def _bind(self, code="123456", nonce="0123456789abcdef0123456789abcdef",
              account_token=None):
        body = self._pair_body(code, nonce)
        self.assertEqual(self.client.post("/api/v2/device/pair/start", json=body).status_code, 200)
        headers = {"Authorization": f"Bearer {account_token or self.token1}"}
        claimed = self.client.post("/api/v2/me/devices/claim", headers=headers,
                                   json={"pairing_code": code, "display_name": "会议室落叶"})
        self.assertEqual(claimed.status_code, 200, claimed.text)
        status = self.client.post("/api/v2/device/pair/status",
                                  json={"device_id": body["device_id"], "nonce": nonce})
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.headers.get("cache-control"), "no-store")
        return status.json()

    def test_build_info_contract(self):
        body = self.client.get("/api/v2/build-info").json()
        self.assertEqual(body["server_version"], "0.21.0")
        self.assertEqual(body["protocol_version"], "luoye-device-api/2")
        self.assertEqual(body["device_auth_profile"], "engineering")
        self.assertIn("idempotent_upload", body["capabilities"])
        self.assertIn("storage_management", body["capabilities"])
        self.assertIn("bulk_upload_10mib", body["capabilities"])
        self.assertIn("live_epoch_resume", body["capabilities"])
        self.assertIn("independent_sd_delete", body["capabilities"])
        self.assertNotIn("device_rolling_minutes", body["capabilities"])
        self.assertIn("transcript_only_live_v1", body["capabilities"])
        self.assertIn("template_minutes_v1", body["capabilities"])
        self.assertIn("meeting_memory_v1", body["capabilities"])
        self.assertIn("semantic_timeline_v2", body["capabilities"])
        self.assertIn("semantic_timeline_v3_anchored", body["capabilities"])
        self.assertIn("speaker_backend_readiness", body["capabilities"])
        self.assertIn("offline_asr_pipeline_v1", body["capabilities"])
        self.assertIn("canonical_offline_diarization_v2", body["capabilities"])
        self.assertEqual(RANGE_BLOCK_BYTES, 10 * 1024 * 1024)

    def test_reconnect_starts_new_live_epoch_and_gap_waits_for_manual_repair(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        created = self.client.post(
            "/api/v2/device/sessions",
            headers=device | {"Idempotency-Key": "session:epoch-1:create"},
            json={"client_session_id": "epoch-1", "binding_generation": 1,
                  "upload_mode": "live"})
        self.assertEqual(created.status_code, 200, created.text)
        sid = created.json()["server_session_id"]

        def put_chunk(seq, offset, data):
            return self.client.put(
                f"/api/v2/device/sessions/{sid}/audio/{seq}",
                headers=device | {
                    "Content-Type": "audio/L16;rate=16000;channels=1",
                    "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                    "X-Byte-Offset": str(offset), "X-Byte-Count": str(len(data)),
                }, content=data)

        before = b"\x11\x00" * 160
        gap = b"\x22\x00" * 320
        after = b"\x33\x00" * 160
        tail = b"\x44\x00" * 160
        first = put_chunk(0, 0, before)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["live_acknowledged_bytes"], len(before))

        resumed = self.client.post(
            f"/api/v2/device/sessions/{sid}/live-resume",
            headers=device | {"Idempotency-Key": "epoch-1:resume:320:960"},
            json={"binding_generation": 1, "gap_start_bytes": len(before),
                  "resume_offset_bytes": len(before) + len(gap)})
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertTrue(resumed.json()["gap_pending"])
        self.assertEqual(resumed.json()["live_next_seq"], 1)
        self.assertEqual(resumed.json()["acknowledged_bytes"], len(before))
        self.assertEqual(resumed.json()["live_acknowledged_bytes"],
                         len(before) + len(gap))

        latest = put_chunk(1, len(before) + len(gap), after)
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["acknowledged_bytes"], len(before))
        self.assertEqual(latest.json()["live_acknowledged_bytes"],
                         len(before) + len(gap) + len(after))

        total = len(before) + len(gap) + len(after) + len(tail)
        deferred = self.client.post(
            f"/api/v2/device/sessions/{sid}/defer",
            headers=device | {"Idempotency-Key": "epoch-1:defer"},
            json={"total_bytes": total, "total_samples": total // 2,
                  "ended_at_utc": None, "binding_generation": 1})
        self.assertEqual(deferred.status_code, 200, deferred.text)
        self.assertEqual(deferred.json()["status"], "awaiting_repair")
        self.assertNotIn(
            sid,
            [row["session_id"] for row in self.storage.unfinished_meetings(
                older_than_seconds=-1)],
            "服务器重启恢复不能提前结束仍在等待录音卡手动补洞的会议",
        )
        self.assertEqual(deferred.json()["missing_ranges"], [
            {"offset": len(before), "length": len(gap)},
            {"offset": len(before) + len(gap) + len(after), "length": len(tail)},
        ])

        plan = self.client.post(
            f"/api/v2/device/sessions/{sid}/upload-plan", headers=device,
            json={"total_bytes": total, "total_samples": total // 2,
                  "binding_generation": 1, "mode": "repair"})
        self.assertEqual(plan.status_code, 200, plan.text)
        for offset, data in ((len(before), gap),
                             (len(before) + len(gap) + len(after), tail)):
            repaired = self.client.put(
                f"/api/v2/device/sessions/{sid}/audio-range",
                headers=device | {
                    "Content-Type": "audio/L16;rate=16000;channels=1",
                    "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                    "X-Byte-Offset": str(offset), "X-Byte-Count": str(len(data)),
                }, content=data)
            self.assertEqual(repaired.status_code, 200, repaired.text)
        complete = self.client.post(
            f"/api/v2/device/sessions/{sid}/complete",
            headers=device | {"Idempotency-Key": "epoch-1:complete"},
            json={"total_bytes": total, "total_samples": total // 2,
                  "ended_at_utc": None, "binding_generation": 1})
        self.assertEqual(complete.status_code, 200, complete.text)
        self.assertEqual(
            (Path(self.temp) / "audio_cache" / f"{sid}.b.pcm").read_bytes(),
            before + gap + after + tail)

    def test_v2_bulk_plan_out_of_order_ranges_complete_and_cancel(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        created = self.client.post(
            "/api/v2/device/sessions",
            headers=device | {"Idempotency-Key": "session:bulk-1:create"},
            json={"client_session_id": "bulk-1", "binding_generation": 1,
                  "upload_mode": "bulk",
                  "audio": {"codec": "pcm_s16le", "sample_rate": 16000,
                            "channels": 1, "bits_per_sample": 16}})
        self.assertEqual(created.status_code, 200, created.text)
        sid = created.json()["server_session_id"]
        plan_body = {"total_bytes": 4096, "total_samples": 2048,
                     "binding_generation": 1, "mode": "bulk"}
        plan = self.client.post(
            f"/api/v2/device/sessions/{sid}/upload-plan", headers=device,
            json=plan_body)
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertEqual(plan.json()["missing_ranges"], [{"offset": 0, "length": 4096}])

        first = b"\x11\x00" * 1024
        second = b"\x22\x00" * 1024

        def put_range(offset, data):
            return self.client.put(
                f"/api/v2/device/sessions/{sid}/audio-range",
                headers=device | {
                    "Content-Type": "audio/L16;rate=16000;channels=1",
                    "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                    "X-Byte-Offset": str(offset), "X-Byte-Count": str(len(data)),
                }, content=data)

        out_of_order = put_range(2048, second)
        self.assertEqual(out_of_order.status_code, 200, out_of_order.text)
        self.assertEqual(out_of_order.json()["missing_ranges"],
                         [{"offset": 0, "length": 2048}])
        accepted = put_range(0, first)
        self.assertTrue(accepted.json()["complete"])
        self.assertTrue(put_range(0, first).json()["duplicate"])
        self.assertGreaterEqual(self.range_committed.count(sid), 4)

        complete = self.client.post(
            f"/api/v2/device/sessions/{sid}/complete",
            headers=device | {"Idempotency-Key": "session:bulk-1:complete"},
            json={"total_bytes": 4096, "total_samples": 2048,
                  "ended_at_utc": None, "binding_generation": 1,
                  "file_sha256": hashlib.sha256(first + second).hexdigest()})
        self.assertEqual(complete.status_code, 200, complete.text)
        self.assertEqual(complete.json()["status"], "processing")
        self.assertEqual(self.completed, [(sid, 128)])
        self.assertEqual((Path(self.temp) / "audio_cache" / f"{sid}.b.pcm").read_bytes(),
                         first + second)

        cancelled_create = self.client.post(
            "/api/v2/device/sessions",
            headers=device | {"Idempotency-Key": "session:cancel-1:create"},
            json={"client_session_id": "cancel-1", "binding_generation": 1,
                  "upload_mode": "repair"}).json()
        cancelled_sid = cancelled_create["server_session_id"]
        self.client.post(
            f"/api/v2/device/sessions/{cancelled_sid}/upload-plan", headers=device,
            json={"total_bytes": 2048, "total_samples": 1024,
                  "binding_generation": 1, "mode": "repair"})
        cancelled = self.client.post(
            f"/api/v2/device/sessions/{cancelled_sid}/cancel", headers=device,
            json={"binding_generation": 1, "reason": "local_delete"})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["cancelled"])

    def test_fixed_sd_inventory_owner_isolation_safe_delete_and_ack(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        snapshot = {
            "binding_generation": 1, "scan_id": "scan0001", "scan_start": True,
            "complete": True, "total_bytes": 16_000_000_000,
            "free_bytes": 4_000_000_000,
            "sessions": [
                {"client_session_id": "safe-one", "server_session_id": "lys-safe",
                 "local_bytes": 640044, "ended_at_utc": 1785542400,
                 "upload_state": "done", "deletable": True},
                {"client_session_id": "pending-one", "server_session_id": None,
                 "local_bytes": 320044, "ended_at_utc": 1785542500,
                 "upload_state": "uploading", "deletable": False},
            ],
        }
        reported = self.client.put("/api/v2/device/storage/snapshot",
                                   headers=device, json=snapshot)
        self.assertEqual(reported.status_code, 200, reported.text)
        self.assertIsNone(reported.json()["command"])
        owner = {"Authorization": f"Bearer {self.token1}"}
        other = {"Authorization": f"Bearer {self.token2}"}
        storage = self.client.get(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/storage", headers=owner)
        self.assertEqual(storage.status_code, 200, storage.text)
        self.assertEqual(len(storage.json()["sessions"]), 2)
        self.assertNotIn("policy", storage.json())
        self.assertNotIn("server_session_id", storage.json()["sessions"][0])
        self.assertNotIn("upload_state", storage.json()["sessions"][0])
        unsupported = self.client.post(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/storage/commands", headers=owner,
            json={"action": "cleanup_synced", "session_ids": []})
        self.assertEqual(unsupported.status_code, 422)
        removed_policy = self.client.patch(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/storage/policy", headers=owner,
            json={"auto_cleanup_enabled": True, "retention_days": 30,
                  "trigger_free_percent": 15, "target_free_percent": 20})
        self.assertEqual(removed_policy.status_code, 404)
        self.assertEqual(self.client.get(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/storage", headers=other).status_code, 404)
        queued = self.client.post(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/storage/commands", headers=owner,
            json={"action": "delete_sessions", "session_ids": ["pending-one"]})
        self.assertEqual(queued.status_code, 200, queued.text)
        command_id = queued.json()["command_id"]
        delivered = self.client.put("/api/v2/device/storage/snapshot",
                                    headers=device, json=snapshot).json()["command"]
        self.assertEqual((delivered["command_id"], delivered["session_ids"]),
                         (command_id, ["pending-one"]))
        ack = self.client.post(
            f"/api/v2/device/storage/commands/{command_id}/ack", headers=device,
            json={"binding_generation": 1, "status": "completed",
                  "deleted_session_ids": ["pending-one"], "deleted_count": 1,
                  "freed_bytes": 320044, "error_code": None})
        self.assertEqual(ack.status_code, 200, ack.text)
        remaining = self.client.get(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/storage", headers=owner).json()
        self.assertEqual([item["client_session_id"] for item in remaining["sessions"]],
                         ["safe-one"])

        bulk = self.client.post(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/storage/commands", headers=owner,
            json={"action": "delete_all_closed", "session_ids": []})
        self.assertEqual(bulk.status_code, 200, bulk.text)
        bulk_id = bulk.json()["command_id"]
        delivered = self.client.put("/api/v2/device/storage/snapshot",
                                    headers=device, json=snapshot).json()["command"]
        self.assertEqual((delivered["command_id"], delivered["action"]),
                         (bulk_id, "delete_all_closed"))
        ack = self.client.post(
            f"/api/v2/device/storage/commands/{bulk_id}/ack", headers=device,
            json={"binding_generation": 1, "status": "completed",
                  "deleted_session_ids": [], "deleted_count": 2,
                  "freed_bytes": 960088, "error_code": None})
        self.assertEqual(ack.status_code, 200, ack.text)
        remaining = self.client.get(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/storage", headers=owner).json()
        self.assertEqual(remaining["sessions"], [])

    def test_device_protocol_headers_body_limits_and_zero_chunk(self):
        self.assertGreaterEqual(MAX_CHUNK_BYTES, 163_840)
        self.assertGreaterEqual(MAX_TODO_BYTES, 1024 * 1024)
        body = self._pair_body()
        mismatch = self.client.post("/api/v2/device/pair/start", json=body,
                                    headers={"X-Luoye-Firmware": "different"})
        self.assertEqual((mismatch.status_code, mismatch.json()["error"]["code"]),
                         (400, "FIRMWARE_HEADER_MISMATCH"))
        unsupported = self.client.post("/api/v2/device/pair/start", json=body,
                                       headers={"X-Luoye-Protocol": "legacy"})
        self.assertEqual((unsupported.status_code, unsupported.json()["error"]["code"]),
                         (409, "PROTOCOL_VERSION_UNSUPPORTED"))

        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        wrong_device = self.client.get(
            "/api/v2/device/agenda", headers=device | {
                "X-Luoye-Device": "LY-001122334455"})
        self.assertEqual((wrong_device.status_code, wrong_device.json()["error"]["code"]),
                         (403, "DEVICE_HEADER_MISMATCH"))
        created = self.client.post("/api/v2/device/sessions", headers=device | {
            "Idempotency-Key": "session:limits:create"}, json={
                "client_session_id": "limits", "binding_generation": 1,
                "audio": {"codec": "pcm_s16le", "sample_rate": 16000,
                          "channels": 1, "bits_per_sample": 16}}).json()
        sid = created["server_session_id"]
        tiny = b"\x01\x00"
        common = {"Content-Type": "audio/L16;rate=16000;channels=1",
                  "X-Content-SHA256": hashlib.sha256(tiny).hexdigest(),
                  "X-Byte-Offset": "0"}
        wrong_media = self.client.put(
            f"/api/v2/device/sessions/{sid}/audio/0", headers=device | common | {
                "Content-Type": "application/octet-stream", "X-Byte-Count": "2"},
            content=tiny)
        self.assertEqual((wrong_media.status_code, wrong_media.json()["error"]["code"]),
                         (415, "CONTENT_TYPE_UNSUPPORTED"))
        missing_count = self.client.put(
            f"/api/v2/device/sessions/{sid}/audio/0",
            headers=device | common, content=tiny)
        self.assertEqual((missing_count.status_code, missing_count.json()["error"]["code"]),
                         (400, "AUDIO_BYTE_COUNT_REQUIRED"))
        empty = b""
        empty_result = self.client.put(
            f"/api/v2/device/sessions/{sid}/audio/0", headers=device | {
                "Content-Type": "audio/L16;rate=16000;channels=1",
                "X-Content-SHA256": hashlib.sha256(empty).hexdigest(),
                "X-Byte-Offset": "0", "X-Byte-Count": "0"}, content=empty)
        self.assertEqual((empty_result.status_code, empty_result.json()["error"]["code"]),
                         (422, "AUDIO_CHUNK_EMPTY"))
        oversized = self.client.put(
            f"/api/v2/device/sessions/{sid}/audio/0", headers=device | common | {
                "X-Byte-Count": "2", "Content-Length": str(2 * 1024 * 1024)},
            content=tiny)
        self.assertEqual((oversized.status_code, oversized.json()["error"]["code"]),
                         (413, "AUDIO_CHUNK_TOO_LARGE"))

    def test_voice_todo_client_id_cannot_cross_binding_generation(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        wav = b"RIFF" + (36 + 32).to_bytes(4, "little") + b"WAVEfmt " \
              + (16).to_bytes(4, "little") + b"\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00" \
              + b"data" + (32).to_bytes(4, "little") + b"\0" * 32
        sent = self.client.put(
            "/api/v2/device/todos/reused-id/audio?binding_generation=1", headers=device | {
                "Content-Type": "audio/wav",
                "X-Content-SHA256": hashlib.sha256(wav).hexdigest()}, content=wav)
        self.assertEqual(sent.status_code, 200, sent.text)
        account = {"Authorization": f"Bearer {self.token1}"}
        self.assertEqual(self.client.delete(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/binding", headers=account).status_code, 200)
        rebound = self._bind("654321", "fedcba9876543210fedcba9876543210")
        new_device = {"Authorization": f"Bearer {rebound['device_token']}"}
        leaked = self.client.put(
            "/api/v2/device/todos/reused-id/audio?binding_generation=3", headers=new_device | {
                "Content-Type": "audio/wav",
                "X-Content-SHA256": hashlib.sha256(wav).hexdigest()}, content=wav)
        self.assertEqual((leaked.status_code, leaked.json()["error"]["code"]),
                         (403, "TODO_BINDING_MISMATCH"))

    def test_pair_claim_account_isolation_and_retryable_token_delivery(self):
        first = self._bind()
        self.assertEqual(first["binding_generation"], 1)
        h1 = {"Authorization": f"Bearer {self.token1}"}
        h2 = {"Authorization": f"Bearer {self.token2}"}
        self.assertEqual(len(self.client.get("/api/v2/me/devices", headers=h1).json()["devices"]), 1)
        self.assertEqual(self.client.get("/api/v2/me/devices", headers=h2).json()["devices"], [])
        self.assertEqual(self.client.patch("/api/v2/me/devices/LY-AABBCCDDEEFF", headers=h2,
                                           json={"display_name": "越权"}).status_code, 404)

        # status 回包丢失可重试：新 token 生效，上一个立即撤销。
        second = self.client.post("/api/v2/device/pair/status", json={
            "device_id": "LY-AABBCCDDEEFF", "nonce": "0123456789abcdef0123456789abcdef",
        }).json()
        self.assertNotEqual(first["device_token"], second["device_token"])
        bad = self.client.get("/api/v2/device/agenda",
                              headers={"Authorization": f"Bearer {first['device_token']}"})
        self.assertEqual(bad.status_code, 401)
        good = self.client.get("/api/v2/device/agenda",
                               headers={"Authorization": f"Bearer {second['device_token']}"})
        self.assertEqual(good.status_code, 200)

    def test_cloud_speaker_policy_ignores_stale_firmware_override(self):
        bound = self._bind()
        owner = {"Authorization": f"Bearer {self.token1}"}
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        changed = self.client.patch(
            "/api/v2/me/devices/LY-AABBCCDDEEFF", headers=owner,
            json={"speaker_diarization_enabled": False})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertFalse(changed.json()["device"]["speaker_diarization_enabled"])

        # Legacy firmware may still send a stale cached value.  The server is
        # authoritative and must snapshot its device setting for this meeting.
        created = self.client.post(
            "/api/v2/device/sessions", headers=device | {
                "Idempotency-Key": "cloud-speaker-policy:create"},
            json={"client_session_id": "cloud-speaker-policy",
                  "binding_generation": 1, "upload_mode": "live",
                  "speaker_diarization_enabled": True})
        self.assertEqual(created.status_code, 200, created.text)
        row = self.storage.db.query_one(
            "SELECT speaker_diarization_enabled FROM device_sessions"
            " WHERE server_session_id=?",
            (created.json()["server_session_id"],))
        self.assertIsNotNone(row)
        self.assertFalse(bool(row["speaker_diarization_enabled"]))

    def test_device_state_exposes_speaker_progress_and_latest_timeline(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        created = self.client.post(
            "/api/v2/device/sessions", headers=device | {
                "Idempotency-Key": "device-display-state:create"},
            json={"client_session_id": "device-display-state",
                  "binding_generation": 1, "upload_mode": "live"})
        self.assertEqual(created.status_code, 200, created.text)
        sid = created.json()["server_session_id"]
        service = self.router.device_service
        seg_id, _ = service._store_live_caption(
            sid, text="讨论交付计划", start_sample=16000, end_sample=32000)
        service._store_live_speaker(sid, seg_id, "spk_01", "说话人 1")
        self.storage.save_summary_draft(sid, {
            "summary_stage": "rolling", "timeline_schema": 1,
            "timeline_chapters": [{
                "chapter_no": 2, "start_ms": 1000, "end_ms": 2000,
                "title": "交付计划", "items": [
                    "介绍背景", "说明现状", "确认时间", "明确负责人"],
                "status": "current", "mark_count": 1,
            }],
        })
        state = self.client.get(
            f"/api/v2/device/sessions/{sid}/state?after_revision=0",
            headers=device)
        self.assertEqual(state.status_code, 200, state.text)
        body = state.json()
        self.assertEqual(body["timeline"]["chapter_no"], 2)
        self.assertEqual(body["timeline"]["items"], ["确认时间", "明确负责人"])
        self.assertTrue(body["speaker"]["enabled"])
        self.assertEqual(body["speaker"]["speaker_count"], 1)
        self.assertEqual(body["speaker"]["labeled_segments"], 1)

    def test_unbind_revokes_token_and_rebind_increments_generation(self):
        bound = self._bind()
        account = {"Authorization": f"Bearer {self.token1}"}
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        result = self.client.delete(
            "/api/v2/me/devices/LY-AABBCCDDEEFF/binding", headers=account)
        self.assertEqual(result.json()["binding_generation"], 2)
        self.assertIn(self.client.get("/api/v2/device/agenda", headers=device).status_code, (401, 403))
        rebound = self._bind("654321", "fedcba9876543210fedcba9876543210")
        self.assertEqual(rebound["binding_generation"], 3)

    def test_claim_is_rate_limited_by_account_and_ip(self):
        headers = {"Authorization": f"Bearer {self.token2}",
                   "X-Forwarded-For": "192.0.2.77"}
        for code in ("000001", "000002", "000003", "000004", "000005"):
            self.assertEqual(self.client.post("/api/v2/me/devices/claim", headers=headers,
                                              json={"pairing_code": code}).status_code, 404)
        limited = self.client.post("/api/v2/me/devices/claim", headers=headers,
                                   json={"pairing_code": "000006"})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error"]["code"], "PAIRING_CLAIM_RATE_LIMITED")

    def test_session_create_twenty_replays_chunk_recovery_and_safe_end(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        create_headers = device | {"Idempotency-Key": "session:local-1:create"}
        payload = {
            "client_session_id": "local-1", "started_at_utc": None,
            "binding_generation": 1,
            "scene": "translate", "title": "双语联调",
            "audio": {"codec": "pcm_s16le", "sample_rate": 16000,
                      "channels": 1, "bits_per_sample": 16},
        }
        responses = [self.client.post("/api/v2/device/sessions", headers=create_headers,
                                      json=payload) for _ in range(20)]
        self.assertTrue(all(r.status_code == 200 for r in responses))
        session_id = responses[0].json()["server_session_id"]
        self.assertEqual(len({r.json()["server_session_id"] for r in responses}), 1)
        count = self.storage.db.query_one("SELECT COUNT(*) n FROM device_sessions")["n"]
        self.assertEqual(count, 1)

        chunks = [b"\x01\x00" * 100, b"\x02\x00" * 80]
        def put(seq, start):
            data = chunks[seq]
            headers = device | {
                "Content-Type": "audio/L16;rate=16000;channels=1",
                "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                "X-Byte-Offset": str(start * 2), "X-Byte-Count": str(len(data)),
            }
            return self.client.put(f"/api/v2/device/sessions/{session_id}/audio/{seq}",
                                   headers=headers, content=data)

        out_of_order = put(1, 100).json()
        self.assertEqual(out_of_order["next_seq"], 0)
        self.assertEqual(out_of_order["acknowledged_bytes"], 0)
        ack = put(0, 0).json()
        self.assertEqual((ack["next_seq"], ack["acknowledged_bytes"]), (2, 360))
        self.assertTrue(put(0, 0).json()["duplicate"])

        # 客户先报三片：明确回 409 + 缺片，修复后同一幂等键可成功。
        end_headers = device | {"Idempotency-Key": "session:local-1:end"}
        end_body = {"total_chunks": 3, "total_samples": 200,
                    "ended_at_utc": None, "binding_generation": 1}
        missing = self.client.post(f"/api/v2/device/sessions/{session_id}/end",
                                   headers=end_headers, json=end_body)
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["missing_sequences"], [2])
        # 改正 manifest 为真实两片；幂等键没有因上次缺片被永久卡死。
        end_body["total_chunks"], end_body["total_samples"] = 2, 180
        ok_headers = device | {"Idempotency-Key": "session:local-1:end-fixed"}
        ended = self.client.post(f"/api/v2/device/sessions/{session_id}/end",
                                 headers=ok_headers, json=end_body)
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual(self.completed, [(session_id, 11)])
        self.assertEqual(self.storage.get_state(session_id), "finalizing")
        audio = Path(self.temp) / "audio_cache" / f"{session_id}.b.pcm"
        self.assertEqual(audio.read_bytes(), b"".join(chunks))
        chunk_rows = self.storage.db.query(
            "SELECT path FROM device_audio_chunks WHERE server_session_id=?", (session_id,))
        self.assertTrue(chunk_rows)
        self.assertTrue(all(not Path(row["path"]).exists() for row in chunk_rows))
        state = self.client.get(f"/api/v2/device/sessions/{session_id}/state",
                                headers=device).json()
        self.assertEqual(state["upload"]["acknowledged_bytes"], 360)
        self.assertEqual((state["scene"], state["title"]), ("translate", "双语联调"))
        late_mark = self.client.put(
            f"/api/v2/device/sessions/{session_id}/marks/late", headers=device,
            json={"offset_samples": 1, "kind": "mark", "label": None})
        self.assertEqual((late_mark.status_code, late_mark.json()["error"]["code"]),
                         (409, "SESSION_NOT_UPLOADING"))

    def test_idempotency_key_payload_conflict(self):
        bound = self._bind()
        headers = {"Authorization": f"Bearer {bound['device_token']}",
                   "Idempotency-Key": "same-key"}
        base = {"client_session_id": "one", "binding_generation": 1,
                "audio": {"codec": "pcm_s16le", "sample_rate": 16000,
                          "channels": 1, "bits_per_sample": 16}}
        self.assertEqual(self.client.post("/api/v2/device/sessions", headers=headers,
                                          json=base).status_code, 200)
        conflict = self.client.post("/api/v2/device/sessions", headers=headers,
                                    json=base | {"client_session_id": "two"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED")

    def test_restart_restores_token_upload_ack_and_agenda_revision(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        headers = device | {"Idempotency-Key": "session:restart-1:create"}
        payload = {"client_session_id": "restart-1", "binding_generation": 1,
                   "audio": {"codec": "pcm_s16le", "sample_rate": 16000,
                             "channels": 1, "bits_per_sample": 16}}
        created = self.client.post("/api/v2/device/sessions", headers=headers, json=payload).json()
        sid = created["server_session_id"]
        data = b"\x01\x00" * 64
        self.assertEqual(self.client.put(
            f"/api/v2/device/sessions/{sid}/audio/0", headers=device | {
                "Content-Type": "audio/L16;rate=16000;channels=1",
                "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                "X-Byte-Offset": "0", "X-Byte-Count": str(len(data))}, content=data).status_code, 200)
        AgendaStore(self.storage).create_event({
            "owner": "TEST1", "type": "meeting", "title": "重启后仍可见",
            "start": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "source": "manual"})

        self.storage.db.close()
        self.storage = Storage(Path(self.temp))
        auth.configure_auth(self.storage)
        restarted_app = FastAPI()
        restarted_app.include_router(create_device_v2_router(self.storage))
        restarted = TestClient(restarted_app)
        restarted.headers.update({
            "X-Luoye-Protocol": "luoye-device-api/2",
            "X-Luoye-Firmware": "0.6.1",
            "X-Luoye-Device": "LY-AABBCCDDEEFF",
        })
        replay = restarted.post("/api/v2/device/sessions", headers=headers, json=payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual((replay.json()["next_seq"], replay.json()["acknowledged_bytes"]),
                         (1, len(data)))
        agenda = restarted.get("/api/v2/device/agenda?after_revision=0", headers=device)
        self.assertEqual(agenda.status_code, 200)
        self.assertTrue(any(item["title"] == "重启后仍可见" for item in agenda.json()["items"]))

    def test_agenda_snapshot_revision_and_voice_todo(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        store = AgendaStore(self.storage)
        store.create_event({"owner": "TEST1", "type": "todo", "title": "交报告",
                            "start": "2026-08-02T10:00:00+08:00", "source": "manual"})
        self.storage.db.execute(
            "INSERT INTO agenda_todos(id,owner,text,due_at,done,source_event_id)"
            " VALUES(?,?,?,NULL,0,NULL)",
            ("no-time-todo", "TEST1", "交付包装材料"))
        self.router.device_service.bump_agenda_revision("TEST1")
        snapshot = self.client.get("/api/v2/device/agenda?after_revision=0&window_days=7",
                                   headers=device).json()
        self.assertGreater(snapshot["revision"], 0)
        self.assertLessEqual(len(snapshot["items"]), 24)
        self.assertTrue(all(set(item) == {"id", "title", "display_time", "start_utc",
                                         "reminder_utc", "has_time"}
                            for item in snapshot["items"]))
        no_time = next(item for item in snapshot["items"] if item["id"] == "no-time-todo")
        self.assertEqual((no_time["display_time"], no_time["start_utc"],
                          no_time["has_time"]), ("未定时间", 0, False))
        unchanged = self.client.get(
            f"/api/v2/device/agenda?after_revision={snapshot['revision']}&window_days=7",
            headers=device).json()
        self.assertEqual(unchanged["revision"], snapshot["revision"])
        self.assertEqual(unchanged["items"], [])

        wav = b"RIFF" + (36 + 320).to_bytes(4, "little") + b"WAVEfmt " \
              + (16).to_bytes(4, "little") + b"\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00" \
              + b"data" + (320).to_bytes(4, "little") + b"\0" * 320
        todo = self.client.put(
            "/api/v2/device/todos/todo-1/audio?binding_generation=1", headers=device | {
                "Content-Type": "audio/wav",
                "X-Content-SHA256": hashlib.sha256(wav).hexdigest()}, content=wav)
        self.assertEqual(todo.status_code, 200, todo.text)
        server_id = todo.json()["server_id"]
        duplicate = self.client.put(
            "/api/v2/device/todos/todo-1/audio?binding_generation=1", headers=device | {
                "Content-Type": "audio/wav",
                "X-Content-SHA256": hashlib.sha256(wav).hexdigest()}, content=wav)
        self.assertEqual(duplicate.json()["server_id"], server_id)
        result = self.client.get(f"/api/v2/device/todos/{server_id}/result",
                                 headers=device).json()
        self.assertIn(result["status"], {"needs_confirmation", "failed"})
        self.assertEqual(result["todo_id"], "todo-1")
        self.assertEqual(result["binding_generation"], 1)
        if result["status"] == "needs_confirmation":
            stale = self.client.post(f"/api/v2/device/todos/{server_id}/actions", headers=device | {
                "Idempotency-Key": "todo:todo-1:action:confirm:stale"},
                                     json={"action": "confirm", "revision": max(1, result["revision"] - 1)})
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()["error"]["code"], "TODO_REVISION_MISMATCH")
            action = self.client.post(f"/api/v2/device/todos/{server_id}/actions", headers=device | {
                "Idempotency-Key": f"todo:todo-1:action:confirm:{result['revision']}"},
                                      json={"action": "confirm", "revision": result["revision"]})
            self.assertEqual(action.status_code, 200, action.text)
            self.assertEqual(action.json()["status"], "confirmed")

    def test_live_asr_revision_incremental_state_and_firmware_size(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        class TranslationStub:
            async def translate(self, text, target_lang="zh", context=None):
                return "翻译：" + text
        self.router.device_service._translation_llm = TranslationStub()
        created = self.client.post("/api/v2/device/sessions", headers=device | {
            "Idempotency-Key": "session:live-1:create"}, json={
                "client_session_id": "live-1", "binding_generation": 1,
                "source_language": "en", "target_language": "zh-CN",
                "scene": "translate",
                "audio": {"codec": "pcm_s16le", "sample_rate": 16000,
                          "channels": 1, "bits_per_sample": 16},
            })
        self.assertEqual(created.status_code, 200, created.text)
        sid = created.json()["server_session_id"]
        offset = 0
        for seq in range(4):
            pcm = (seq + 1).to_bytes(2, "little", signed=True) * 160
            ack = self.client.put(
                f"/api/v2/device/sessions/{sid}/audio/{seq}", headers=device | {
                    "Content-Type": "audio/L16;rate=16000;channels=1",
                    "X-Content-SHA256": hashlib.sha256(pcm).hexdigest(),
                    "X-Byte-Offset": str(offset), "X-Byte-Count": str(len(pcm)),
                }, content=pcm)
            self.assertEqual(ack.status_code, 200, ack.text)
            self.assertEqual(ack.json()["received_chunks"], seq + 1)
            offset += len(pcm)
        state = self.client.get(
            f"/api/v2/device/sessions/{sid}/state?after_revision=0", headers=device)
        self.assertEqual(state.status_code, 200, state.text)
        body = state.json()
        self.assertTrue(body["changed"])
        self.assertGreater(body["revision"], 0)
        self.assertTrue(body["captions"])
        self.assertTrue(body["translations"])
        self.assertEqual(body["target_language"], "zh-CN")
        self.assertLessEqual(len(state.content), 8191)
        unchanged = self.client.get(
            f"/api/v2/device/sessions/{sid}/state?after_revision={body['revision']}",
            headers=device).json()
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["captions"], [])
        self.assertEqual(unchanged["translations"], [])

        # Fill many oversized visible results.  The API must truncate each
        # field and then bound the whole response to the ESP32 8192-byte buffer.
        service = self.router.device_service
        for index in range(24):
            seg_id, _ = service._store_live_caption(
                sid, text=(f"caption-{index}-" + "字" * 400),
                start_sample=index * 160, end_sample=(index + 1) * 160)
            service._store_live_translation(sid, seg_id, "译" * 400)
        bounded = self.client.get(
            f"/api/v2/device/sessions/{sid}/state?after_revision=0", headers=device)
        self.assertLessEqual(len(bounded.content), 8191)
        bounded_body = bounded.json()
        self.assertTrue(all(len(item["text"].encode("utf-8")) <= 255
                            for item in bounded_body["captions"]))
        self.assertTrue(all(len(item["translated_text"].encode("utf-8")) <= 383
                            for item in bounded_body["translations"]))
        end = self.client.post(f"/api/v2/device/sessions/{sid}/end", headers=device | {
            "Idempotency-Key": "session:live-1:end"}, json={
                "total_chunks": 4, "total_samples": 640, "binding_generation": 1})
        self.assertEqual(end.status_code, 200, end.text)
        processing = self.client.get(
            f"/api/v2/device/sessions/{sid}/state?after_revision={bounded_body['revision']}",
            headers=device).json()
        self.assertEqual(processing["status"], "processing")
        self.assertEqual((processing["upload"]["received_chunks"],
                          processing["upload"]["received_samples"],
                          processing["upload"]["acknowledged_bytes"]), (4, 640, 1280))
        self.storage.set_state(sid, "done")
        terminal = self.client.get(
            f"/api/v2/device/sessions/{sid}/state?after_revision={processing['revision']}",
            headers=device).json()
        self.assertEqual(terminal["status"], "done")
        self.assertTrue(terminal["changed"])
        self.assertEqual((terminal["upload"]["received_chunks"],
                          terminal["upload"]["received_samples"],
                          terminal["upload"]["acknowledged_bytes"]), (4, 640, 1280))

    def test_ack_requires_offset_continuity_and_missing_list_is_bounded(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}

        def create(local_id):
            response = self.client.post("/api/v2/device/sessions", headers=device | {
                "Idempotency-Key": f"session:{local_id}:create"}, json={
                    "client_session_id": local_id, "binding_generation": 1,
                    "audio": {"codec": "pcm_s16le", "sample_rate": 16000,
                              "channels": 1, "bits_per_sample": 16}})
            return response.json()["server_session_id"]

        sid = create("offset-gap")
        pcm = b"\x01\x00" * 8
        future = self.client.put(f"/api/v2/device/sessions/{sid}/audio/1",
                                 headers=device | {
                                     "Content-Type": "audio/L16;rate=16000;channels=1",
                                     "X-Content-SHA256": hashlib.sha256(pcm).hexdigest(),
                                     "X-Byte-Offset": "32", "X-Byte-Count": str(len(pcm))},
                                 content=pcm).json()
        self.assertEqual((future["next_seq"], future["received_chunks"],
                          future["total_received_chunks"]), (0, 0, 1))
        first = self.client.put(f"/api/v2/device/sessions/{sid}/audio/0",
                                headers=device | {
                                    "Content-Type": "audio/L16;rate=16000;channels=1",
                                    "X-Content-SHA256": hashlib.sha256(pcm).hexdigest(),
                                    "X-Byte-Offset": "0", "X-Byte-Count": str(len(pcm))},
                                content=pcm).json()
        # seq=1 starts at sample 16, leaving samples 8..15 absent.
        self.assertEqual((first["next_seq"], first["received_chunks"],
                          first["total_received_chunks"]), (1, 1, 2))

        silent = create("silent-mark")
        mark = self.client.put(
            f"/api/v2/device/sessions/{silent}/marks/mark-1", headers=device,
            json={"offset_samples": 0, "kind": "mark", "label": None})
        self.assertEqual(mark.status_code, 200, mark.text)
        self.assertGreater(mark.json()["revision"], 0)
        duplicate_mark = self.client.put(
            f"/api/v2/device/sessions/{silent}/marks/mark-1", headers=device,
            json={"offset_samples": 0, "kind": "mark", "label": None})
        self.assertEqual(duplicate_mark.json()["revision"], mark.json()["revision"])
        silent_state = self.client.get(
            f"/api/v2/device/sessions/{silent}/state?after_revision=0", headers=device).json()
        self.assertTrue(silent_state["changed"])
        self.assertEqual(silent_state["captions"], [])

        empty = create("missing-many")
        error = self.client.post(
            f"/api/v2/device/sessions/{empty}/end", headers=device | {
                "Idempotency-Key": "session:missing-many:end"}, json={
                    "total_chunks": 2_000_000, "total_samples": 0,
                    "binding_generation": 1})
        self.assertEqual(error.status_code, 409, error.text)
        payload = error.json()
        self.assertEqual(payload["missing_count"], 2_000_000)
        self.assertEqual(len(payload["missing_sequences"]), 64)
        self.assertTrue(payload["missing_truncated"])
        self.assertLessEqual(len(error.content), 8191)

    def test_end_enqueue_failure_is_terminal_and_keeps_progress(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        created = self.client.post("/api/v2/device/sessions", headers=device | {
            "Idempotency-Key": "session:enqueue-fail:create"}, json={
                "client_session_id": "enqueue-fail", "binding_generation": 1,
                "audio": {"codec": "pcm_s16le", "sample_rate": 16000,
                          "channels": 1, "bits_per_sample": 16}}).json()
        sid = created["server_session_id"]
        pcm = b"\x01\x00" * 32
        uploaded = self.client.put(
            f"/api/v2/device/sessions/{sid}/audio/0", headers=device | {
                "Content-Type": "audio/L16;rate=16000;channels=1",
                "X-Content-SHA256": hashlib.sha256(pcm).hexdigest(),
                "X-Byte-Offset": "0", "X-Byte-Count": str(len(pcm))}, content=pcm)
        self.assertEqual(uploaded.status_code, 200, uploaded.text)

        async def fail_enqueue(_session_id, _end_ms):
            raise RuntimeError("queue unavailable")
        self.router.device_service.on_session_complete = fail_enqueue
        ended = self.client.post(f"/api/v2/device/sessions/{sid}/end", headers=device | {
            "Idempotency-Key": "session:enqueue-fail:end"}, json={
                "total_chunks": 1, "total_samples": 32, "binding_generation": 1})
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual(ended.json()["status"], "failed")
        state = self.client.get(
            f"/api/v2/device/sessions/{sid}/state?after_revision=0", headers=device).json()
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["changed"])
        self.assertEqual(state["error"]["code"], "PROCESSING_ENQUEUE_FAILED")
        self.assertEqual((state["upload"]["received_chunks"],
                          state["upload"]["received_samples"],
                          state["upload"]["acknowledged_bytes"]), (1, 32, 64))

    def test_pairing_expiry_supersede_empty_agenda_and_completed_todo(self):
        first = self._pair_body("111111", "11111111111111111111111111111111")
        self.assertEqual(self.client.post("/api/v2/device/pair/start", json=first).status_code, 200)
        self.storage.db.execute(
            "UPDATE device_pairings SET expires_at=? WHERE nonce_digest=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
             self.router.device_service.digest_nonce(first["device_id"], first["nonce"])))
        expired = self.client.post("/api/v2/device/pair/start", json=first)
        self.assertEqual((expired.status_code, expired.json()["error"]["code"]),
                         (410, "PAIRING_EXPIRED"))
        replacement = self._pair_body("111111", "22222222222222222222222222222222")
        self.assertEqual(self.client.post("/api/v2/device/pair/start", json=replacement).status_code, 200)
        newer = self._pair_body("222222", "33333333333333333333333333333333")
        self.assertEqual(self.client.post("/api/v2/device/pair/start", json=newer).status_code, 200)
        superseded = self.client.post("/api/v2/device/pair/start", json=replacement)
        self.assertEqual((superseded.status_code, superseded.json()["error"]["code"]),
                         (409, "PAIRING_NOT_ACTIVE"))

        claimed = self.client.post("/api/v2/me/devices/claim",
                                   headers={"Authorization": f"Bearer {self.token1}"},
                                   json={"pairing_code": "222222"})
        self.assertEqual(claimed.status_code, 200, claimed.text)
        status = self.client.post("/api/v2/device/pair/status", json={
            "device_id": newer["device_id"], "nonce": newer["nonce"]}).json()
        device = {"Authorization": f"Bearer {status['device_token']}"}
        empty_agenda = self.client.get("/api/v2/device/agenda?after_revision=0",
                                       headers=device).json()
        self.assertEqual(empty_agenda["revision"], 1)
        self.assertEqual(empty_agenda["items"], [])

        event = AgendaStore(self.storage).create_event({
            "owner": "TEST1", "type": "todo", "title": "finish me",
            "start": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "source": "manual"})
        before = self.client.get("/api/v2/device/agenda?after_revision=0", headers=device).json()
        self.assertTrue(any(item["id"] == event["id"] for item in before["items"]))
        done = self.client.patch(f"/api/v1/agenda/todos/{event['todo']['id']}",
                                 headers={"Authorization": f"Bearer {self.token1}"},
                                 json={"done": True})
        self.assertEqual(done.status_code, 200, done.text)
        after = self.client.get(
            f"/api/v2/device/agenda?after_revision={before['revision']}", headers=device).json()
        self.assertGreater(after["revision"], before["revision"])
        self.assertFalse(any(item["id"] == event["id"] for item in after["items"]))

    def test_agenda_is_flat_sanitized_capped_and_under_firmware_buffer(self):
        bound = self._bind()
        device = {"Authorization": f"Bearer {bound['device_token']}"}
        store = AgendaStore(self.storage)
        start = datetime.now(timezone.utc) + timedelta(minutes=10)
        for index in range(30):
            store.create_event({"owner": "TEST1", "type": "meeting",
                                "title": f"\x01会议{index}" + chr(0x1F600) + "长" * 100,
                                "start": (start + timedelta(minutes=index)).isoformat(),
                                "source": "manual"})
        response = self.client.get("/api/v2/device/agenda?after_revision=0&window_days=7",
                                   headers=device)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertLessEqual(len(body["items"]), 24)
        self.assertLessEqual(len(response.content), 8191)
        self.assertTrue(all(item["reminder_utc"] <= item["start_utc"] for item in body["items"]))
        self.assertTrue(all("\x01" not in item["title"]
                            and all(ord(ch) <= 0xFFFF for ch in item["title"])
                            and len(item["title"].encode("utf-8")) <= 71 for item in body["items"]))


if __name__ == "__main__":
    unittest.main()
