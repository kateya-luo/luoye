import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.storage import Storage


def seg(seg_id, start, end, text, label=None, source="live"):
    return {"seg_id": seg_id, "start_ms": start, "end_ms": end, "text": text,
            "speaker_label": label, "source": source, "state": "provisional"}


class StorageSqliteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = Storage(self.root)
        self.addCleanup(self.storage.db.close)

    def test_roundtrip_shapes_match_json_era(self):
        self.storage.create_meeting("m1")
        self.storage.upsert_segment("m1", seg("a", 0, 1000, "第一句", "说话人 1"))
        self.storage.upsert_segment("m1", seg("b", 1000, 2000, "第二句"))
        m = self.storage.get_meeting("m1")
        self.assertEqual(m["transcript"], ["[说话人 1] 第一句", "第二句"])
        self.assertEqual([s["seg_id"] for s in m["segments"]], ["a", "b"])
        self.assertFalse(m["summary_pending"])         # 未点击生成，不是后台生成中
        self.assertEqual(m["minutes_status"], "not_created")
        self.storage.save_summary("m1", {"summary": "总结", "decisions": [], "action_items": [],
                                         "mindmap": {"title": "主题X", "branches": [{"title": "t", "items": ["i"]}]}})
        m = self.storage.get_meeting("m1")
        self.assertFalse(m["summary_pending"])
        self.assertEqual(m["summary"]["summary"], "总结")
        lst = self.storage.list_meetings()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["segment_count"], 2)
        self.assertEqual(lst[0]["title"], "主题X")     # 无用户标题时回落 mindmap 标题
        self.assertIn("第一句", lst[0]["transcript_preview"])

    def test_old_database_adds_owner_and_assigns_history_to_test1(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        conn = sqlite3.connect(root / "clearmeeting.db")
        conn.execute("CREATE TABLE meetings(session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, state TEXT NOT NULL)")
        conn.execute("INSERT INTO meetings VALUES('legacy-meeting','2026-07-01T00:00:00+00:00','done')")
        conn.commit()
        conn.close()
        migrated = Storage(root)
        self.addCleanup(migrated.db.close)
        self.assertEqual(migrated.meeting_owner("legacy-meeting"), "TEST1")

    def test_translation_survives_replace_segments(self):
        """双语记录核心保护：译文落库后，meeting_end 的整表重写（timeline 不带 translation）
        必须按 seg_id 把旧译文带回，绝不清空。"""
        self.storage.create_meeting("m1")
        self.storage.upsert_segment("m1", seg("a", 0, 1000, "Hello everyone."))
        self.storage.upsert_segment("m1", seg("b", 1000, 2000, "Please review chapter five."))
        self.storage.set_segment_translation("m1", "a", "大家好。")
        self.storage.set_segment_translation("m1", "b", "请复习第五章。")
        # 定稿整表重写：模拟 timeline.to_list()——不携带 translation 字段
        self.storage.replace_segments("m1", [seg("a", 0, 1000, "Hello everyone."),
                                             seg("b", 1000, 2000, "Please review chapter five."),
                                             seg("c", 2000, 3000, "New tail sentence.")])
        segs = {s["seg_id"]: s for s in self.storage.load_segments("m1")}
        self.assertEqual(segs["a"]["translation"], "大家好。")     # 旧译文带回
        self.assertEqual(segs["b"]["translation"], "请复习第五章。")
        self.assertIsNone(segs["c"]["translation"])               # 新句无译文，正常
        # get_meeting 的 segments 也应携带 translation（历史页双语的数据来源）
        m = self.storage.get_meeting("m1")
        self.assertEqual({s["seg_id"]: s.get("translation") for s in m["segments"]}["a"], "大家好。")

    def test_canonical_publish_is_atomic_and_records_pipeline_run(self):
        self.storage.create_meeting("canonical-meeting")
        self.storage.upsert_segment(
            "canonical-meeting", seg("old-live", 0, 500, "旧的实时字幕"))
        # This storage-level test does not need the pairing/device fixtures.
        self.storage.db.execute("PRAGMA foreign_keys=OFF")
        self.storage.db.execute(
            "INSERT INTO device_sessions(server_session_id,client_session_id,device_id,"
            "owner_user_id,binding_generation,started_at_utc,codec,sample_rate,channels,"
            "bits_per_sample,status,revision,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("canonical-meeting", "client-1", "device-1", "TEST1", 1,
             "2026-08-27T00:00:00+00:00", "pcm_s16le", 16000, 1, 16,
             "processing", 4, "2026-08-27T00:00:00+00:00",
             "2026-08-27T00:00:00+00:00"))
        self.storage.db.execute("PRAGMA foreign_keys=ON")
        one = seg("canon-1", 0, 900, "第一句", "说话人 1", "offline_canonical")
        one["speaker_id"] = "spk_01"
        two = seg("canon-2", 900, 1800, "第二句", "说话人 2", "offline_canonical")
        two["speaker_id"] = "spk_02"
        run = {
            "canonical_sha256": "a" * 64,
            "pipeline_version": "offline-canonical-diarization-v2.0.0",
            "speaker_count": 2,
            "processing_ms": 1234,
            "realtime_factor": 0.12,
            "speakers": [
                {"speaker_id": "spk_01", "label": "说话人 1", "segment_count": 1},
                {"speaker_id": "spk_02", "label": "说话人 2", "segment_count": 1},
            ],
        }

        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.replace_canonical_segments(
                "canonical-meeting", [one, dict(one)], run)
        self.assertEqual(
            [item["seg_id"] for item in self.storage.load_segments("canonical-meeting")],
            ["old-live"],
        )

        revision = self.storage.replace_canonical_segments(
            "canonical-meeting", [one, two], run)
        self.assertEqual(revision, 5)
        self.assertEqual(
            [item["seg_id"] for item in self.storage.load_segments("canonical-meeting")],
            ["canon-1", "canon-2"],
        )
        saved_run = self.storage.db.query_one(
            "SELECT * FROM canonical_diarization_runs WHERE session_id=?",
            ("canonical-meeting",),
        )
        self.assertEqual(saved_run["state"], "done")
        self.assertEqual(saved_run["speaker_count"], 2)
        self.assertEqual(
            len(self.storage.get_meta_info("canonical-meeting")["speakers"]), 2)

    def test_background_processing_status_tracks_upload_queue_diarization_and_publish(self):
        sid = "processing-meeting"
        now = "2026-08-28T01:00:00+00:00"
        self.storage.create_meeting(sid)
        self.storage.db.execute("PRAGMA foreign_keys=OFF")
        self.storage.db.execute(
            "INSERT INTO device_sessions(server_session_id,client_session_id,device_id,"
            "owner_user_id,binding_generation,started_at_utc,codec,sample_rate,channels,"
            "bits_per_sample,status,canonical_total_bytes,expected_samples,revision,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "client-progress", "device-progress", "TEST1", 1, now,
             "pcm_s16le", 16000, 1, 16, "uploading", 320000, 160000, 0, now, now))
        self.storage.db.execute(
            "INSERT INTO device_audio_ranges(server_session_id,start_byte,end_byte,sha256,byte_count,created_at)"
            " VALUES(?,?,?,?,?,?)", (sid, 0, 160000, "a" * 64, 160000, now))
        self.storage.db.execute("PRAGMA foreign_keys=ON")

        uploading = self.storage.get_processing_status(sid)
        self.assertTrue(uploading["active"])
        self.assertEqual(uploading["stage"], "uploading")
        self.assertEqual(uploading["upload"]["percent"], 50)
        self.assertEqual(uploading["progress_percent"], 50)
        self.assertEqual(uploading["upload"]["missing_bytes"], 160000)
        self.assertEqual(uploading["upload"]["missing_duration_ms"], 5000)
        self.assertIn("仍缺约 5 秒", uploading["detail"])

        # Overlapping retry records are counted as union coverage, never twice.
        self.storage.db.execute(
            "INSERT INTO device_audio_ranges(server_session_id,start_byte,end_byte,sha256,byte_count,created_at)"
            " VALUES(?,?,?,?,?,?)", (sid, 80000, 160000, "b" * 64, 80000, now))
        retried = self.storage.get_processing_status(sid)
        self.assertEqual(retried["upload"]["received_bytes"], 160000)
        self.assertEqual(retried["progress_percent"], 50)

        self.storage.db.execute(
            "UPDATE device_sessions SET status='processing' WHERE server_session_id=?", (sid,))
        self.storage.set_state(sid, "finalizing")
        jobs = [
            (sid, 0, 5000, "bulk", 0, "done"),
            (sid, 5000, 10000, "bulk", 1, "running"),
            (sid, 0, 0, "canonical", 2147483646, "queued"),
            (sid, 0, 0, "publish", 2147483647, "queued"),
        ]
        for index, (session_id, start_ms, end_ms, reason, chunk_index, state) in enumerate(jobs):
            self.storage.db.execute(
                "INSERT INTO offline_asr_jobs(session_id,start_ms,end_ms,reason,chunk_index,"
                "order_key,state,attempts,available_at,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,0,?,?,?)",
                (session_id, start_ms, end_ms, reason, chunk_index, now, state,
                 now, now, now))

        transcribing = self.storage.get_processing_status(sid)
        self.assertEqual(transcribing["stage"], "transcribing")
        self.assertEqual(transcribing["jobs"]["done"], 1)
        self.assertGreater(transcribing["eta_seconds"], 0)

        self.storage.db.execute(
            "UPDATE offline_asr_jobs SET state='done' WHERE session_id=? AND reason='bulk'", (sid,))
        self.storage.db.execute(
            "UPDATE offline_asr_jobs SET state='running' WHERE session_id=? AND reason='canonical'", (sid,))
        diarizing = self.storage.get_processing_status(sid)
        self.assertEqual(diarizing["stage"], "diarizing")
        self.assertIn("多人识别", diarizing["title"])

        self.storage.db.execute(
            "UPDATE offline_asr_jobs SET state='done' WHERE session_id=? AND reason='canonical'", (sid,))
        self.storage.db.execute(
            "UPDATE offline_asr_jobs SET state='running' WHERE session_id=? AND reason='publish'", (sid,))
        self.assertEqual(self.storage.get_processing_status(sid)["stage"], "publishing")

        self.storage.db.execute(
            "UPDATE offline_asr_jobs SET state='done' WHERE session_id=? AND reason='publish'", (sid,))
        self.storage.db.execute(
            "UPDATE device_sessions SET status='done' WHERE server_session_id=?", (sid,))
        self.storage.set_state(sid, "transcript_ready")
        complete = self.storage.get_processing_status(sid)
        self.assertFalse(complete["active"])
        self.assertEqual(complete["stage"], "completed")
        self.assertEqual(complete["progress_percent"], 100)

    def test_background_processing_failure_does_not_expose_internal_error(self):
        sid = "failed-processing"
        self.storage.create_meeting(sid)
        self.storage.set_state(sid, "finalizing")
        now = "2026-08-28T01:00:00+00:00"
        self.storage.db.execute(
            "INSERT INTO offline_asr_jobs(session_id,start_ms,end_ms,reason,chunk_index,"
            "order_key,state,attempts,available_at,last_error,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,'failed',31,?,?,?,?)",
            (sid, 0, 300000, "bulk", 0, now, now,
             "ConnectionError: http://internal-service:10097/secret", now, now))
        failed = self.storage.get_processing_status(sid)
        self.assertEqual(failed["stage"], "failed")
        self.assertNotIn("internal-service", failed["detail"])

    def test_finalizing_without_queue_is_reported_as_stalled_not_processing_forever(self):
        sid = "stalled-processing"
        self.storage.create_meeting(sid)
        self.storage.db.execute(
            "UPDATE meetings SET state='finalizing',updated_at=? WHERE session_id=?",
            ("2026-08-20T00:00:00+00:00", sid))
        stalled = self.storage.get_processing_status(sid)
        self.assertFalse(stalled["active"])
        self.assertEqual(stalled["stage"], "stalled")
        self.assertEqual(stalled["error_code"], "BACKGROUND_QUEUE_STALLED")

    def test_apply_patch_is_incremental(self):
        self.storage.create_meeting("m1")
        self.storage.upsert_segment("m1", seg("live1", 0, 10000, "断网前", "说话人 1"))
        # 补洞 patch：只加洞内段，不动 live1
        self.storage.apply_patch("m1", {"patches": [seg("off1", 15000, 20000, "断网中", source="offline")],
                                        "removed": []})
        m = self.storage.get_meeting("m1")
        self.assertEqual([s["seg_id"] for s in m["segments"]], ["live1", "off1"])
        # 替换：删掉占位、换成新段
        self.storage.apply_patch("m1", {"patches": [seg("off2", 15000, 18000, "更正后")], "removed": ["off1"]})
        m = self.storage.get_meeting("m1")
        self.assertEqual([s["seg_id"] for s in m["segments"]], ["live1", "off2"])

    def test_title_update_and_delete(self):
        self.storage.create_meeting("m1")
        self.storage.set_title("m1", "我的会议")
        self.assertEqual(self.storage.get_meeting("m1")["title"], "我的会议")
        with self.assertRaises(FileNotFoundError):
            self.storage.set_title("nope", "x")
        self.assertTrue(self.storage.delete_meeting("m1"))
        self.assertIsNone(self.storage.get_meeting("m1"))

    def test_delete_meeting_cancels_device_and_background_jobs(self):
        sid = "delete-processing"
        now = "2026-08-28T01:00:00+00:00"
        self.storage.create_meeting(sid)
        self.storage.db.execute("PRAGMA foreign_keys=OFF")
        self.storage.db.execute(
            "INSERT INTO device_sessions(server_session_id,client_session_id,device_id,"
            "owner_user_id,binding_generation,started_at_utc,codec,sample_rate,channels,"
            "bits_per_sample,status,revision,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "client-delete", "device-delete", "TEST1", 1, now,
             "pcm_s16le", 16000, 1, 16, "processing", 0, now, now))
        self.storage.db.execute("PRAGMA foreign_keys=ON")
        self.storage.db.execute(
            "INSERT INTO offline_asr_jobs(session_id,start_ms,end_ms,reason,chunk_index,"
            "order_key,state,attempts,available_at,lease_owner,lease_until,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,'running',0,?,'worker','2099-01-01T00:00:00+00:00',?,?)",
            (sid, 0, 0, "canonical", 0, now, now, now, now))

        self.assertTrue(self.storage.delete_meeting(sid))
        job = self.storage.db.query_one(
            "SELECT state,lease_owner FROM offline_asr_jobs WHERE session_id=?", (sid,))
        device = self.storage.db.query_one(
            "SELECT status FROM device_sessions WHERE server_session_id=?", (sid,))
        self.assertEqual(job["state"], "cancelled")
        self.assertIsNone(job["lease_owner"])
        self.assertEqual(device["status"], "cancelled")

    def test_gaps_and_coverage_survive_reopen(self):
        self.storage.create_meeting("m1")
        self.storage.add_gap("m1", 15000, 25000)
        self.storage.save_coverage("m1", [(0, 30000)])
        self.storage.db.close()
        reopened = Storage(self.root)                  # 模拟 server 重启
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.list_gaps("m1"), [("m1", 15000, 25000)])
        self.assertEqual(reopened.load_all_coverage()["m1"], [(0, 30000)])
        reopened.delete_gap("m1", 15000)
        self.assertEqual(reopened.list_gaps("m1"), [])

    def test_unfinished_meetings_scan(self):
        self.storage.create_meeting("m1")              # recording，无纪要
        self.storage.create_meeting("m2")
        self.storage.save_summary("m2", {"summary": "done"})   # state → done
        time.sleep(0.02)
        rows = self.storage.unfinished_meetings(older_than_seconds=0.01)
        self.assertEqual([r["session_id"] for r in rows], ["m1"])

    def test_audio_prefers_bpcm_and_cleanup(self):
        audio = self.root / "audio_cache"
        audio.mkdir()
        self.storage.create_meeting("m1")
        # 只有 .pcm（旧会议）→ has_audio 仍为真
        (audio / "m1.pcm").write_bytes(b"\x00" * 100)
        self.assertTrue(self.storage.get_meeting("m1")["has_audio"])
        # .b.pcm 比 .pcm 短（通道B不完整）→ 清理不动 .pcm（兜底保留）
        (audio / "m1.b.pcm").write_bytes(b"\x00" * 50)
        self.storage.cleanup_live_audio("m1")
        self.assertTrue((audio / "m1.pcm").exists())
        # .b.pcm 完整（≥ .pcm）→ 清理删掉 .pcm，has_audio 仍为真（走 .b.pcm）
        (audio / "m1.b.pcm").write_bytes(b"\x00" * 100)
        self.storage.cleanup_live_audio("m1")
        self.assertFalse((audio / "m1.pcm").exists())
        self.assertTrue(self.storage.get_meeting("m1")["has_audio"])

    def test_json_migration(self):
        # 独立目录模拟真实首启：先有 JSON、后建库（setUp 的库已置迁移标记，不复用）
        root = self.root / "mig"
        (root / "transcripts").mkdir(parents=True)
        (root / "summaries").mkdir(parents=True)
        (root / "transcripts" / "old1.json").write_text(json.dumps({
            "session_id": "old1", "created_at": "2026-06-01T00:00:00+00:00", "title": "旧会议",
            "transcript": ["[说话人 1] 老内容"],
            "segments": [{"seg_id": "s1", "start_ms": 0, "end_ms": 900, "text": "老内容",
                          "speaker_label": "说话人 1"}],
        }, ensure_ascii=False), encoding="utf-8")
        (root / "summaries" / "old1.json").write_text(json.dumps({
            "summary": "旧纪要", "decisions": [], "action_items": [],
            "mindmap": {"title": "旧", "branches": []}}, ensure_ascii=False), encoding="utf-8")
        migrated = Storage(root)                       # __init__ 触发迁移
        self.addCleanup(migrated.db.close)
        m = migrated.get_meeting("old1")
        self.assertEqual(m["title"], "旧会议")
        self.assertEqual(m["transcript"], ["[说话人 1] 老内容"])
        self.assertEqual(m["summary"]["summary"], "旧纪要")
        self.assertFalse(m["summary_pending"])
        # 原 JSON 保留作为备份
        self.assertTrue((root / "transcripts" / "old1.json").exists())


if __name__ == "__main__":
    unittest.main()
