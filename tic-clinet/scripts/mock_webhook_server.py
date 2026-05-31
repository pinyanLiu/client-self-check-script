#!/usr/bin/env python3
"""
本機 Mock Webhook 伺服器 — 在 Mac 開發時代替 n8n 接收 POST。

用法:
  python scripts/mock_webhook_server.py

三個端點對應 Daemon 的三種 Webhook:
  http://127.0.0.1:9001/log
  http://127.0.0.1:9001/heartbeat
  http://127.0.0.1:9001/alert
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import FastAPI, Request
import uvicorn

app = FastAPI(title="Mock n8n Webhook")


def _print_event(kind: str, payload: dict) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'=' * 60}")
    print(f"[{ts}] {kind.upper()}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


@app.post("/log")
async def receive_log(request: Request) -> dict:
    payload = await request.json()
    _print_event("log", payload)
    return {"ok": True}


@app.post("/heartbeat")
async def receive_heartbeat(request: Request) -> dict:
    payload = await request.json()
    _print_event("heartbeat", payload)
    return {"ok": True}


@app.post("/alert")
async def receive_alert(request: Request) -> dict:
    payload = await request.json()
    _print_event("alert", payload)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9001, log_level="warning")
