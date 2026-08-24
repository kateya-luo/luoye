import tempfile
import unittest
from pathlib import Path

from app.storage import Storage
from app.timeline_chapters import (build_timeline_chapters, enrich_summary_timeline,
                                   transcript_with_time_and_marks)


class TimelineChaptersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name))
        self.storage.create_meeting("timeline-test", owner_user_id="TEST1")
        for index, text in enumerate(("进度完成", "进入测试", "讨论交付", "周五完成")):
            self.storage.upsert_segment("timeline-test", {
                "seg_id": f"seg-{index}", "start_ms": index * 10_000,
                "end_ms": index * 10_000 + 8_000, "text": text,
            })

    def tearDown(self):
        self.storage.db.close()
        self.temp.cleanup()

    def test_real_boundaries_and_mark_assignment(self):
        result = {"timeline_chapters": [
            {"start_ms": 0, "end_ms": 18_000, "title": "项目进度", "items": ["进入测试"],
             "boundary": {"kind": "initial", "confidence": 1.0, "reason": "开场"}},
            {"start_ms": 20_000, "end_ms": 38_000, "title": "交付计划", "items": ["周五完成"],
             "boundary": {"kind": "topic_change", "confidence": 0.9,
                          "reason": "由进度转入交付"}},
        ]}
        marks = [{"id": "m1", "at_ms": 5_000, "kind": "mark", "label": None},
                 {"id": "m2", "at_ms": 25_000, "kind": "mark", "label": "重点"}]
        chapters = build_timeline_chapters(result, self.storage.load_segments("timeline-test"),
                                           marks, rolling=True)
        self.assertEqual([c["status"] for c in chapters], ["frozen", "current"])
        self.assertEqual([c["mark_count"] for c in chapters], [1, 1])
        self.assertEqual(chapters[1]["marks"][0]["id"], "m2")

    def test_enrichment_reads_device_marks(self):
        self.storage.db.execute(
            "INSERT INTO users(id,username,password_hash,role,active,token_version,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)", ("TEST1", "TEST1", "test", "member", 1, 1,
                                          "2026-08-13T00:00:00+00:00",
                                          "2026-08-13T00:00:00+00:00"))
        self.storage.db.execute(
            "INSERT INTO devices(device_id,owner_user_id,display_name,binding_generation,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)", ("LY-TIMELINE", "TEST1", "测试设备", 1,
                                   "2026-08-13T00:00:00+00:00", "2026-08-13T00:00:00+00:00"))
        self.storage.db.execute(
            "INSERT INTO device_sessions(server_session_id,client_session_id,device_id,owner_user_id,"
            "binding_generation,started_at_utc,codec,sample_rate,channels,bits_per_sample,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("timeline-test", "client-timeline", "LY-TIMELINE", "TEST1", 1,
             "2026-08-13T00:00:00+00:00", "pcm_s16le", 16000, 1, 16, "uploading",
             "2026-08-13T00:00:00+00:00",
             "2026-08-13T00:00:00+00:00"))
        self.storage.db.execute(
            "INSERT INTO device_session_marks(server_session_id,client_mark_id,offset_samples,kind,label,created_at)"
            " VALUES(?,?,?,?,?,?)", ("timeline-test", "mark-1", 16_000 * 22, "mark", None,
                                      "2026-08-13T00:00:00+00:00"))
        result = {"timeline_chapters": [
            {"start_ms": 0, "end_ms": 18_000, "title": "项目进度", "items": ["进入测试"],
             "boundary": {"kind": "initial", "confidence": 1.0, "reason": "开场"}},
            {"start_ms": 20_000, "end_ms": 38_000, "title": "交付计划", "items": ["周五完成"],
             "boundary": {"kind": "topic_change", "confidence": 0.9,
                          "reason": "由进度转入交付"}},
        ]}
        enriched = enrich_summary_timeline(self.storage, "timeline-test", result, rolling=False)
        self.assertEqual(enriched["timeline_schema"], 3)
        self.assertEqual(enriched["timeline_chapters"][1]["mark_count"], 1)

    def test_frozen_chapter_stays_stable(self):
        previous = [{"chapter_no": 1, "start_ms": 0, "end_ms": 19_999,
                     "title": "旧标题保持", "items": ["旧内容"], "status": "frozen"}]
        changed = {"timeline_chapters": [
            {"start_ms": 0, "end_ms": 19_000, "title": "模型重写标题", "items": ["重写"]},
            {"start_ms": 20_000, "end_ms": 38_000, "title": "新章节", "items": ["新内容"],
             "boundary": {"kind": "topic_change", "confidence": 0.9,
                          "reason": "明确换题"}},
        ]}
        chapters = build_timeline_chapters(changed, self.storage.load_segments("timeline-test"),
                                           [], rolling=True, previous=previous)
        self.assertEqual(chapters[0]["title"], "旧标题保持")
        self.assertEqual(chapters[-1]["status"], "current")

    def test_mark_is_injected_into_nearest_transcript_line(self):
        transcript = transcript_with_time_and_marks(
            self.storage.load_segments("timeline-test"),
            [{"id": "m", "at_ms": 21_000, "kind": "mark"}])
        lines = transcript.splitlines()
        self.assertIn("[anchor=S0001", lines[0])
        self.assertIn("[anchor=S0003", lines[2])
        self.assertNotIn("[MARK:", lines[1])
        self.assertIn("[MARK: user-designated key point]", lines[2])

    def test_low_confidence_tail_updates_current_without_new_timestamp(self):
        previous = [{"chapter_no": 1, "start_ms": 0, "end_ms": 18_000,
                     "title": "项目进度", "items": ["完成开发"], "status": "current",
                     "boundary": {"kind": "initial", "confidence": 1.0, "reason": ""}}]
        result = {"timeline_chapters": [
            {"start_ms": 0, "end_ms": 18_000, "title": "项目进度", "items": ["完成开发"],
             "boundary": {"kind": "initial", "confidence": 1.0, "reason": "开场"}},
            {"start_ms": 20_000, "end_ms": 38_000, "title": "仍在讨论进度",
             "items": ["补充测试细节"],
             "boundary": {"kind": "topic_change", "confidence": 0.45,
                          "reason": "只是补充细节"}},
        ]}
        chapters = build_timeline_chapters(
            result, self.storage.load_segments("timeline-test"), [], rolling=True,
            previous=previous)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0]["start_ms"], 0)
        self.assertIn("补充测试细节", chapters[0]["items"])

    def test_strong_semantic_boundary_after_ten_seconds_is_accepted(self):
        previous = [{"chapter_no": 1, "start_ms": 0, "end_ms": 18_000,
                     "title": "项目进度", "items": ["完成开发"], "status": "current"}]
        result = {"timeline_chapters": [
            {"start_ms": 0, "end_ms": 18_000, "title": "项目进度", "items": ["进入测试"],
             "boundary": {"kind": "initial", "confidence": 1.0, "reason": "开场"}},
            {"start_ms": 20_000, "end_ms": 38_000, "title": "交付决策",
             "items": ["周五完成"],
             "boundary": {"kind": "decision_phase", "confidence": 0.93,
                          "reason": "从进度汇报转入交付决策"}},
        ]}
        chapters = build_timeline_chapters(
            result, self.storage.load_segments("timeline-test"), [], rolling=True,
            previous=previous)
        self.assertEqual(len(chapters), 2)
        self.assertEqual([item["status"] for item in chapters], ["frozen", "current"])
        self.assertEqual(chapters[1]["boundary"]["kind"], "decision_phase")

    def test_rapid_boundary_is_debounced_unless_backed_by_real_mark(self):
        result = {"timeline_chapters": [
            {"start_ms": 0, "end_ms": 4_000, "title": "开场", "items": ["说明目标"],
             "boundary": {"kind": "initial", "confidence": 1.0, "reason": "开场"}},
            {"start_ms": 5_000, "end_ms": 18_000, "title": "短暂插话", "items": ["补充"],
             "boundary": {"kind": "topic_change", "confidence": 0.99, "reason": "插话"}},
        ]}
        chapters = build_timeline_chapters(
            result, self.storage.load_segments("timeline-test"), [], rolling=True)
        self.assertEqual(len(chapters), 1)

        result["timeline_chapters"][1]["boundary"] = {
            "kind": "mark", "confidence": 0.8, "reason": "用户标记新重点"}
        result["timeline_chapters"][1]["anchor"] = "S0002"
        chapters = build_timeline_chapters(
            result, self.storage.load_segments("timeline-test"),
            [{"id": "mark-fast", "at_ms": 10_000, "kind": "mark"}], rolling=True)
        self.assertEqual(len(chapters), 2)

    def test_timeline_has_no_fixed_five_chapter_limit(self):
        segments = []
        candidates = []
        for index in range(7):
            start = index * 12_000
            segments.append({"start_ms": start, "end_ms": start + 10_000,
                             "text": f"主题{index}"})
            candidates.append({"start_ms": start, "end_ms": start + 10_000,
                               "title": f"主题{index}", "items": [f"内容{index}"],
                               "boundary": {"kind": "initial" if index == 0 else "topic_change",
                                            "confidence": 1.0, "reason": "明确换题"}})
        chapters = build_timeline_chapters(
            {"timeline_chapters": candidates}, segments, [], rolling=True)
        self.assertEqual(len(chapters), 7)

    def test_missing_timeline_fallback_never_interpolates_fake_chapters(self):
        result = {"mindmap": {"title": "会议重点", "branches": [
            {"title": "进度", "items": ["开发完成"]},
            {"title": "交付", "items": ["周五完成"]},
        ]}}
        chapters = build_timeline_chapters(
            result, self.storage.load_segments("timeline-test"), [], rolling=True)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0]["title"], "会议重点")

    def test_timeline_duration_never_changes_anchor_mapping(self):
        segments = [
            {"seg_id": "s0", "start_ms": 0, "end_ms": 15_000, "text": "开场介绍数列基础"},
            {"seg_id": "s1", "start_ms": 2_400_000, "end_ms": 2_420_000, "text": "转入等差数列定义"},
            {"seg_id": "s2", "start_ms": 5_700_000, "end_ms": 5_730_000, "text": "最后讨论考试应用"},
            {"seg_id": "s3", "start_ms": 7_180_000, "end_ms": 7_200_000, "text": "课程结束"},
        ]
        # Deliberately reproduce the production failure: the model returns
        # every numeric timestamp inside the first two minutes.  Exact
        # transcript anchors must win regardless of the two-hour duration.
        result = {"timeline_chapters": [
            {"anchor": "S0001", "start_ms": 0, "end_ms": 30_000,
             "title": "开场", "items": ["数列基础"],
             "boundary": {"kind": "initial", "confidence": 1.0}},
            {"anchor": "S0002", "start_ms": 30_000, "end_ms": 60_000,
             "title": "定义", "items": ["等差数列定义"],
             "boundary": {"kind": "topic_change", "confidence": 0.9}},
            {"anchor": "S0003", "start_ms": 90_000, "end_ms": 120_000,
             "title": "应用", "items": ["考试应用"],
             "boundary": {"kind": "phase_change", "confidence": 0.92}},
        ]}
        chapters = build_timeline_chapters(result, segments, [], rolling=False)
        self.assertEqual([chapter["start_ms"] for chapter in chapters],
                         [0, 2_400_000, 5_700_000])
        self.assertEqual(chapters[-1]["end_ms"], 7_200_000)
        self.assertTrue(all(chapter["timestamp_source"] == "anchor" for chapter in chapters))


if __name__ == "__main__":
    unittest.main()
