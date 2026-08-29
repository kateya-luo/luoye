"""Internal HTTP service for preview ASR and canonical meeting finalization."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from offline_canonical_finalizer import (
    PIPELINE_VERSION,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    build_model,
    run_finalizer,
)


LOG = logging.getLogger("clearmeeting.offline_canonical_service")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
AUDIO_ROOT = Path(os.getenv("OFFLINE_CANONICAL_AUDIO_ROOT", "/app/data/audio_cache"))
PORT = int(os.getenv("OFFLINE_CANONICAL_PORT", "10096"))
MAX_PREVIEW_BYTES = int(os.getenv("OFFLINE_CANONICAL_MAX_PREVIEW_BYTES", str(20 * 1024 * 1024)))
MODEL = build_model()
MODEL_LOCK = threading.Lock()


def _preview_segments(raw_result: dict, base_offset_ms: int) -> list[dict]:
    output = []
    for index, item in enumerate(raw_result.get("sentence_info") or []):
        text = str(item.get("sentence") or item.get("text") or "").strip()
        if not text:
            continue
        start_ms = base_offset_ms + max(0, int(round(float(item.get("start") or 0))))
        end_ms = base_offset_ms + max(
            int(round(float(item.get("start") or 0))),
            int(round(float(item.get("end") or item.get("start") or 0))),
        )
        if end_ms <= start_ms:
            continue
        output.append({
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
            "source": "offline",
            "state": "final",
            "preview_index": index,
        })
    return output


def transcribe_preview(raw_pcm: bytes, base_offset_ms: int) -> dict:
    if not raw_pcm or len(raw_pcm) % SAMPLE_WIDTH:
        raise ValueError("preview PCM is empty or misaligned")
    audio = np.frombuffer(raw_pcm, dtype="<i2").astype(np.float32)
    audio *= 1.0 / 32768.0
    with MODEL_LOCK:
        results = MODEL.generate(
            input=audio,
            input_len=len(audio),
            batch_size_s=int(os.getenv("OFFLINE_CANONICAL_BATCH_SIZE_S", "300")),
            merge_vad=False,
            sentence_timestamp=True,
            return_spk_res=False,
            use_itn=True,
        )
    if not results:
        return {"segments": []}
    return {"segments": _preview_segments(results[0], base_offset_ms)}


class Handler(BaseHTTPRequestHandler):
    server_version = "ClearMeetingOfflineCanonical/2"

    def do_GET(self):
        if urlparse(self.path).path != "/health":
            self.send_error(404)
            return
        self.send_json(200, {
            "status": "ok",
            "pipeline_version": PIPELINE_VERSION,
            "busy": MODEL_LOCK.locked(),
        })

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/transcribe":
                self.handle_transcribe()
            elif path == "/finalize":
                self.handle_finalize()
            else:
                self.send_error(404)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)[:240]})
        except FileNotFoundError:
            self.send_json(404, {"error": "canonical audio not found"})
        except Exception as exc:
            LOG.exception("offline_canonical_request_failed path=%s error=%s", path, exc)
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"[:500]})

    def handle_transcribe(self):
        length = self.content_length(MAX_PREVIEW_BYTES)
        raw_pcm = self.rfile.read(length)
        if len(raw_pcm) != length:
            raise ValueError("incomplete preview request body")
        base_offset_ms = max(0, int(self.headers.get("X-Base-Offset-Ms", "0")))
        payload = transcribe_preview(raw_pcm, base_offset_ms)
        self.send_json(200, payload)

    def handle_finalize(self):
        length = self.content_length(64 * 1024)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        session_id = str(body.get("session_id") or "")
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session id")
        audio_path = AUDIO_ROOT / f"{session_id}.b.pcm"
        with MODEL_LOCK:
            payload = run_finalizer(MODEL, session_id, audio_path)
        expected_sha256 = str(body.get("canonical_sha256") or "").lower()
        if expected_sha256 and payload["canonical_sha256"] != expected_sha256:
            raise ValueError("canonical SHA-256 mismatch")
        self.send_json(200, payload)

    def content_length(self, maximum: int) -> int:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > maximum:
            raise ValueError("invalid Content-Length")
        return length

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        LOG.info("request " + fmt, *args)


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOG.info("offline_canonical_service_ready port=%d pipeline=%s", PORT, PIPELINE_VERSION)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
