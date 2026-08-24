import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from funasr import AutoModel

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("speaker_service")
model_lock = threading.Lock()
model_name = os.getenv("SPEAKER_MODEL", "iic/speech_campplus_sv_zh-cn_16k-common")
max_segment_seconds = max(2.0, float(os.getenv("SPEAKER_SERVICE_MAX_SEGMENT_SECONDS", "12")))
max_pcm_bytes = int(max_segment_seconds * 16000 * 2)
logger.info("loading_speaker_model model=%s", model_name)
model = AutoModel(model=model_name, device="cpu", disable_update=True)


def find_embedding(value):
    if isinstance(value, dict):
        for key in ("spk_embedding", "embedding"):
            if key in value:
                return find_embedding(value[key])
        for child in value.values():
            found = find_embedding(child)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, (int, float)) for item in value):
            return [float(item) for item in value]
        for child in value:
            found = find_embedding(child)
            if found is not None:
                return found
    if hasattr(value, "detach"):
        return value.detach().cpu().reshape(-1).tolist()
    if isinstance(value, np.ndarray):
        return value.reshape(-1).tolist()
    return None


def generate_embedding(audio):
    with model_lock:
        result = model.generate(input=audio, fs=16000, is_final=True)
    embedding = find_embedding(result)
    if not embedding:
        raise RuntimeError("CAM++ returned no embedding")
    return embedding


# Loading the Python process is not enough: an incompatible FunASR/CAM++ pair
# can load successfully and then return no embedding for every meeting.  Run
# one bounded inference before the container advertises readiness.
_t = np.arange(16000, dtype=np.float32) / 16000.0
_selftest_audio = (0.08 * np.sin(2 * np.pi * 180 * _t)
                   + 0.04 * np.sin(2 * np.pi * 360 * _t)).astype(np.float32)
_selftest_embedding = generate_embedding(_selftest_audio)
logger.info("speaker_model_ready model=%s embedding_dims=%d",
            model_name, len(_selftest_embedding))


class Handler(BaseHTTPRequestHandler):
    server_version = "ClearMeetingSpeaker/0.11"

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_json(200, {"status": "ok", "model": model_name})

    def do_POST(self):
        if self.path != "/embed":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > max_pcm_bytes:
                self.send_json(413, {"error": f"PCM segment exceeds {max_segment_seconds:g}s limit"})
                return
            raw = self.rfile.read(length)
            if len(raw) < 3200 or len(raw) % 2:
                raise ValueError("PCM segment is too short or misaligned")
            audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            embedding = generate_embedding(audio)
            self.send_json(200, {"embedding": embedding})
        except Exception as exc:
            logger.exception("embedding_failed error=%s", exc)
            self.send_json(500, {"error": str(exc)})

    def log_message(self, fmt, *args):
        logger.info("request " + fmt, *args)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.getenv("SPEAKER_PORT", "10100"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
