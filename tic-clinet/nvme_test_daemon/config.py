"""設定載入模組 — 從環境變數讀取 n8n 與監控相關參數。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Daemon 執行時設定。"""

    # n8n Webhook URLs
    n8n_log_webhook_url: str = field(
        default_factory=lambda: os.getenv("N8N_LOG_WEBHOOK_URL", "")
    )
    n8n_heartbeat_webhook_url: str = field(
        default_factory=lambda: os.getenv("N8N_HEARTBEAT_WEBHOOK_URL", "")
    )
    n8n_alert_webhook_url: str = field(
        default_factory=lambda: os.getenv("N8N_ALERT_WEBHOOK_URL", "")
    )

    # HTTP API
    api_host: str = field(default_factory=lambda: os.getenv("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: int(os.getenv("API_PORT", "8080")))

    # 監控間隔 (秒)
    heartbeat_interval_sec: int = field(
        default_factory=lambda: int(os.getenv("HEARTBEAT_INTERVAL_SEC", "300"))
    )
    disk_check_interval_sec: int = field(
        default_factory=lambda: int(os.getenv("DISK_CHECK_INTERVAL_SEC", "10"))
    )
    process_poll_interval_sec: float = field(
        default_factory=lambda: float(os.getenv("PROCESS_POLL_INTERVAL_SEC", "1.0"))
    )

    # Crash 判定：Log 中必須出現的結束標記 (可選)
    completion_marker: str = field(
        default_factory=lambda: os.getenv("COMPLETION_MARKER", "")
    )

    # 指定要監控的 NVMe 控制器名稱，例如 nvme0,nvme1；留空則監控所有
    nvme_watch_list: List[str] = field(default_factory=list)

    # n8n HTTP 請求逾時 (秒)
    http_timeout_sec: float = field(
        default_factory=lambda: float(os.getenv("HTTP_TIMEOUT_SEC", "30"))
    )

    # 指令執行逾時 (秒)，0 表示不限制
    command_timeout_sec: int = field(
        default_factory=lambda: int(os.getenv("COMMAND_TIMEOUT_SEC", "0"))
    )

    # 是否允許透過 API 從 abnormal 手動復原
    allow_manual_reset: bool = field(
        default_factory=lambda: _env_bool("ALLOW_MANUAL_RESET", True)
    )

    # Mac 本機開發：模擬 NVMe，不讀 sysfs
    dev_mode: bool = field(default_factory=lambda: _env_bool("DEV_MODE", False))

    # DEV_MODE 下模擬的 NVMe 控制器 (逗號分隔)
    mock_nvme_devices: List[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        watch_raw = os.getenv("NVME_WATCH_LIST", "").strip()
        watch_list = [x.strip() for x in watch_raw.split(",") if x.strip()]
        mock_raw = os.getenv("MOCK_NVME_DEVICES", "nvme0").strip()
        mock_devices = [x.strip() for x in mock_raw.split(",") if x.strip()]
        base = cls()
        object.__setattr__(base, "nvme_watch_list", watch_list)
        object.__setattr__(base, "mock_nvme_devices", mock_devices)
        return base

    def validate(self) -> None:
        missing = []
        if not self.n8n_log_webhook_url:
            missing.append("N8N_LOG_WEBHOOK_URL")
        if not self.n8n_heartbeat_webhook_url:
            missing.append("N8N_HEARTBEAT_WEBHOOK_URL")
        if not self.n8n_alert_webhook_url:
            missing.append("N8N_ALERT_WEBHOOK_URL")
        if missing:
            raise ValueError(
                f"缺少必要環境變數: {', '.join(missing)}"
            )
