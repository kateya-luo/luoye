from pathlib import Path

class AudioBuffer:
    def __init__(self, root: Path, session_id: str, truncate: bool = False):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{session_id}.pcm"
        # truncate=True 开启全新会话（覆盖旧 .pcm）；默认追加，用于重连续写。
        self._file = self.path.open("wb" if truncate else "ab")
    def append(self, chunk: bytes): self._file.write(chunk); self._file.flush()
    def close(self):
        if not self._file.closed: self._file.close()
