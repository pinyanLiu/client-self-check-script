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
    result: str
    issues: List[str] = field(default_factory=list)
    files: List[LogFileEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "result": self.result,
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
    pattern_name: str,
    before: Dict[str, float],
    *,
    started_at: float,
    max_file_bytes: int,
) -> Dict[str, List[LogFileEntry]]:
    """
    收集執行期間新增或更新的 log 檔。

    entries 固定分成兩類：
    1. "pattern_name"：檔名包含 pattern_name 的 log
    2. "DMAList"：檔名包含 "DMAList" 的 log

    比對條件：
    1. 檔案不在 before 中，或 mtime 大於 before 記錄值
    2. mtime 晚於 started_at
    3. 檔名必須包含 pattern_name 或 DMAList
    4. 忽略 Pass/Fail 資料夾底下的log
    """
    entries: Dict[str, List[LogFileEntry]] = {
        "pattern_name": [],
        "DMAList": [],
    }

    if not log_dir.is_dir():
        return entries

    for p in sorted(log_dir.rglob("*")):
        if not p.is_file():
            continue

        # 忽略 Pass / Fail 資料夾底下的檔案
        relative_parts = p.relative_to(log_dir).parts
        parent_dirs = relative_parts[:-1]
        if any(part in ("Pass", "Fail") for part in parent_dirs):
            continue

        name_lower = p.name.lower()
        matched_keys: List[str] = []

        if pattern_name and pattern_name.lower() in name_lower:
            matched_keys.append("pattern_name")

        if "DMAList" in p.name:
            matched_keys.append("DMAList")

        # 只收檔名包含 pattern_name 或 DMAList 的 log
        if not matched_keys:
            continue

        key = str(p.resolve())

        try:
            stat = p.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError:
            continue

        prev_mtime = before.get(key)
        is_new_or_updated = prev_mtime is None or mtime > prev_mtime
        is_after_start = mtime >= started_at - 1.0  # 容許 1 秒時鐘誤差

        if not (is_new_or_updated and is_after_start):
            continue

        content, truncated = _read_file_capped(p, max_file_bytes)

        entry = LogFileEntry(
            path=key,
            name=p.name,
            size=size,
            content=content,
            truncated=truncated,
        )

        for matched_key in matched_keys:
            entries[matched_key].append(entry)

    return entries


def validate_debug_logs(
    entries: Dict[str, List[LogFileEntry]],
    *,
    completion_marker: str,
    require_new_logs: bool,
) -> LogValidation:
    """
    嚴格判斷 Pattern Pass / Fail / Abnormal。
    判斷 Pattern log 是否缺漏。

    只檢查 entries["pattern_name"]。
    entries["DMAList"] 只收集，不參與判斷。

    嚴格規則：
    1. 沒有 pattern log -> Abnormal
    2. pattern log 為空或無有效內容 -> Abnormal
    3. 沒有設定 completion_marker -> Abnormal
    4. pattern log 缺少 completion_marker -> Abnormal
    5. 同時出現 Pass 和 Fail -> Abnormal
    6. 只有 Fail -> Fail
    7. 只有 Pass，且沒有任何問題 -> Pass
    8. 沒有 Pass / Fail -> Abnormal
    """
    issues: List[str] = []
    result = "Abnormal"

    debug_entries = entries.get("pattern_name", [])
    DMA_entries = entries.get("DMAList", [])

    marker = (completion_marker or "").strip()

    # 嚴格模式下，沒有 log 就不能判斷為 Pass
    if not debug_entries:
        if require_new_logs:
            issues.append("執行後未產生或更新任何 log 檔")
        else:
            issues.append("未找到 Pattern log，無法判斷測試結果")

        return LogValidation(
            result=result,
            issues=issues,
            files=DMA_entries,
        )

    # 檢查 log 檔案是否有效
    valid_contents: List[str] = []

    for entry in debug_entries:
        if entry.size == 0:
            issues.append(f"log 檔為空: {entry.name}")
            continue

        if not entry.content.strip():
            issues.append(f"log 檔無有效內容: {entry.name}")
            continue

        valid_contents.append(entry.content)

    # 沒有任何有效 log 內容
    if not valid_contents:
        issues.append("所有 Pattern log 都沒有有效內容")

        return LogValidation(
            result=result,
            issues=issues,
            files=DMA_entries,
        )

    combined = "\n".join(valid_contents)

    # 嚴格模式下，completion_marker 必須有設定
    if not marker:
        issues.append("未設定結束標記，無法嚴格判斷 Pattern log 是否完整")

        return LogValidation(
            result=result,
            issues=issues,
            files=DMA_entries,
        )

    # 嚴格模式下，log 必須包含結束標記
    if marker not in combined:
        issues.append(f"Pattern log 中缺少結束標記 '{marker}'")

        return LogValidation(
            result=result,
            issues=issues,
            files=DMA_entries,
        )

    has_pass = "[Test Result] Pass" in combined
    has_fail = "[Test Result] Fail" in combined

    # 同時出現 Pass / Fail，代表結果不一致，判為 Abnormal
    if has_pass and has_fail:
        issues.append("Pattern log 中同時出現 Pass 與 Fail，結果不一致")
        result = "Abnormal"

    elif has_fail:
        result = "Fail"

    elif has_pass:
        # 只有在完全沒有 issues 的情況下才給 Pass
        if issues:
            result = "Abnormal"
        else:
            result = "Pass"

    else:
        issues.append("Pattern log 中找不到測試結果標記 '[Test Result] Pass' 或 '[Test Result] Fail'")
        result = "Abnormal"

    return LogValidation(
        result=result,
        issues=issues,
        files=DMA_entries,
    )