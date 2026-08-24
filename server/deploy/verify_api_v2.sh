#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-${CLEARMEETING_BASE_URL:-http://127.0.0.1}}"
base_url="${base_url%/}"
expected_version="${EXPECTED_SERVER_VERSION:-1.0.1}"
expected_contract="${EXPECTED_API_CONTRACT:-luoye-device-api/2}"

command -v curl >/dev/null 2>&1 || { echo "失败：需要 curl" >&2; exit 1; }
if command -v python3 >/dev/null 2>&1 && python3 -c 'import json' >/dev/null 2>&1; then
  python_cmd=python3
elif command -v python >/dev/null 2>&1 && python -c 'import json' >/dev/null 2>&1; then
  python_cmd=python
else
  echo "失败：需要可用的 Python 3" >&2
  exit 1
fi

echo "== 验证 ${base_url} =="
health_json="$(curl --fail --silent --show-error --max-time 10 "${base_url}/health")"
printf '%s' "${health_json}" | "${python_cmd}" -c '
import json, sys
body = json.load(sys.stdin)
if body.get("status") != "ok":
    raise SystemExit(f"/health 返回异常: {body!r}")
'
echo "PASS  /health"

build_json="$(curl --fail --silent --show-error --max-time 10 "${base_url}/api/v2/build-info")"
export VERIFY_EXPECTED_VERSION="${expected_version}"
export VERIFY_EXPECTED_CONTRACT="${expected_contract}"
printf '%s' "${build_json}" | "${python_cmd}" -c '
import json, os, sys
body = json.load(sys.stdin)
required = {"device_pairing", "idempotent_upload", "agenda_sync", "voice_todo",
            "storage_management", "network_scheduler", "bulk_upload_10mib",
            "range_repair", "streaming_request_body", "session_cancel",
            "live_epoch_resume", "manual_gap_repair", "independent_sd_delete"}
required.add("semantic_timeline_v2")
required.add("semantic_timeline_v3_anchored")
required.add("offline_asr_pipeline_v1")
required.add("device_live_partial_caption_v1")
required.add("device_live_partial_caption_v2")
required.add("device_caption_upsert_v1")
required.add("device_revision_channels_v1")
actual_version = body.get("server_version")
actual_contract = body.get("api_contract")
if actual_version != os.environ["VERIFY_EXPECTED_VERSION"]:
    raise SystemExit(f"server_version 不匹配: {actual_version!r}")
if actual_contract != os.environ["VERIFY_EXPECTED_CONTRACT"]:
    raise SystemExit(f"api_contract 不匹配: {actual_contract!r}")
missing = sorted(required - set(body.get("capabilities") or []))
if missing:
    raise SystemExit(f"build-info 缺少能力: {missing}")
print("PASS  /api/v2/build-info")
print("      release=" + str(body.get("server_release")))
print("      minimum_firmware=" + str(body.get("minimum_firmware")))
'

echo "验证通过：${base_url} 正在运行 ClearMeeting ${expected_version} / ${expected_contract}"
