"""NVMe 掉碟偵測 — Linux sysfs 與 Mac 開發用 Mock 後端。"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Set

logger = logging.getLogger(__name__)

NVME_SYSFS = Path("/sys/class/nvme")


class DiskMonitorBase(ABC):
    """碟片監控抽象介面。"""

    @abstractmethod
    def discover_devices(self) -> Set[str]: ...

    @abstractmethod
    def establish_baseline(self) -> Set[str]: ...

    @property
    @abstractmethod
    def baseline(self) -> Set[str]: ...

    @abstractmethod
    def check_for_drop(self) -> List[str]: ...

    @abstractmethod
    def is_healthy_for_idle(self) -> bool: ...

    @abstractmethod
    def refresh_baseline_if_recovered(self) -> bool: ...

    @abstractmethod
    def health_snapshot(self) -> dict: ...


class SysfsDiskMonitor(DiskMonitorBase):
    """Linux 正式環境：讀取 /sys/class/nvme/。"""

    def __init__(self, watch_list: List[str] | None = None) -> None:
        self._watch_list = watch_list or []
        self._baseline: Set[str] = set()

    def discover_devices(self) -> Set[str]:
        if not NVME_SYSFS.is_dir():
            logger.warning("NVMe sysfs 路徑不存在: %s", NVME_SYSFS)
            return set()

        devices = {
            p.name
            for p in NVME_SYSFS.iterdir()
            if p.is_dir() and p.name.startswith("nvme")
        }

        if self._watch_list:
            devices = devices.intersection(set(self._watch_list))

        return devices

    def establish_baseline(self) -> Set[str]:
        self._baseline = self.discover_devices()
        logger.info("NVMe 基準碟列表: %s", sorted(self._baseline))
        return set(self._baseline)

    @property
    def baseline(self) -> Set[str]:
        return set(self._baseline)

    def check_for_drop(self) -> List[str]:
        if not self._baseline:
            return []

        current = self.discover_devices()
        dropped = sorted(self._baseline - current)
        if dropped:
            logger.error("偵測到掉碟: %s (目前: %s)", dropped, sorted(current))
        return dropped

    def is_healthy_for_idle(self) -> bool:
        if not self._baseline:
            return NVME_SYSFS.is_dir()
        return len(self.check_for_drop()) == 0

    def refresh_baseline_if_recovered(self) -> bool:
        current = self.discover_devices()
        if self._watch_list:
            expected = set(self._watch_list)
            if expected.issubset(current):
                self._baseline = expected
                return True
            return False

        if current:
            self._baseline = current
            return True
        return False

    def health_snapshot(self) -> dict:
        current = self.discover_devices()
        dropped = sorted(self._baseline - current) if self._baseline else []
        return {
            "backend": "sysfs",
            "baseline_devices": sorted(self._baseline),
            "current_devices": sorted(current),
            "dropped_devices": dropped,
            "sysfs_accessible": NVME_SYSFS.is_dir(),
        }


class MockDiskMonitor(DiskMonitorBase):
    """
    Mac 開發環境：模擬 NVMe 控制器，無需 /sys/class/nvme/。

    可透過 POST /dev/simulate/drop 與 /dev/simulate/restore 手動觸發掉碟。
    """

    def __init__(
        self,
        mock_devices: List[str] | None = None,
        watch_list: List[str] | None = None,
    ) -> None:
        self._watch_list = watch_list or []
        initial = mock_devices or watch_list or ["nvme0"]
        self._baseline: Set[str] = set()
        self._current: Set[str] = set(initial)
        self._lock = threading.RLock()
        logger.warning(
            "使用 Mock 碟片監控 (DEV_MODE)，模擬裝置: %s",
            sorted(self._current),
        )

    def discover_devices(self) -> Set[str]:
        with self._lock:
            devices = set(self._current)
            if self._watch_list:
                devices = devices.intersection(set(self._watch_list))
            return devices

    def establish_baseline(self) -> Set[str]:
        with self._lock:
            self._baseline = self.discover_devices()
            logger.info("[Mock] NVMe 基準碟列表: %s", sorted(self._baseline))
            return set(self._baseline)

    @property
    def baseline(self) -> Set[str]:
        with self._lock:
            return set(self._baseline)

    def check_for_drop(self) -> List[str]:
        with self._lock:
            if not self._baseline:
                return []
            current = self.discover_devices()
            dropped = sorted(self._baseline - current)
            if dropped:
                logger.error(
                    "[Mock] 偵測到掉碟: %s (目前: %s)", dropped, sorted(current)
                )
            return dropped

    def is_healthy_for_idle(self) -> bool:
        with self._lock:
            if not self._baseline:
                return bool(self._current)
            return len(self.check_for_drop()) == 0

    def refresh_baseline_if_recovered(self) -> bool:
        with self._lock:
            current = self.discover_devices()
            if self._watch_list:
                expected = set(self._watch_list)
                if expected.issubset(current):
                    self._baseline = expected
                    return True
                return False
            if current:
                self._baseline = current
                return True
            return False

    def health_snapshot(self) -> dict:
        with self._lock:
            current = self.discover_devices()
            dropped = sorted(self._baseline - current) if self._baseline else []
            return {
                "backend": "mock",
                "baseline_devices": sorted(self._baseline),
                "current_devices": sorted(current),
                "dropped_devices": dropped,
                "sysfs_accessible": False,
            }

    def simulate_drop(self, device: str) -> bool:
        """開發用：模擬指定 NVMe 控制器消失。"""
        with self._lock:
            if device not in self._current:
                return False
            self._current.remove(device)
            logger.warning("[Mock] 模擬掉碟: %s", device)
            return True

    def simulate_restore(self, device: str | None = None) -> List[str]:
        """開發用：還原掉碟的裝置；device 為 None 時還原全部 baseline。"""
        with self._lock:
            restored: List[str] = []
            targets = [device] if device else sorted(self._baseline)
            for dev in targets:
                if dev and dev not in self._current:
                    self._current.add(dev)
                    restored.append(dev)
            if restored:
                logger.info("[Mock] 模擬還原碟片: %s", restored)
            return restored


def create_disk_monitor(
    *,
    dev_mode: bool,
    mock_devices: List[str],
    watch_list: List[str],
) -> DiskMonitorBase:
    if dev_mode:
        return MockDiskMonitor(mock_devices=mock_devices, watch_list=watch_list)
    return SysfsDiskMonitor(watch_list=watch_list)


# 向後相容別名
DiskMonitor = SysfsDiskMonitor
