"""
NVMe 測試機 Daemon — 主控模組。

整合 FastAPI、狀態機、掉碟偵測、指令執行與 n8n 通訊。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .command_executor import CommandExecutor, CommandResult
from .config import Settings
from .disk_monitor import MockDiskMonitor, create_disk_monitor
from .n8n_client import N8nClient
from .state_machine import StateMachine, SystemState
from .status_report import build_table_row

logger = logging.getLogger(__name__)


class CommandRequest(BaseModel):
    command: str = Field(..., description="要執行的 Shell 指令")
    command_id: Optional[str] = Field(
        default=None, description="可選的指令 ID，未提供則自動產生"
    )


class ResetRequest(BaseModel):
    reason: str = Field(default="手動復原", description="復原原因")


class SimulateDropRequest(BaseModel):
    device: str = Field(..., description="要模擬消失的 NVMe 控制器，例如 nvme0")


class SimulateRestoreRequest(BaseModel):
    device: str | None = Field(
        default=None, description="要還原的裝置；留空則還原全部 baseline"
    )


class DaemonCore:
    """Daemon 核心邏輯，供 API 與排程共用。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_machine = StateMachine()
        self.disk_monitor = create_disk_monitor(
            dev_mode=settings.dev_mode,
            mock_devices=settings.mock_nvme_devices,
            watch_list=settings.nvme_watch_list,
        )
        self.n8n = N8nClient(settings)
        self.executor = CommandExecutor(settings, on_crash=self._handle_crash_sync)
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._abnormal_lock = asyncio.Lock()

    def _handle_crash_sync(self, result: CommandResult) -> None:
        """CommandExecutor 同步 callback — 排程 async 異常處理。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.handle_abnormal("crash", result.crash_reason or "指令 Crash", result))
        except RuntimeError:
            logger.error("無法排程 abnormal 處理 (無 event loop)")

    async def notify_status(
        self,
        event: str,
        *,
        alert_type: Optional[str] = None,
        message: Optional[str] = None,
        last_command_id: Optional[str] = None,
        last_command_status: Optional[str] = None,
    ) -> None:
        """推送扁平狀態至 n8n，供 Data Table Upsert（每台機器一列）。"""
        row = build_table_row(
            self,
            event=event,
            alert_type=alert_type,
            message=message,
            last_command_id=last_command_id,
            last_command_status=last_command_status,
        )
        await self.n8n.send_status(row)

    async def startup(self) -> None:
        baseline = self.disk_monitor.establish_baseline()
        if not baseline and self.settings.nvme_watch_list:
            msg = f"找不到指定的 NVMe 裝置: {self.settings.nvme_watch_list}"
            logger.warning(msg)
            await self.handle_abnormal("disk_drop", msg)

        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self.heartbeat_job,
            "interval",
            seconds=self.settings.heartbeat_interval_sec,
            id="heartbeat",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self.disk_check_job,
            "interval",
            seconds=self.settings.disk_check_interval_sec,
            id="disk_check",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("Daemon 啟動完成，狀態=%s", self.state_machine.state.value)

        await self.n8n.send_heartbeat(await self.build_health_payload())
        await self.notify_status("startup")

    async def shutdown(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
        await self.n8n.close()

    async def build_health_payload(self) -> Dict[str, Any]:
        disk = self.disk_monitor.health_snapshot()
        sm = self.state_machine.snapshot()
        payload = {
            "state": sm["state"],
            "command_id": sm["command_id"],
            "pid": sm["pid"],
            "abnormal_reason": sm["abnormal_reason"],
            "disk": disk,
            "healthy_for_idle": self.disk_monitor.is_healthy_for_idle(),
        }
        if self.settings.dev_mode:
            payload["dev_mode"] = True
        return payload

    async def heartbeat_job(self) -> None:
        try:
            payload = await self.build_health_payload()
            await self.n8n.send_heartbeat(payload)
            await self.notify_status("heartbeat")
        except Exception:
            logger.exception("Heartbeat 任務失敗")

    async def disk_check_job(self) -> None:
        try:
            dropped = self.disk_monitor.check_for_drop()
            if dropped:
                await self.handle_abnormal(
                    "disk_drop",
                    f"NVMe 掉碟: {', '.join(dropped)}",
                )
        except Exception:
            logger.exception("掉碟檢查失敗")

    async def handle_abnormal(
        self,
        alert_type: str,
        message: str,
        command_result: Optional[CommandResult] = None,
    ) -> None:
        async with self._abnormal_lock:
            changed = self.state_machine.enter_abnormal(message)
            if not changed and self.state_machine.state == SystemState.ABNORMAL:
                return

            table_row = build_table_row(
                self,
                event="abnormal",
                alert_type=alert_type,
                message=message,
                last_command_id=command_result.command_id if command_result else None,
                last_command_status="crashed" if command_result and command_result.crashed else "",
            )
            details: Dict[str, Any] = {
                "state_snapshot": self.state_machine.snapshot(),
                "disk": self.disk_monitor.health_snapshot(),
                "table_row": table_row,
            }
            if command_result:
                details["command"] = {
                    "command_id": command_result.command_id,
                    "command": command_result.command,
                    "return_code": command_result.return_code,
                    "crashed": command_result.crashed,
                    "log": command_result.combined_log(),
                }

            await self.n8n.send_alert(
                alert_type=alert_type,
                message=message,
                details=details,
            )
            await self.n8n.send_status(table_row)

            if command_result:
                await self.n8n.send_log(
                    command_id=command_result.command_id,
                    status="abnormal",
                    return_code=command_result.return_code,
                    stdout=command_result.stdout,
                    stderr=command_result.stderr,
                    crashed=True,
                    extra={"alert_type": alert_type, "message": message},
                )

    async def execute_command(self, command_id: str, command: str) -> None:
        """背景執行指令的完整生命週期。"""
        # 佔位 PID；實際 PID 在 subprocess 建立後更新
        if not self.state_machine.start_testing(command_id, pid=-1):
            logger.warning("無法進入 testing 狀態 (可能已被 abnormal 佔用)")
            return

        await self.notify_status(
            "command_start",
            last_command_id=command_id,
            last_command_status="running",
        )

        result = await self.executor.run_command(command_id, command)

        if result.pid and result.pid > 0:
            self.state_machine.update_pid(command_id, result.pid)

        if result.crashed:
            await self.handle_abnormal(
                "crash",
                result.crash_reason or "指令 Crash",
                result,
            )
            return

        # 掉碟可能在執行期間發生，finish 前再確認
        if self.disk_monitor.check_for_drop():
            await self.handle_abnormal(
                "disk_drop",
                "指令完成前偵測到掉碟",
                result,
            )
            return

        if self.state_machine.state == SystemState.ABNORMAL:
            return

        self.state_machine.finish_testing(command_id)

        await self.n8n.send_log(
            command_id=command_id,
            status="success",
            return_code=result.return_code,
            stdout=result.stdout,
            stderr=result.stderr,
            crashed=False,
            extra={"duration_sec": result.duration_sec},
        )
        await self.notify_status(
            "command_success",
            last_command_id=command_id,
            last_command_status="success",
        )

    async def try_reset(self, reason: str) -> bool:
        if not self.settings.allow_manual_reset:
            return False
        if self.state_machine.state != SystemState.ABNORMAL:
            return False
        if not self.disk_monitor.refresh_baseline_if_recovered():
            return False
        ok = self.state_machine.reset_from_abnormal(reason)
        if ok:
            await self.notify_status("reset", message=reason)
        return ok


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.load()
    core = DaemonCore(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.validate()
        await core.startup()
        yield
        await core.shutdown()

    app = FastAPI(
        title="NVMe Test Daemon",
        description="n8n 雙向通訊的 NVMe 測試機常駐服務",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return await core.build_health_payload()

    @app.get("/status")
    async def status() -> Dict[str, Any]:
        return core.state_machine.snapshot()

    @app.post("/command")
    async def receive_command(req: CommandRequest) -> Dict[str, Any]:
        command_id = req.command_id or str(uuid.uuid4())

        if core.state_machine.state == SystemState.ABNORMAL:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "abnormal",
                    "message": core.state_machine.context.abnormal_reason,
                },
            )

        if not core.disk_monitor.is_healthy_for_idle():
            raise HTTPException(
                status_code=503,
                detail={"error": "disk_unhealthy", "message": "NVMe 裝置不可用"},
            )

        if not core.state_machine.can_accept_command():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "busy",
                    "message": "測試機正在執行其他指令",
                    "current_command_id": core.state_machine.context.current_command_id,
                },
            )

        asyncio.create_task(core.execute_command(command_id, req.command))
        asyncio.create_task(
            core.notify_status(
                "command_accepted",
                last_command_id=command_id,
                last_command_status="accepted",
            )
        )

        return {
            "accepted": True,
            "command_id": command_id,
            "state": "testing",
        }

    @app.post("/reset")
    async def reset_abnormal(req: ResetRequest) -> Dict[str, Any]:
        ok = await core.try_reset(req.reason)
        if not ok:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "reset_failed",
                    "message": "目前無法復原 (非 abnormal、碟片未恢復或禁止手動復原)",
                    "state": core.state_machine.state.value,
                },
            )
        return {"reset": True, "state": core.state_machine.state.value}

    if settings.dev_mode:

        @app.post("/dev/simulate/drop")
        async def dev_simulate_drop(req: SimulateDropRequest) -> Dict[str, Any]:
            monitor = core.disk_monitor
            if not isinstance(monitor, MockDiskMonitor):
                raise HTTPException(status_code=400, detail="非 Mock 模式")
            ok = monitor.simulate_drop(req.device)
            if not ok:
                raise HTTPException(
                    status_code=404,
                    detail=f"找不到模擬裝置: {req.device}",
                )
            dropped = monitor.check_for_drop()
            if dropped:
                await core.handle_abnormal(
                    "disk_drop",
                    f"[Mock] NVMe 掉碟: {', '.join(dropped)}",
                )
            return {"simulated_drop": req.device, "state": core.state_machine.state.value}

        @app.post("/dev/simulate/restore")
        async def dev_simulate_restore(
            req: SimulateRestoreRequest,
        ) -> Dict[str, Any]:
            monitor = core.disk_monitor
            if not isinstance(monitor, MockDiskMonitor):
                raise HTTPException(status_code=400, detail="非 Mock 模式")
            restored = monitor.simulate_restore(req.device)
            return {
                "restored": restored,
                "disk": monitor.health_snapshot(),
                "state": core.state_machine.state.value,
            }

    return app
