"""PPS2 app/Debug/log 收集與完整性判斷。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LogFileEntry:
    path: str
    name: str
    size: int
    content: str
    truncated: bool = False


@dataclass
class LogValidation:
    ok: bool
    issues: List[str] = field(default_factory=list)
    files: List[LogFileEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "files": [
                {
                    "path": f.path,
                    "name": f.name,
                    "size": f.size,
                    "truncated": f.truncated,
                    "content": f.content,
                }
                for f in self.files
            ],
        }


def snapshot_log_dir(log_dir: Path) -> Dict[str, float]:
    """記錄 log 目錄內各檔案的 mtime（用於判斷執行期間是否有新/更新 log）。"""
    if not log_dir.is_dir():
        return {}
    snapshot: Dict[str, float] = {}
    for p in log_dir.rglob("*"):
        if p.is_file():
            try:
                snapshot[str(p.resolve())] = p.stat().st_mtime
            except OSError:
                continue
    return snapshot


def _read_file_capped(path: Path, max_bytes: int) -> tuple[str, bool]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("無法讀取 log: %s (%s)", path, exc)
        return "", False
    truncated = len(data) > max_bytes
    if truncated:
        data = data[-max_bytes:]
    return data.decode(errors="replace"), truncated


def collect_logs_since(
    log_dir: Path,
    before: Dict[str, float],
    *,
    started_at: float,
    max_file_bytes: int,
) -> List[LogFileEntry]:
    """
    收集執行期間新增或更新的 log 檔。
    比對：不在 before 中、mtime 晚於 started_at、或 mtime 大於 before 記錄值。
    """
    if not log_dir.is_dir():
        return []

    entries: List[LogFileEntry] = []
    for p in sorted(log_dir.rglob("*")):
        if not p.is_file():
            continue
        key = str(p.resolve())
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue

        prev_mtime = before.get(key)
        is_new_or_updated = prev_mtime is None or mtime > prev_mtime
        is_after_start = mtime >= started_at - 1.0  # 容許 1 秒時鐘誤差

        if not (is_new_or_updated and is_after_start):
            continue

        content, truncated = _read_file_capped(p, max_file_bytes)
        entries.append(
            LogFileEntry(
                path=key,
                name=p.name,
                size=p.stat().st_size,
                content=content,
                truncated=truncated,
            )
        )
    return entries


def validate_debug_logs(
    entries: List[LogFileEntry],
    *,
    completion_marker: str,
    require_new_logs: bool,
) -> LogValidation:
    """判斷 Debug log 是否缺漏。"""
    issues: List[str] = []

    if require_new_logs and not entries:
        issues.append("執行後未產生或更新任何 Debug log 檔")

    for entry in entries:
        if entry.size == 0:
            issues.append(f"log 檔為空: {entry.name}")
        elif not entry.content.strip():
            issues.append(f"log 檔無有效內容: {entry.name}")

    marker = (completion_marker or "").strip()
    if marker and entries:
        combined = "\n".join(e.content for e in entries)
        if marker not in combined:
            issues.append(f"Debug log 中缺少結束標記 '{marker}'")
    elif marker and not entries:
        issues.append(f"無法檢查結束標記 '{marker}'（無 log 檔）")

    return LogValidation(ok=not issues, issues=issues, files=entries)
