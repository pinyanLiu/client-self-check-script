"""產生供 n8n Data Table Upsert 使用的扁平狀態列。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .daemon import DaemonCore


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_table_row(
    core: "DaemonCore",
    *,
    event: str,
    alert_type: Optional[str] = None,
    message: Optional[str] = None,
    last_command_id: Optional[str] = None,
    last_command_status: Optional[str] = None,
) -> Dict[str, Any]:
    """
    將 Daemon 狀態壓平成單一列，欄位名稱對應 n8n Data Table 欄位。

    以 machine_ip 作為 Upsert 條件欄位（每台測試機一列）。
    """
    sm = core.state_machine.snapshot()
    disk = core.disk_monitor.health_snapshot()
    settings = core.settings

    baseline = disk.get("baseline_devices") or []
    current = disk.get("current_devices") or []
    dropped = disk.get("dropped_devices") or []
    fw = disk.get("fw_naming") or {}

    return {
        # --- Upsert 主鍵 (必填，每台機器唯一) ---
        "machine_ip": settings.machine_ip,
        # --- 識別資訊 ---
        "hostname": settings.hostname,
        # --- 運行狀態 ---
        "state": sm["state"],
        "healthy_for_idle": core.disk_monitor.is_healthy_for_idle(),
        "current_command_id": sm["command_id"] or "",
        "current_pid": sm["pid"] if sm["pid"] and sm["pid"] > 0 else "",
        "abnormal_reason": sm["abnormal_reason"] or "",
        # --- 碟片 ---
        "disk_backend": disk.get("backend", ""),
        "nvme_baseline": ",".join(baseline),
        "nvme_current": ",".join(current),
        "nvme_dropped": ",".join(dropped),
        "sysfs_accessible": bool(disk.get("sysfs_accessible", False)),
        # --- 碟片 FW naming ---
        "nvme_fw_model": fw.get("mn", ""),
        "nvme_fw_serial": fw.get("sn", ""),
        "nvme_fw_revision": fw.get("fr", ""),
        # --- 最近事件 ---
        "last_event": event,
        "last_alert_type": alert_type or "",
        "last_message": message or "",
        "last_command_id": last_command_id or "",
        "last_command_status": last_command_status or "",
        "last_updated_at": _utc_now_iso(),
        # --- 環境 ---
        "dev_mode": settings.dev_mode,
    }
