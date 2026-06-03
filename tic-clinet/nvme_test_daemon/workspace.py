"""PPS2 工作目錄解析 — 在 /nvme/ 下尋找最新 PPS2 開頭資料夾。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def find_latest_pps2_folder(base_dir: Path, prefix: str = "PPS2") -> Optional[Path]:
    """
    回傳 base_dir 底下名稱以 prefix 開頭、修改時間最新的資料夾。
    """
    if not base_dir.is_dir():
        logger.warning("工作目錄不存在: %s", base_dir)
        return None

    candidates = [
        p
        for p in base_dir.iterdir()
        if p.is_dir() and p.name.startswith(prefix)
    ]
    if not candidates:
        logger.warning("找不到 %s 開頭的資料夾: %s", prefix, base_dir)
        return None

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    logger.info("使用最新工作資料夾: %s", latest)
    return latest


def resolve_debug_log_dir(pps2_folder: Path, relative: str = "app/Debug/log") -> Path:
    return pps2_folder / relative
