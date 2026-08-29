"""FunASR 离线（批量）转写客户端（设计文档 §6）。

把一段 PCM 交给 FunASR 做"非实时整段转写"，输出带绝对会议时间偏移的分段，
用于断网补洞与会议结束 finalize 重转。

设计要点（经严谨推敲，避开两个真坑）：
1. **收发并发**：边发音频边收结果。绝不能"全发完再读"——服务器在接收期间就会陆续
   吐 VAD 分句结果，若不并发读取，服务器出站缓冲填满→TCP 背压→我们的 send() 永久阻塞→死锁。
2. **限速但远快于实时地喂**：断网"丢字"的真因是瞬时灌爆服务器输入缓冲导致音频被丢弃；
   只要分块 + 小间隔（默认每 1s 音频隔 40ms，约 25x 实时）就不丢，且 2h 音频几分钟喂完。
3. **复用现有 funasr 服务**（默认 ws://funasr:10095, mode=2pass）：生产已验证每天在出
   `2pass-offline` 带 timestamp 的高质量结果。这里**只采集带 offline 标记/timestamp 的结果**，
   丢弃 online 流式噪声。要切专用离线容器：设 FUNASR_OFFLINE_WS_URL + OFFLINE_ASR_MODE=offline。

协议细节（timestamp 格式、is_final 语义）以真服务器为准——LOG_LEVEL=DEBUG 会打印每条
原始返回（offline_raw），或调试端点 POST /api/v1/sessions/{id}/offline-transcribe 校准。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import suppress

import websockets
import httpx

from .segments import Segment

logger = logging.getLogger("ai_recorder.funasr_offline")

BYTES_PER_MS = 16000 * 2 / 1000
_SENT_RE = re.compile(r"(?<=[。！？!?])")  # 句末标点后切句，保留标点


def parse_timestamp(ts) -> tuple[int, int] | None:
    """FunASR timestamp：[[start_ms,end_ms],...]（可能是 list，也可能是 JSON 字符串）。
    返回整段 [首token起, 末token止]；解析不出返回 None。"""
    if not ts:
        return None
    if isinstance(ts, str):
        try:
            ts = json.loads(ts)
        except (ValueError, TypeError):
            return None
    try:
        pairs = [p for p in ts if isinstance(p, (list, tuple)) and len(p) >= 2]
        if not pairs:
            return None
        return int(pairs[0][0]), int(pairs[-1][1])
    except (TypeError, ValueError):
        return None


class FunASROfflineClient:
    def __init__(self) -> None:
        self.url = os.getenv("FUNASR_OFFLINE_WS_URL", "ws://funasr:10095")
        self.mock = os.getenv("ASR_MODE", "mock").lower() == "mock"
        self.mode = os.getenv("OFFLINE_ASR_MODE", "2pass")          # 复用现有2pass服务；专用离线容器可设 "offline"
        self.feed_chunk_ms = int(os.getenv("OFFLINE_FEED_CHUNK_MS", "1000"))
        self.feed_pace_ms = int(os.getenv("OFFLINE_FEED_PACE_MS", "40"))   # 每块间隔，避免灌爆服务器输入缓冲
        self.idle_timeout = float(os.getenv("OFFLINE_IDLE_TIMEOUT", "30")) # 喂完后等结果/结果间静默多久算结束
        self.http_url = os.getenv("FUNASR_OFFLINE_HTTP_URL", "").strip().rstrip("/")
        self.http_preview_timeout = float(os.getenv("OFFLINE_HTTP_PREVIEW_TIMEOUT", "900"))
        self.http_finalize_timeout = float(os.getenv("OFFLINE_HTTP_FINALIZE_TIMEOUT", "7200"))

    async def transcribe(self, pcm: bytes, base_offset_ms: int) -> list[Segment]:
        """转写一段 16k/16bit/mono PCM，返回带绝对会议时间偏移的分段（speaker 暂为局部/None）。"""
        if self.mock:
            dur = int(len(pcm) / BYTES_PER_MS)
            return [Segment(start_ms=base_offset_ms, end_ms=base_offset_ms + dur,
                            text="（离线转写占位）", source="offline", state="final")]
        if not pcm:
            return []
        if self.http_url:
            return await self._transcribe_http(pcm, base_offset_ms)

        total_ms = int(len(pcm) / BYTES_PER_MS)
        texts: list[str] = []   # 累积离线服务返回的整段文字

        async with websockets.connect(self.url, open_timeout=20, max_size=16 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "mode": self.mode,
                "chunk_size": [5, 10, 5],
                "chunk_interval": 10,
                "wav_name": "offline",
                "wav_format": "pcm",
                "audio_fs": 16000,
                "is_speaking": True,
                "itn": True,
                "hotwords": "",
            }, ensure_ascii=False))

            sender = asyncio.create_task(self._feed(ws, pcm))
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout)
                    except asyncio.TimeoutError:
                        # 专用离线服务收完 is_speaking:False 才一次性返回；喂音频期间没结果是正常的，
                        # 只有"喂完了还静默这么久"才算真没结果。
                        if sender.done():
                            break
                        continue
                    except websockets.ConnectionClosed:
                        break

                    logger.debug("offline_raw %s", str(raw)[:300])
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    # 只累积整段文字；时间戳只用于"洞在哪"，洞内文字按句切即可（见下）
                    t = (msg.get("text") or "").strip()
                    mode = msg.get("mode", "")
                    if t and ((self.mode == "offline") or ("offline" in mode) or msg.get("timestamp")):
                        texts.append(t)
                    if msg.get("is_final"):
                        break
            finally:
                if not sender.done():
                    sender.cancel()
                with suppress(asyncio.CancelledError, websockets.ConnectionClosed, Exception):
                    await sender

        segments = self._text_to_segments("".join(texts), base_offset_ms, total_ms)
        logger.info("offline_transcribe_done base=%d dur_ms=%d segments=%d", base_offset_ms, total_ms, len(segments))
        return segments

    async def _transcribe_http(self, pcm: bytes, base_offset_ms: int) -> list[Segment]:
        timeout = httpx.Timeout(
            connect=30, read=self.http_preview_timeout,
            write=self.http_preview_timeout, pool=30)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                self.http_url + "/transcribe",
                content=pcm,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Base-Offset-Ms": str(max(0, int(base_offset_ms))),
                },
            )
            response.raise_for_status()
            payload = response.json()
        segments = []
        for item in payload.get("segments") or []:
            text = str(item.get("text") or "").strip()
            start_ms = max(0, int(item.get("start_ms") or 0))
            end_ms = max(start_ms, int(item.get("end_ms") or start_ms))
            if text and end_ms > start_ms:
                segments.append(Segment(
                    start_ms=start_ms, end_ms=end_ms, text=text,
                    source="offline", state="final"))
        logger.info("offline_http_transcribe_done base=%d segments=%d",
                    base_offset_ms, len(segments))
        return segments

    async def finalize(self, session_id: str, canonical_sha256: str | None = None) -> dict:
        if not self.http_url:
            raise RuntimeError("FUNASR_OFFLINE_HTTP_URL is required for canonical finalization")
        timeout = httpx.Timeout(
            connect=30, read=self.http_finalize_timeout,
            write=60, pool=30)
        body = {"session_id": session_id}
        if canonical_sha256:
            body["canonical_sha256"] = canonical_sha256
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self.http_url + "/finalize", json=body)
            response.raise_for_status()
            payload = response.json()
        if not payload.get("segments"):
            raise RuntimeError("canonical finalizer returned no segments")
        logger.info(
            "offline_canonical_received session_id=%s segments=%d speakers=%d rtf=%s",
            session_id, len(payload["segments"]), int(payload.get("speaker_count") or 0),
            payload.get("realtime_factor"))
        return payload

    def _text_to_segments(self, text: str, base_offset_ms: int, total_ms: int) -> list[Segment]:
        """把整段离线文字按句末标点切句，按字数比例均匀铺到 [0,total_ms] 上。
        时间戳是粗略的——仅用于让 fill_gaps 把落在断网洞里的句子塞进去，不追求精确。"""
        text = (text or "").strip()
        if not text:
            return []
        sents = [s.strip() for s in _SENT_RE.split(text) if s.strip()] or [text]
        total_chars = sum(len(s) for s in sents) or 1
        out: list[Segment] = []
        cursor = 0
        for s in sents:
            dur = round(total_ms * len(s) / total_chars)
            start, end = cursor, min(total_ms, cursor + max(1, dur))
            out.append(Segment(start_ms=base_offset_ms + start, end_ms=base_offset_ms + end,
                               text=s, source="offline", state="final"))
            cursor = end
        return out

    async def _feed(self, ws, pcm: bytes) -> None:
        """并发地把整段音频限速喂给服务器，最后发结束标记。"""
        step = int(16000 * 2 * self.feed_chunk_ms / 1000)
        pace = self.feed_pace_ms / 1000
        try:
            for i in range(0, len(pcm), step):
                await ws.send(pcm[i:i + step])
                if pace:
                    await asyncio.sleep(pace)
            await ws.send(json.dumps({"is_speaking": False}))
        except websockets.ConnectionClosed:
            pass
