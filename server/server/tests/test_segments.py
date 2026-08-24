import unittest

from app.segments import Segment, Timeline


class TimelineTest(unittest.TestCase):
    def test_live_segments_stay_ordered_by_time(self):
        tl = Timeline()
        tl.upsert_live(Segment(start_ms=2000, end_ms=3000, text="第三句"))
        tl.upsert_live(Segment(start_ms=0, end_ms=1000, text="第一句"))
        tl.upsert_live(Segment(start_ms=1000, end_ms=2000, text="第二句"))
        self.assertEqual([s.text for s in tl.ordered()], ["第一句", "第二句", "第三句"])

    def test_gap_then_offline_fills_in_place(self):
        tl = Timeline()
        tl.upsert_live(Segment(start_ms=0, end_ms=15000, text="断网前", seg_id="a"))
        tl.upsert_live(Segment(start_ms=25000, end_ms=26000, text="重连后", seg_id="b"))
        gap = tl.mark_gap(15000, 25000)
        # 占位在中间
        self.assertEqual([s.text for s in tl.ordered()], ["断网前", "", "重连后"])

        patch = tl.apply_offline(15000, 25000, [
            Segment(start_ms=16000, end_ms=20000, text="断网中A"),
            Segment(start_ms=20000, end_ms=24000, text="断网中B"),
        ])
        # 占位被移除
        self.assertIn(gap.added[0].seg_id, patch.removed)
        # 原位插入、整体有序，断网前后不动
        self.assertEqual([s.text for s in tl.ordered()], ["断网前", "断网中A", "断网中B", "重连后"])
        # 离线段标记为 final/offline
        self.assertTrue(all(s.state == "final" and s.source == "offline" for s in patch.added))

    def test_finalize_replaces_whole_range(self):
        tl = Timeline()
        tl.upsert_live(Segment(start_ms=0, end_ms=1000, text="临时1"))
        tl.upsert_live(Segment(start_ms=1000, end_ms=2000, text="临时2"))
        patch = tl.apply_offline(0, 3000, [Segment(start_ms=0, end_ms=2000, text="整场重转结果")])
        self.assertEqual(len(patch.removed), 2)
        self.assertEqual([s.text for s in tl.ordered()], ["整场重转结果"])

    def test_fill_gaps_only_inserts_non_overlapping(self):
        tl = Timeline()
        # 实时段：0-15s（断网前，带说话人）、25-26s（重连后）
        tl.upsert_live(Segment(start_ms=0, end_ms=15000, text="断网前", speaker_id="spk_01", seg_id="a"))
        tl.upsert_live(Segment(start_ms=25000, end_ms=26000, text="重连后", speaker_id="spk_02", seg_id="b"))
        # 离线整场转写结果：含断网前(重叠)、断网中(无重叠)、重连后(重叠)
        patch = tl.fill_gaps([
            Segment(start_ms=1000, end_ms=14000, text="断网前-离线版"),   # 与a重叠 → 跳过
            Segment(start_ms=16000, end_ms=20000, text="断网中A"),        # 空洞 → 插入
            Segment(start_ms=20000, end_ms=24000, text="断网中B"),        # 空洞 → 插入
            Segment(start_ms=25200, end_ms=25800, text="重连后-离线版"),  # 与b重叠 → 跳过
        ])
        # 只插入了 2 段断网内容
        self.assertEqual([s.text for s in patch.added], ["断网中A", "断网中B"])
        # 整体有序，实时段（含说话人）原样保留
        self.assertEqual([s.text for s in tl.ordered()], ["断网前", "断网中A", "断网中B", "重连后"])
        self.assertEqual(tl.ordered()[0].speaker_id, "spk_01")  # 实时说话人没丢

    def test_relabel_speakers_global(self):
        tl = Timeline()
        tl.upsert_live(Segment(start_ms=0, end_ms=1000, text="x", speaker_id="spk_01"))
        tl.upsert_live(Segment(start_ms=1000, end_ms=2000, text="y", speaker_id="spk_02"))
        tl.relabel_speakers({"spk_01": "G1", "spk_02": "G1"})
        self.assertTrue(all(s.speaker_id == "G1" and s.speaker_final for s in tl.ordered()))


if __name__ == "__main__":
    unittest.main()
