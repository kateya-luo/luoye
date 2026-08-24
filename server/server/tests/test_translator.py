import asyncio
import unittest

from app.translator import SessionTranslator, dominant_lang


class FakeLLM:
    """第一句故意慢——用来证明串行队列保序（v1 的 create_task 并发会乱序）。"""
    def __init__(self):
        self.calls = []

    async def translate(self, text, target_lang="zh", context=None):
        self.calls.append({"text": text, "context": list(context or [])})
        if len(self.calls) == 1:
            await asyncio.sleep(0.05)   # 第一句最慢
        return f"T({text})"


class TestDominantLang(unittest.TestCase):
    def test_chinese(self):
        self.assertEqual(dominant_lang("我们先把采集和上传跑通"), "zh")

    def test_english(self):
        self.assertEqual(dominant_lang("Please review chapter five"), "en")

    def test_mixed_returns_none(self):
        self.assertIsNone(dominant_lang("这个 feature 明天 release 上线预计没问题"))


class TestSessionTranslator(unittest.IsolatedAsyncioTestCase):
    async def test_order_preserved_and_context_grows(self):
        llm, results = FakeLLM(), []
        async def sink(r): results.append(r)
        tr = SessionTranslator("zh", llm, sink, context_pairs=3)
        await tr.enqueue("s1", "First sentence about the exam.")
        await tr.enqueue("s2", "Second sentence about homework.")
        await tr.enqueue("s3", "Third sentence about Friday.")
        await tr.close()
        self.assertEqual([r["seg_id"] for r in results], ["s1", "s2", "s3"])   # 保序
        self.assertEqual(results[0]["text"], "T(First sentence about the exam.)")
        # 上下文滚动：第2句带1对、第3句带2对
        self.assertEqual(len(llm.calls[0]["context"]), 0)
        self.assertEqual(len(llm.calls[1]["context"]), 1)
        self.assertEqual(len(llm.calls[2]["context"]), 2)
        self.assertEqual(llm.calls[1]["context"][0][1], "T(First sentence about the exam.)")

    async def test_same_language_skipped_without_api_call(self):
        llm, results = FakeLLM(), []
        async def sink(r): results.append(r)
        tr = SessionTranslator("zh", llm, sink)
        await tr.enqueue("s1", "这句话本来就是中文的所以不用翻译")
        await tr.enqueue("s2", "But this English one should be translated.")
        await tr.close()
        self.assertEqual(len(llm.calls), 1)                    # 中文句没调 API
        self.assertTrue(results[0]["skipped"])
        self.assertEqual(results[0]["src"], "这句话本来就是中文的所以不用翻译")
        self.assertFalse(results[1]["skipped"])
        self.assertEqual(results[1]["text"], "T(But this English one should be translated.)")

    async def test_enqueue_after_close_is_noop(self):
        """关闭后迟到的句子（如会后补洞）静默跳过：不堆死队列、不报错、不产生结果。"""
        llm, results = FakeLLM(), []
        async def sink(r): results.append(r)
        tr = SessionTranslator("zh", llm, sink)
        await tr.enqueue("s1", "Before close.")
        await tr.close()
        await tr.enqueue("s2", "After close — should be ignored.")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["seg_id"], "s1")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(tr._queue.qsize(), 0)   # 关键：关闭后没有东西堆进死队列


if __name__ == "__main__":
    unittest.main()
