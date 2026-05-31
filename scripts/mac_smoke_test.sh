#!/usr/bin/env bash
# Mac 煙霧測試 — 需先啟動 mock_webhook_server.py 與 Daemon
set -euo pipefail

BASE="${API_BASE:-http://127.0.0.1:8080}"

echo "== 1. Health =="
curl -s "$BASE/health" | python3 -m json.tool

echo -e "\n== 2. 正常指令 =="
curl -s -X POST "$BASE/command" \
  -H "Content-Type: application/json" \
  -d '{"command":"echo hello && echo TEST_COMPLETED","command_id":"mac-test-001"}' \
  | python3 -m json.tool
sleep 2

echo -e "\n== 3. Busy 測試 (背景長指令) =="
curl -s -X POST "$BASE/command" \
  -H "Content-Type: application/json" \
  -d '{"command":"sleep 10","command_id":"mac-test-busy"}' > /dev/null
curl -s -X POST "$BASE/command" \
  -H "Content-Type: application/json" \
  -d '{"command":"echo should-fail","command_id":"mac-test-busy2"}' \
  | python3 -m json.tool || true
sleep 11

echo -e "\n== 4. 模擬掉碟 =="
curl -s -X POST "$BASE/dev/simulate/drop" \
  -H "Content-Type: application/json" \
  -d '{"device":"nvme0"}' | python3 -m json.tool

echo -e "\n== 5. 嘗試下指令 (應 503 abnormal) =="
curl -s -X POST "$BASE/command" \
  -H "Content-Type: application/json" \
  -d '{"command":"echo fail","command_id":"mac-test-after-drop"}' \
  | python3 -m json.tool || true

echo -e "\n== 6. 還原碟片 + Reset =="
curl -s -X POST "$BASE/dev/simulate/restore" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool
curl -s -X POST "$BASE/reset" \
  -H "Content-Type: application/json" \
  -d '{"reason":"Mac 測試復原"}' | python3 -m json.tool

echo -e "\n完成。請查看 mock_webhook_server 終端機的 log / heartbeat / alert 輸出。"
