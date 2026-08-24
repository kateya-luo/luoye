"""实时翻译管道 v2（ROADMAP §5.1）：每会话一个实例，通道A 与设备路径共用。

修正 v1 的四个草率点：
1. **保序**：单会话串行队列（并发=1），译文到达顺序=说话顺序，不再乱序、不再无界并发。
2. **上下文**：滚动携带最近 N 对（原文,译文）进 prompt，保术语/代词一致（听译基本功）。
3. **同语言跳过**：字符集启发式判断句子主语言，==目标语言则不调 API（中英混合会议的刚需）。
4. **延迟可测**：逐句记录 translate_latency_ms 日志，拿数据不拿体感。

落库由 on_result 回调方持有 storage 决定（translation 挂回 segment）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress
from typing import Awaitable, Callable

logger = logging.getLogger("ai_recorder.translator")

_CJK = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[A-Za-z]")


def dominant_lang(text: str) -> str | None:
    """粗判句子主语言：zh / en / None(不确定)。启发式够用——只用于"同语言跳过"。"""
    cjk = len(_CJK.findall(text))
    latin = len(_LATIN.findall(text))
    total = cjk + latin
    if total < 2:
        return None
    if cjk / total >= 0.5:
        return "zh"
    if latin / total >= 0.8:
        return "en"
    return None   # 混合句：交给翻译（混合正是要翻的）


class SessionTranslator:
    """一条会话的串行翻译工作流。enqueue() 快速入队不阻塞字幕链；worker 顺序处理。"""

    def __init__(self, target: str, llm, on_result: Callable[[dict], Awaitable[None]],
                 context_pairs: int = 3, max_queue: int = 200) -> None:
        self.target = target
        self.llm = llm
        self.on_result = on_result            # async ({seg_id,text,lang,src,skipped}) -> None
        self.context: list[tuple[str, str]] = []
        self.context_pairs = context_pairs
        self._queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=max_queue)
        self._closed = False
        self._worker = asyncio.create_task(self._run())

    async def enqueue(self, seg_id: str, text: str) -> None:
        if self._closed:   # 已关闭（会话结束后迟到的补洞句）：静默跳过，不往死队列里堆
            return
        text = (text or "").strip()
        if not text:
            return
        try:
            self._queue.put_nowait((seg_id, text))
        except asyncio.QueueFull:   # 极端积压：丢最旧保最新（实时性优先，日志留痕）
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            logger.warning("translator_queue_overflow target=%s dropped_oldest", self.target)
            with suppress(asyncio.QueueFull):
                self._queue.put_nowait((seg_id, text))

    async def close(self) -> None:
        """排空剩余任务后退出（会议结束时调用；不打断已入队的句子）。此后 enqueue 为 no-op。"""
        self._closed = True
        await self._queue.put(None)
        with suppress(Exception):
            await asyncio.wait_for(self._worker, timeout=30)

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            seg_id, text = item
            try:
                await self._translate_one(seg_id, text)
            except Exception:
                logger.exception("translator_item_failed seg_id=%s", seg_id)

    async def _translate_one(self, seg_id: str, text: str) -> None:
        # 同语言跳过：句子主语言==目标语言 → 不调 API（对照模式下该句无需译文）
        if dominant_lang(text) == self.target:
            await self.on_result({"seg_id": seg_id, "text": "", "lang": self.target,
                                  "src": text, "skipped": True})
            return
        t0 = time.monotonic()
        out = await self.llm.translate(text, self.target, context=list(self.context))
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info("translate_latency_ms=%d target=%s chars=%d", latency_ms, self.target, len(text))
        if not out:
            return
        self.context.append((text, out))
        if len(self.context) > self.context_pairs:
            self.context.pop(0)
        await self.on_result({"seg_id": seg_id, "text": out, "lang": self.target,
                              "src": text, "skipped": False})
