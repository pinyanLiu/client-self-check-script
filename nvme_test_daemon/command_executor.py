"""指令執行與 Crash 偵測。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import psutil

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    command_id: str
    command: str
    return_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    pid: Optional[int] = None
    crashed: bool = False
    crash_reason: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    @property
    def duration_sec(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    def combined_log(self) -> str:
        parts = []
        if self.stdout:
            parts.append(f"[STDOUT]\n{self.stdout}")
        if self.stderr:
            parts.append(f"[STDERR]\n{self.stderr}")
        return "\n".join(parts)


class CommandExecutor:
    """
    非同步執行 Shell 指令，並在背景監控 PID 以偵測 Crash。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        on_crash: Optional[Callable[[CommandResult], None]] = None,
    ) -> None:
        self._settings = settings
        self._on_crash = on_crash

    def _is_log_incomplete(self, result: CommandResult) -> tuple[bool, str]:
        """判斷 Log 是否不完整，作為 Crash 輔助條件。"""
        reasons: list[str] = []

        if result.return_code is not None and result.return_code != 0:
            reasons.append(f"return_code={result.return_code}")

        marker = self._settings.completion_marker.strip()
        if marker:
            combined = result.combined_log()
            if marker not in combined:
                reasons.append(f"缺少結束標記 '{marker}'")

        if reasons:
            return True, "; ".join(reasons)
        return False, ""

    async def _monitor_process(
        self,
        proc: asyncio.subprocess.Process,
        result: CommandResult,
        poll_interval: float,
    ) -> None:
        """
        背景監控：若 PID 消失且 Log 不完整，標記為 crashed。
        正常結束時由 run_command 主流程設定 return_code。
        """
        pid = proc.pid
        result.pid = pid

        while True:
            await asyncio.sleep(poll_interval)

            # 程序已結束，交由 communicate 處理
            if proc.returncode is not None:
                return

            if pid and not psutil.pid_exists(pid):
                incomplete, detail = self._is_log_incomplete(result)
                if incomplete or proc.returncode is None:
                    result.crashed = True
                    result.crash_reason = (
                        f"PID {pid} 異常消失"
                        + (f" ({detail})" if detail else "")
                    )
                    logger.error(
                        "Crash 偵測: command_id=%s %s",
                        result.command_id,
                        result.crash_reason,
                    )
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    if self._on_crash:
                        self._on_crash(result)
                return

    async def run_command(self, command_id: str, command: str) -> CommandResult:
        """執行指令並回傳完整結果。"""
        result = CommandResult(command_id=command_id, command=command)
        logger.info("開始執行指令 [%s]: %s", command_id, command)

        # 使用 shell=True 以支援 n8n 傳入的完整 shell 指令
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
            env=os.environ.copy(),
        )

        monitor_task = asyncio.create_task(
            self._monitor_process(
                proc,
                result,
                self._settings.process_poll_interval_sec,
            )
        )

        try:
            if self._settings.command_timeout_sec > 0:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._settings.command_timeout_sec,
                )
            else:
                stdout_bytes, stderr_bytes = await proc.communicate()
        except asyncio.TimeoutError:
            result.crashed = True
            result.crash_reason = (
                f"指令逾時 ({self._settings.command_timeout_sec}s)"
            )
            proc.kill()
            stdout_bytes, stderr_bytes = await proc.communicate()
        finally:
            await monitor_task

        result.stdout = (stdout_bytes or b"").decode(errors="replace")
        result.stderr = (stderr_bytes or b"").decode(errors="replace")
        result.return_code = proc.returncode
        result.finished_at = time.time()

        # 程序正常結束但仍不符合預期 Log → Crash
        if not result.crashed:
            incomplete, detail = self._is_log_incomplete(result)
            if incomplete:
                result.crashed = True
                result.crash_reason = f"執行結果異常: {detail}"
                logger.error(
                    "Crash 偵測 (結束後): command_id=%s %s",
                    command_id,
                    result.crash_reason,
                )
                if self._on_crash:
                    self._on_crash(result)

        logger.info(
            "指令完成 [%s] rc=%s crashed=%s duration=%.2fs",
            command_id,
            result.return_code,
            result.crashed,
            result.duration_sec or 0,
        )
        return result
