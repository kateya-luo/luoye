from dataclasses import dataclass
from enum import Enum
from typing import Any

SUPPORTED_LANGUAGES = {"auto", "zh", "en", "mixed"}
SUPPORTED_SUMMARY_LANGUAGES = {"auto", "zh", "en"}
_LANGUAGE_ALIASES = {
    "chinese": "zh", "zh-cn": "zh", "中文": "zh",
    "english": "en", "en-us": "en", "英文": "en",
    "zh-en": "mixed", "bilingual": "mixed", "中英混合": "mixed",
}


def normalize_language(value: Any, *, summary: bool = False) -> str:
    value = str(value or "auto").strip().lower()
    value = _LANGUAGE_ALIASES.get(value, value)
    allowed = SUPPORTED_SUMMARY_LANGUAGES if summary else SUPPORTED_LANGUAGES
    return value if value in allowed else "auto"

class MessageType(str, Enum):
    ASR_RESULT = "asr_result"
    TRANSLATION = "translation"           # 实时翻译：某条终句字幕的译文（按 seg_id 挂到原句下）
    MEETING_END = "meeting_end"
    MEETING_UPDATE = "meeting_update"
    MEETING_RESULT = "meeting_result"
    ERROR = "error"
    # 双通道 + 离线补洞（设计文档 §11）
    SESSION_RESUMED = "session_resumed"   # 重连后补发 Timeline 快照
    GAP_MARKER = "gap_marker"             # 断网区间占位（state=filling）
    SEGMENTS_PATCH = "segments_patch"     # 离线补洞/定稿后的原位插入/替换

@dataclass(slots=True)
class AudioFrame:
    session_id: str
    sequence: int
    payload: bytes

def event(kind: MessageType | str, **payload: Any) -> dict[str, Any]:
    """Build a wire event from either a protocol enum or an extension name.

    Observer-only messages such as ``observer_catchup`` are intentionally not
    part of the device protocol enum.  Accepting their string names here keeps
    the common event builder usable without raising before the first WebSocket
    payload is sent.
    """
    event_type = kind.value if isinstance(kind, MessageType) else str(kind)
    return {"type": event_type, **payload}
