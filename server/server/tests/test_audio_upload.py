import tempfile
import unittest
from pathlib import Path

from app.audio_upload_api import CoverageTracker, write_chunk_at


class AudioUploadTest(unittest.TestCase):
    def test_out_of_order_sparse_write_fills_gap(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.b.pcm"
            write_chunk_at(p, 0, b"AAAA")
            write_chunk_at(p, 8, b"BBBB")    # 留出 4..8 的洞（断网段）
            self.assertEqual(p.read_bytes(), b"AAAA\x00\x00\x00\x00BBBB")
            write_chunk_at(p, 4, b"CCCC")    # 重传补洞
            self.assertEqual(p.read_bytes(), b"AAAACCCCBBBB")

    def test_coverage_detects_disconnect_gap(self):
        c = CoverageTracker()
        c.add("s", 0, 15000)
        c.add("s", 25000, 26000)           # 断网 15s-25s
        self.assertEqual(c.gaps("s", 26000), [(15000, 25000)])
        c.add("s", 15000, 25000)           # 补传到齐
        self.assertEqual(c.gaps("s", 26000), [])


if __name__ == "__main__":
    unittest.main()
