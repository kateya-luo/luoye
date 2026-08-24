import asyncio
import json
import logging
import os
from contextlib import suppress

import websockets

logger = logging.getLogger("ai_recorder.funasr")


class FunASRClient:
    def __init__(self):
        self.url = os.getenv("FUNASR_WS_URL", "ws://funasr:10095")
        self.mock = os.getenv("ASR_MODE", "mock").lower() == "mock"
        self.sequence = 0
        self.websocket = None
        self.receiver_task = None
        self.results: asyncio.Queue[dict] = asyncio.Queue()
        self.online_parts: list[str] = []
        self.language = "auto"

    async def start(self, session_id: str, language: str = "auto"):
        self.language = language
        if self.mock:
            logger.info("funasr_mock_started session_id=%s language=%s", session_id, language)
            return
        logger.info("funasr_connecting session_id=%s url=%s language=%s", session_id, self.url, language)
        self.websocket = await websockets.connect(
            self.url,
            open_timeout=20,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        )
        await self.websocket.send(json.dumps({
            "mode": "2pass",
            "chunk_size": [5, 10, 5],
            "chunk_interval": 10,
            "encoder_chunk_look_back": 4,
            "decoder_chunk_look_back": 0,
            "audio_fs": 16000,
            "wav_name": session_id,
            "wav_format": "pcm",
            "is_speaking": True,
            "hotwords": "",
            "itn": True,
            "language": language,
        }, ensure_ascii=False))
        self.receiver_task = asyncio.create_task(self._receive_loop())
        logger.info("funasr_connected session_id=%s language=%s", session_id, language)

    async def send_audio(self, chunk: bytes) -> list[dict]:
        self.sequence += 1
        if self.mock:
            if self.sequence % 4 == 0:
                examples = {
                    "en": f"This is real-time transcript segment {self.sequence // 4}.",
                    "mixed": f"This is 第 {self.sequence // 4} 段 bilingual transcript.",
                }
                return [{
                    "text": examples.get(
                        self.language,
                        f"这是第 {self.sequence // 4} 段实时转写示例。",
                    ),
                    "mode": "mock",
                    "is_final": True,
                    "language": self.language,
                }]
            return []
        await self.websocket.send(chunk)
        await asyncio.sleep(0)
        return self._drain_results()

    async def collect_results(self, timeout: float = 0.0) -> list[dict]:
        """Collect results for device workers without changing WebSocket clients.

        Device HTTP uploads are acknowledged before this wait.  A short wait
        here lets the background consumer publish a 2-pass online hypothesis
        as soon as FunASR produces it instead of deferring it to the next PCM
        chunk.
        """
        collected = self._drain_results()
        if self.mock or collected or timeout <= 0:
            return collected
        try:
            collected.append(await asyncio.wait_for(self.results.get(), timeout=timeout))
        except asyncio.TimeoutError:
            pass
        return collected

    async def finish(self) -> list[dict]:
        if self.mock:
            return []
        await self.websocket.send(json.dumps({"is_speaking": False}))
        collected = self._drain_results()
        saw_final = any(item.get("is_final") for item in collected)
        # A final 2pass result can take several seconds on a small CPU instance.
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            try:
                item = await asyncio.wait_for(self.results.get(), timeout=0.75)
                collected.append(item)
                saw_final = saw_final or bool(item.get("is_final"))
            except asyncio.TimeoutError:
                if saw_final:
                    break
        if not saw_final and self.online_parts:
            collected.append({
                "text": "".join(self.online_parts),
                "mode": "online-fallback",
                "is_final": True,
            })
        await self.close()
        return collected

    async def close(self):
        if self.websocket is not None:
            with suppress(Exception):
                await self.websocket.close()
            self.websocket = None
        if self.receiver_task is not None:
            self.receiver_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.receiver_task
            self.receiver_task = None

    async def _receive_loop(self):
        try:
            async for raw in self.websocket:
                message = json.loads(raw)
                text = message.get("text", "").strip()
                if not text:
                    continue
                mode = message.get("mode", "online")
                is_final = mode in {"offline", "2pass-offline"} or bool(message.get("is_final"))
                logger.info("asr_msg mode=%s final=%s chars=%d bytes=%d",
                            mode, is_final, len(text), len(text.encode("utf-8")))
                if is_final:
                    self.online_parts.clear()
                    partial_text = ""
                else:
                    # This FunASR runtime emits 2pass-online as consecutive
                    # fragments, not as a complete mutable hypothesis.  Keep
                    # the raw fragment in `text` for existing browser clients,
                    # and expose the accumulated utterance separately for the
                    # recorder-card snapshot/polling protocol.
                    self.online_parts.append(text)
                    partial_text = "".join(self.online_parts)
                await self.results.put({
                    "text": text,
                    "partial_text": partial_text,
                    "mode": mode,
                    "is_final": is_final,
                    "language": message.get("language") or self.language,
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("funasr_receive_error error=%s", exc)

    def _drain_results(self) -> list[dict]:
        items = []
        while not self.results.empty():
            items.append(self.results.get_nowait())
        return items
