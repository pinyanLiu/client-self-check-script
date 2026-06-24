"""NVMe 掉碟偵測 — Linux sysfs 與 Mac 開發用 Mock 後端。"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Set
import re

logger = logging.getLogger(__name__)

NVME_SYSFS = Path("/sys/class/nvme_interface")


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
    """
    Linux 正式環境：透過 sysfs 監控自訂 NVMe interface 背後的 PCIe endpoint。

    判斷方式：
    1. 從 /sys/class/nvme_interface/nvme_devX 找到實際 sysfs 路徑
    2. 從實際路徑解析 PCIe BDF，例如 0000:3d:00.0
    3. 讀 /sys/bus/pci/devices/<BDF>/config 前 4 bytes
    4. 若讀不到、路徑不存在、或回傳 ffffffff，就視為裝置已掉線

    注意：
    - 不碰 /dev/nvme_devX
    - 不送 ioctl
    - 不送 NVMe command
    - 不做 PCI rescan
    """

    BDF_PATTERN = re.compile(
        r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]"
    )

    def __init__(self, watch_list: List[str] | None = None) -> None:
        self._watch_list = watch_list or []
        self._baseline: Set[str] = set()
        self._fw_naming: dict | None = None  # 啟動時讀一次，不重複查詢

        # device name -> BDF，例如 {"nvme_dev0": "0000:3d:00.0"}
        self._device_bdf_cache: dict[str, str] = {}

        # device name -> 初始 PCI config 前 4 bytes，例如 {"nvme_dev0": "4d1440a8"}
        self._initial_config_cache: dict[str, str] = {}

    def _resolve_device_path(self, device_name: str) -> Path | None:
        """
        解析 /sys/class/nvme_interface/nvme_devX 的真實路徑。
        """
        device_path = NVME_SYSFS / device_name

        try:
            return device_path.resolve(strict=True)
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("解析 sysfs device path 失敗: %s, error=%s", device_path, exc)
            return None

    def _extract_bdf_from_device(self, device_name: str) -> str | None:
        """
        從 sysfs 真實路徑解析 PCIe BDF。

        範例：
        /sys/devices/pci0000:00/.../0000:3d:00.0/nvme_interface/nvme_dev0

        會取最後一個 BDF：
        0000:3d:00.0
        """
        if device_name in self._device_bdf_cache:
            return self._device_bdf_cache[device_name]

        real_path = self._resolve_device_path(device_name)
        if real_path is None:
            return None

        matches = self.BDF_PATTERN.findall(str(real_path))
        if not matches:
            logger.warning("無法從 sysfs path 解析 PCIe BDF: device=%s, path=%s", device_name, real_path)
            return None

        bdf = matches[-1]
        self._device_bdf_cache[device_name] = bdf
        return bdf

    def _read_pci_config_header(self, bdf: str) -> bytes | None:
        """
        只讀 PCI config space 前 4 bytes。

        前 4 bytes 是：
        - Vendor ID
        - Device ID

        若裝置不存在、讀不到、或 kernel 回錯，回傳 None。
        """
        config_path = Path(f"/sys/bus/pci/devices/{bdf}/config")

        if not config_path.exists():
            return None

        try:
            with config_path.open("rb") as fp:
                data = fp.read(4)
        except OSError:
            return None

        if len(data) < 4:
            return None

        return data

    def _is_pci_endpoint_alive(self, device_name: str) -> bool:
        """
        判斷指定 nvme_devX 背後的 PCIe endpoint 是否仍有回應。
        """
        bdf = self._extract_bdf_from_device(device_name)
        if not bdf:
            return False

        data = self._read_pci_config_header(bdf)
        if data is None:
            logger.warning("PCI config 讀取失敗，可能已掉碟: device=%s, bdf=%s", device_name, bdf)
            return False

        config_value = data.hex()

        if data == b"\xff\xff\xff\xff":
            logger.warning(
                "PCI endpoint 無回應，config space 回傳 ffffffff: device=%s, bdf=%s",
                device_name,
                bdf,
            )
            return False

        initial_config = self._initial_config_cache.get(device_name)
        if initial_config is not None and config_value != initial_config:
            logger.warning(
                "PCI config vendor/device id 改變，可能不是同一顆裝置: "
                "device=%s, bdf=%s, initial=%s, current=%s",
                device_name,
                bdf,
                initial_config,
                config_value,
            )
            return False

        return True

    def _list_sysfs_interface_devices(self) -> Set[str]:
        """
        列出 /sys/class/nvme_interface 底下的候選裝置。

        這裡只列出候選，不代表裝置真的還活著。
        真正是否還活著，要再透過 _is_pci_endpoint_alive() 判斷。
        """
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

    def discover_devices(self) -> Set[str]:
        """
        探測目前仍然健康的裝置。
        自訂 driver 可能在裝置掉線後仍殘留 sysfs node，
        所以這裡檢查背後 PCIe endpoint 是否還有回應。
        """

        # 舊邏輯：只要 sysfs node 存在就視為裝置存在。
        # 這種方式在 driver 沒有清掉 nvme_devX 時會誤判。
        #
        # if not NVME_SYSFS.is_dir():
        #     logger.warning("NVMe sysfs 路徑不存在: %s", NVME_SYSFS)
        #     return set()
        #
        # devices = {
        #     p.name
        #     for p in NVME_SYSFS.iterdir()
        #     if p.is_dir() and p.name.startswith("nvme")
        # }
        #
        # if self._watch_list:
        #     devices = devices.intersection(set(self._watch_list))
        #
        # return devices

        candidates = self._list_sysfs_interface_devices()
        alive_devices: Set[str] = set()

        for device_name in candidates:
            if self._is_pci_endpoint_alive(device_name):
                alive_devices.add(device_name)
            else:
                logger.warning("裝置疑似已掉線: %s", device_name)

        return alive_devices

    def establish_baseline(self) -> Set[str]:
        """
        建立基準裝置列表。

        建立 baseline 時會記錄每個裝置初始 PCI config 前 4 bytes。
        後續如果讀到的 vendor/device id 改變，會視為異常。
        """
        self._baseline = self.discover_devices()

        self._initial_config_cache.clear()

        for device_name in self._baseline:
            bdf = self._extract_bdf_from_device(device_name)
            if not bdf:
                continue

            data = self._read_pci_config_header(bdf)
            if data is None:
                continue

            if data != b"\xff\xff\xff\xff":
                self._initial_config_cache[device_name] = data.hex()

        logger.info("NVMe 基準碟列表: %s", sorted(self._baseline))
        logger.info("NVMe PCI config baseline: %s", self._initial_config_cache)

        return set(self._baseline)

    @property
    def baseline(self) -> Set[str]:
        return set(self._baseline)

    def check_for_drop(self) -> List[str]:
        """
        檢查目前是否有 baseline 裡面的裝置掉線。
        """
        if not self._baseline:
            return []

        current = self.discover_devices()
        dropped = sorted(self._baseline - current)

        if dropped:
            logger.error("偵測到掉碟: %s (目前健康裝置: %s)", dropped, sorted(current))

        return dropped

    def is_healthy_for_idle(self) -> bool:
        """
        給 idle 狀態使用的健康檢查。

        若尚未建立 baseline：
        - 有 watch_list 時，要求 watch_list 內的裝置都還活著
        - 沒有 watch_list 時，只要目前有任一健康裝置即可
        """
        if not self._baseline:
            current = self.discover_devices()

            if self._watch_list:
                return set(self._watch_list).issubset(current)

            return bool(current)

        return len(self.check_for_drop()) == 0

    def refresh_baseline_if_recovered(self) -> bool:
        """
        如果裝置恢復，刷新 baseline。
        """
        current = self.discover_devices()

        if self._watch_list:
            expected = set(self._watch_list)
            if expected.issubset(current):
                self._baseline = expected

                self._initial_config_cache.clear()
                for device_name in self._baseline:
                    bdf = self._extract_bdf_from_device(device_name)
                    if not bdf:
                        continue

                    data = self._read_pci_config_header(bdf)
                    if data and data != b"\xff\xff\xff\xff":
                        self._initial_config_cache[device_name] = data.hex()

                return True

            return False

        if current:
            self._baseline = current

            self._initial_config_cache.clear()
            for device_name in self._baseline:
                bdf = self._extract_bdf_from_device(device_name)
                if not bdf:
                    continue

                data = self._read_pci_config_header(bdf)
                if data and data != b"\xff\xff\xff\xff":
                    self._initial_config_cache[device_name] = data.hex()

            return True

        return False

    def health_snapshot(self) -> dict:
        """
        回傳目前健康狀態快照。

        注意：這裡不觸發 FW naming 查詢。
        FW naming 只在 daemon startup 時讀取一次（於 DaemonCore 內），
        避免在測試運行期間對 NVMe 發 admin command 造成干擾。
        """

        candidates = self._list_sysfs_interface_devices()
        current = self.discover_devices()
        dropped = sorted(self._baseline - current) if self._baseline else []

        device_details = {}

        for device_name in sorted(candidates):
            bdf = self._extract_bdf_from_device(device_name)
            config_value = None
            alive = False

            if bdf:
                data = self._read_pci_config_header(bdf)
                if data is not None:
                    config_value = data.hex()
                    alive = data != b"\xff\xff\xff\xff"

                    initial_config = self._initial_config_cache.get(device_name)
                    if initial_config is not None and config_value != initial_config:
                        alive = False

            device_details[device_name] = {
                "bdf": bdf,
                "pci_config_header": config_value,
                "alive": alive,
                "initial_config_header": self._initial_config_cache.get(device_name),
            }

        # FW naming 不在這裡查詢，避免測試中觸發 admin command。
        # 由 DaemonCore.startup() 在啟動時讀取一次，存入 health_snapshot。

        return {
            "backend": "sysfs_pci_config",
            "baseline_devices": sorted(self._baseline),
            "candidate_devices": sorted(candidates),
            "current_devices": sorted(current),
            "dropped_devices": dropped,
            "sysfs_accessible": NVME_SYSFS.is_dir(),
            "device_details": device_details,
            "fw_naming": self._fw_naming,
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
                "fw_naming": None,
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


def set_fw_naming_on_monitor(monitor: DiskMonitorBase, fw: dict | None) -> None:
    """
    在 daemon startup 時一次性寫入 FW naming。
    之後 heartbeat / abnormal 都不會再觸發 NVMe admin command。

    同時支援 SysfsDiskMonitor 與 MockDiskMonitor。
    """
    if isinstance(monitor, SysfsDiskMonitor):
        monitor._fw_naming = fw
    # MockDiskMonitor 沒有 _fw_naming，health_snapshot 裡會自行決定
