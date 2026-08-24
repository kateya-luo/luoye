"""Disposable real-HTTP integration test for ClearMeeting 0.14 / API/2."""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parent
DEVICE = "LY-A1B2AABBCCDD"
PROTOCOL = "luoye-device-api/2"
FIRMWARE = "0.8.2"
PASSWORD = "Smoke-Only-Password-2026"
BLOCK = 10 * 1024 * 1024


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def expect(response: httpx.Response, status: int, label: str) -> dict:
    if response.status_code != status:
        raise AssertionError(f"{label}: {response.status_code}: {response.text[:1200]}")
    return response.json() if response.content else {}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def stream(data: bytes):
    for offset in range(0, len(data), 64 * 1024):
        yield data[offset : offset + 64 * 1024]


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    process: subprocess.Popen[str] | None = None
    passed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="clearmeeting-api2-", ignore_cleanup_errors=True) as data:
        env = os.environ.copy()
        env.update({
            "DATA_DIR": data,
            "TEST_ACCOUNT_PASSWORD": PASSWORD,
            "AUTH_SECRET": "smoke-auth-secret-0123456789abcdef0123456789abcdef",
            "DEVICE_API_SECRET": "smoke-device-secret-0123456789abcdef0123456789abcdef",
            "DEVICE_AUTH_PROFILE": "engineering",
            "SERVER_RELEASE": "clearmeeting-server-v0.15.0-http-smoke",
            "ASR_MODE": "mock",
            "OFFLINE_ASR_MODE": "mock",
            "SPEAKER_MODE": "off",
            "DEEPSEEK_API_KEY": "",
        })
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        try:
            with httpx.Client(base_url=base, timeout=90.0) as client:
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        raise RuntimeError((process.stdout.read() if process.stdout else "")[-4000:])
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise TimeoutError("uvicorn startup timeout")
                    time.sleep(0.1)

                build = expect(client.get("/api/v2/build-info"), 200, "build")
                assert build["server_version"] == "0.15.0"
                assert build["api_contract"] == PROTOCOL
                assert build["range_block_bytes"] == BLOCK
                for old in ("/api/v1/build-info", "/api/v1/device/pair/start"):
                    assert client.get(old).status_code == 404
                passed.append("contract and old device routes")

                def login(name: str) -> str:
                    return str(expect(client.post("/api/v1/auth/login", json={
                        "username": name, "password": PASSWORD}), 200, f"login {name}")["token"])

                account_a, account_b = login("TEST1"), login("TEST2")
                headers = {"X-Luoye-Protocol": PROTOCOL, "X-Luoye-Firmware": FIRMWARE,
                           "X-Luoye-Device": DEVICE}
                nonce = "0123456789abcdef0123456789abcdef"
                expect(client.post("/api/v2/device/pair/start", headers=headers, json={
                    "device_id": DEVICE, "pairing_code": "731946", "nonce": nonce,
                    "firmware_version": FIRMWARE, "hardware_revision": "LY-HW-ENG-20260710",
                    "capabilities": ["fixed_sd", "bulk_upload_10mib", "range_repair"],
                    "protocol_version": PROTOCOL}), 200, "pair start")
                expect(client.post("/api/v2/me/devices/claim", headers=auth(account_a),
                                   json={"pairing_code": "731946"}), 200, "claim")
                bound = expect(client.post("/api/v2/device/pair/status", headers=headers,
                                           json={"device_id": DEVICE, "nonce": nonce}), 200,
                               "pair status")
                device = headers | auth(str(bound["device_token"]))
                assert expect(client.get("/api/v2/me/devices", headers=auth(account_b)),
                              200, "account B devices")["devices"] == []
                assert client.get(f"/api/v2/me/devices/{DEVICE}/storage",
                                  headers=auth(account_b)).status_code in (403, 404)
                passed.append("pairing and two-account isolation")

                live_created = expect(client.post(
                    "/api/v2/device/sessions", headers=device | {
                        "Idempotency-Key": "api2-live-create"},
                    json={"client_session_id": "api2-live", "binding_generation": 1,
                          "upload_mode": "live"}), 200, "create live")
                live_id = str(live_created["server_session_id"])
                live_head = b"\x31\x00" * 160
                live_tail = b"\x32\x00" * 160

                def put_live(seq: int, offset: int, block: bytes, label: str) -> dict:
                    return expect(client.put(
                        f"/api/v2/device/sessions/{live_id}/audio/{seq}",
                        headers=device | {
                            "Content-Type": "audio/L16;rate=16000;channels=1",
                            "X-Byte-Offset": str(offset),
                            "X-Byte-Count": str(len(block)),
                            "X-Content-SHA256": hashlib.sha256(block).hexdigest()},
                        content=block), 200, label)

                put_live(0, 0, live_head, "live head")
                resumed = expect(client.post(
                    f"/api/v2/device/sessions/{live_id}/live-resume",
                    headers=device | {"Idempotency-Key": "api2-live-resume"},
                    json={"binding_generation": 1,
                          "gap_start_bytes": len(live_head),
                          "resume_offset_bytes": len(live_head) + 640}), 200,
                    "live resume")
                assert resumed["gap_pending"] and resumed["live_next_seq"] == 1
                latest = put_live(1, len(live_head) + 640, live_tail, "live current")
                assert latest["acknowledged_bytes"] == len(live_head)
                assert latest["live_acknowledged_bytes"] == len(live_head) + 640 + len(live_tail)
                deferred = expect(client.post(
                    f"/api/v2/device/sessions/{live_id}/defer",
                    headers=device | {"Idempotency-Key": "api2-live-defer"},
                    json={"binding_generation": 1,
                          "total_bytes": len(live_head) + 640 + len(live_tail),
                          "total_samples": (len(live_head) + 640 + len(live_tail)) // 2}),
                    200, "defer live gap")
                assert deferred["status"] == "awaiting_repair" and deferred["missing_bytes"] == 640
                passed.append("reconnect resumes current live epoch and defers the gap")

                total = BLOCK + 4096
                pcm = (b"\x11\x00\x22\x00" * (total // 4))
                create_body = {
                    "client_session_id": "api2-bulk", "binding_generation": 1,
                    "scene": "meeting", "upload_mode": "bulk",
                    "audio": {"codec": "pcm_s16le", "sample_rate": 16000,
                              "channels": 1, "bits_per_sample": 16},
                }
                created = expect(client.post("/api/v2/device/sessions", headers=device | {
                    "Idempotency-Key": "api2-bulk-create"}, json=create_body), 200, "create bulk")
                server_id = str(created["server_session_id"])
                plan_body = {"total_bytes": total, "total_samples": total // 2,
                             "binding_generation": 1, "mode": "bulk"}
                plan = expect(client.post(
                    f"/api/v2/device/sessions/{server_id}/upload-plan", headers=device,
                    json=plan_body), 200, "initial plan")
                assert plan["missing_ranges"][:2] == [
                    {"offset": 0, "length": BLOCK}, {"offset": BLOCK, "length": 4096}]

                def put_range(offset: int, block: bytes, label: str) -> dict:
                    sha = hashlib.sha256(block).hexdigest()
                    return expect(client.put(
                        f"/api/v2/device/sessions/{server_id}/audio-range",
                        headers=device | {"Content-Type": "audio/L16;rate=16000;channels=1",
                                          "Content-Length": str(len(block)),
                                          "X-Byte-Offset": str(offset),
                                          "X-Byte-Count": str(len(block)),
                                          "X-Content-SHA256": sha},
                        content=stream(block)), 200, label)

                # Deliberately upload out of order, then replay the 10 MiB request.
                tail = put_range(BLOCK, pcm[BLOCK:], "tail first")
                assert not tail["complete"]
                first = put_range(0, pcm[:BLOCK], "10 MiB streamed")
                assert first["complete"] and first["covered_bytes"] == total
                duplicate = put_range(0, pcm[:BLOCK], "duplicate 10 MiB")
                assert duplicate["duplicate"] is True
                passed.append("out-of-order 10 MiB streaming, coverage and duplicate")

                expect(client.put(f"/api/v2/device/sessions/{server_id}/marks/mark-000001",
                                  headers=device,
                                  json={"offset_samples": 16000, "kind": "mark"}), 200,
                       "mark")
                complete = expect(client.post(
                    f"/api/v2/device/sessions/{server_id}/complete",
                    headers=device | {"Idempotency-Key": "api2-bulk-complete"},
                    json={"total_bytes": total, "total_samples": total // 2,
                          "binding_generation": 1}), 200, "complete")
                assert complete["complete"] and complete["status"] in ("processing", "done")
                replay = expect(client.post(
                    f"/api/v2/device/sessions/{server_id}/complete",
                    headers=device | {"Idempotency-Key": "api2-bulk-complete"},
                    json={"total_bytes": total, "total_samples": total // 2,
                          "binding_generation": 1}), 200, "complete replay")
                assert replay["complete"]
                passed.append("MARK, atomic complete and idempotent replay")

                cancelled_session = expect(client.post(
                    "/api/v2/device/sessions", headers=device | {
                        "Idempotency-Key": "api2-cancel-create"},
                    json={**create_body, "client_session_id": "api2-cancel",
                          "upload_mode": "repair"}), 200, "create cancel")
                cancelled = expect(client.post(
                    f"/api/v2/device/sessions/{cancelled_session['server_session_id']}/cancel",
                    headers=device, json={"binding_generation": 1,
                                          "reason": "http_smoke"}), 200, "cancel")
                assert cancelled["cancelled"] is True
                passed.append("incomplete session cancellation")

                expect(client.delete(f"/api/v2/me/devices/{DEVICE}/binding",
                                     headers=auth(account_a)), 200, "unbind")
                assert client.get("/api/v2/device/agenda", headers=device).status_code in (401, 403)
                passed.append("unbind revokes old token")
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
            time.sleep(0.3)

    for item in passed:
        print(f"PASS  {item}")
    print(f"API/2 REAL HTTP PASS ({len(passed)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
