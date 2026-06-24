"""
NVMe FW naming query — 與 driver 類型無關。

支援兩種 driver 路徑：
  1. Phison p_nvme 自訂 driver  → /dev/nvme_dev0
  2. 標準 kernel nvme            → /dev/nvme0, /dev/nvme0n1, …

優先順序：
  1. 先試 Phison char device（/dev/nvme_dev0），成功即可。
  2. 若沒有 /dev/nvme_dev0，掃 /dev 找 /dev/nvme{0,1,2,…} 標準裝置。
  3. 任一路徑讀到 fr 即回傳。

使用 nvme-cli 的 id-ctrl 指令，不需要直接 ioctl。
"""

from __future__ import annotations

import logging
import subprocess
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Phison 自訂 driver 的 character device 路徑
PHISON_CHAR_DEVICE = Path("/dev/nvme_dev0")

# 標準 kernel nvme 裝置 pattern（nvme0, nvme1, …）
STD_NVME_PATTERN = re.compile(r"^nvme\d+$")


def _driver_is_phison() -> bool:
    """檢查 Phison p_nvme driver 是否已載入。"""
    try:
        result = subprocess.run(
            ["lsmod"], capture_output=True, text=True, timeout=10
        )
        return "p_nvme" in result.stdout
    except Exception:
        return False


def _read_fw_naming_from_device(device: Path) -> Optional[Dict[str, str]]:
    """
    對單一裝置執行 nvme id-ctrl，回傳 FW naming dict 或 None。
    """
    try:
        result = subprocess.run(
            ["nvme", "id-ctrl", str(device)],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        logger.error("nvme-cli 未安裝，無法查詢 FW naming")
        return None
    except subprocess.TimeoutExpired:
        logger.error("nvme id-ctrl 逾時 (device=%s)", device)
        return None
    except Exception:
        logger.exception("nvme id-ctrl 執行失敗 (device=%s)", device)
        return None

    if result.returncode != 0:
        logger.warning(
            "nvme id-ctrl 回傳錯誤 (device=%s, rc=%d): %s",
            device, result.returncode, result.stderr.strip(),
        )
        return None

    info: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        m = re.match(r"^(sn|mn|fr|vid|ssvid)\s*:\s*(.+)$", line)
        if m:
            info[m.group(1)] = m.group(2).strip()

    # 只有 fr 是必要欄位
    if "fr" not in info:
        logger.warning(
            "FW naming 缺少必要欄位 fr (device=%s): 取得=%s", device, sorted(info)
        )
        return None

    return info


def _find_phison_device() -> Optional[Path]:
    """找 Phison 自訂 driver 的 char device。"""
    if _driver_is_phison() and PHISON_CHAR_DEVICE.exists():
        return PHISON_CHAR_DEVICE

    # fallback: 掃 /dev 找 nvme_dev*
    dev_dir = Path("/dev")
    try:
        for entry in dev_dir.iterdir():
            if entry.match("nvme_dev*") and entry.is_char_device():
                return entry
    except OSError:
        pass

    return None


def _find_standard_devices() -> List[Path]:
    """掃 /dev 找標準 kernel nvme 裝置（nvme0, nvme1, …）。"""
    dev_dir = Path("/dev")
    devices: List[Path] = []
    try:
        for entry in dev_dir.iterdir():
            if entry.match("nvme*") and entry.is_char_device():
                if entry.name.startswith("nvme") and not entry.name.startswith("nvme_dev"):
                    if entry.match("nvme_dev*"):
                        # 排除 nvme_dev0 之類自訂 char device
                        continue
                    if STD_NVME_PATTERN.match(entry.name):
                        devices.append(entry)
    except OSError:
        pass
    return devices


def query_fw_naming(device: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """
    讀取 NVMe 裝置的 FW naming（sn / mn / fr / vid / ssvid）。

    支援兩種 driver：
      - Phison p_nvme 自訂 driver（/dev/nvme_dev0）
      - 標準 kernel nvme（/dev/nvme0, /dev/nvme0n1, …）

    優先順序：
      1. 若指定 device 就只查該裝置
      2. 先試 Phison char device，成功即可
      3. 再試標準 kernel nvme 裝置

    Args:
        device: 指定 device path；None 時自動偵測。

    Returns:
        dict {"sn": ..., "mn": ..., "fr": ..., "vid": ..., "ssvid": ...}
        讀取失敗時回傳 None。
    """
    if device is not None:
        if not device.exists():
            logger.warning("指定裝置不存在: %s", device)
            return None
        return _read_fw_naming_from_device(device)

    # 1. 先試 Phison 自訂 driver
    phison_dev = _find_phison_device()
    if phison_dev is not None and phison_dev.exists():
        logger.info("嘗試 Phison 裝置: %s", phison_dev)
        info = _read_fw_naming_from_device(phison_dev)
        if info is not None:
            logger.info("Phison FW naming 讀取成功: fr=%s", info.get("fr", ""))
            return info
        logger.info("Phison 裝置無法讀取 fr")

    # 2. 再試標準 kernel nvme 裝置
    std_devs = _find_standard_devices()
    if not std_devs:
        logger.info("找不到標準 kernel NVMe 裝置 (/dev/nvme{0,1,...})")
    else:
        logger.info("找到標準 NVMe 裝置: %s", [str(d) for d in std_devs])

    for dev in std_devs:
        logger.info("嘗試標準裝置: %s", dev)
        info = _read_fw_naming_from_device(dev)
        if info is not None:
            logger.info("標準 NVMe FW naming 讀取成功: fr=%s", info.get("fr", ""))
            return info
        logger.info("標準裝置 %s 無法讀取 fr", dev)

    logger.info("所有 NVMe 裝置都無法讀取 FW naming")
    return None
