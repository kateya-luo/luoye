"""Generate the protocol V1 golden vectors used by firmware and Windows tests."""

from __future__ import annotations

import json
import struct
from pathlib import Path


def crc16(data: bytes) -> int:
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ 0x1021) & 0xFFFF if value & 0x8000 else (value << 1) & 0xFFFF
    return value


def with_crc(body: bytes) -> bytes:
    return body + struct.pack("<H", crc16(body))


def audio_vector() -> bytes:
    codec_payload = struct.pack("<hBB", 0x1234, 10, 0) + bytes(range(160))
    header = struct.pack(
        "<BBBBQIIHH",
        1,
        0x10,
        0x08,
        0x01,
        0x0102030405060708,
        42,
        840,
        320,
        len(codec_payload),
    )
    return with_crc(header + codec_payload)


def command_vector() -> bytes:
    payload = struct.pack(
        "<QQBIB",
        0x0102030405060708,
        1_782_000_000_000,
        1,
        16_000,
        1,
    )
    return with_crc(struct.pack("<BBBBHH", 1, 0x10, 0, 0, 0x1234, len(payload)) + payload)


def caption_vector() -> bytes:
    text = "今天发布新版本".encode("utf-8")
    body = struct.pack(
        "<BBBBQIHHH",
        1,
        1,
        0x06,
        0,
        0x0102030405060708,
        7,
        0,
        1,
        len(text),
    ) + text
    return with_crc(body)


def main() -> None:
    vectors = {
        "protocol_version": 1,
        "byte_order": "little-endian",
        "crc16_check": {"ascii": "123456789", "expected": "29b1"},
        "audio": {
            "description": "20 ms independently decodable IMA-ADPCM frame",
            "hex": audio_vector().hex(),
        },
        "start_session_command": {
            "description": "START_SESSION request_id=0x1234",
            "hex": command_vector().hex(),
        },
        "caption": {
            "description": "Final UTF-8 caption revision 7",
            "text": "今天发布新版本",
            "hex": caption_vector().hex(),
        },
    }
    target = Path(__file__).with_name("test_vectors.json")
    target.write_text(json.dumps(vectors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
