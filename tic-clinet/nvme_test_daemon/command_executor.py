"""指令執行與 Crash 偵測。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import psutil

from .config import Settings
from .debug_log import (
    collect_logs_since,
    snapshot_log_dir,
    validate_debug_logs,
)
from .workspace import find_latest_pps2_folder, resolve_debug_log_dir

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
    work_dir: Optional[str] = None
    debug_log_dir: Optional[str] = None
    log_validation: Optional[Dict[str, Any]] = None

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
        if self.log_validation and self.log_validation.get("files"):
            debug_parts = []
            for f in self.log_validation["files"]:
                debug_parts.append(
                    f"[{f.get('name', 'log')}]\n{f.get('content', '')}"
                )
            if debug_parts:
                parts.append("[DEBUG_LOG]\n" + "\n".join(debug_parts))
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

    def _resolve_work_dir(self) -> Path:
        base = Path(self._settings.nvme_work_base)
        folder = find_latest_pps2_folder(base, self._settings.pps2_folder_prefix)
        if folder is None:
            raise FileNotFoundError(
                f"找不到 {self._settings.pps2_folder_prefix} 工作資料夾: {base}"
            )
        return folder

    def _collect_debug_logs(self, result: CommandResult, log_snapshot: dict) -> None:
        if not result.work_dir:
            return
        log_dir = resolve_debug_log_dir(
            Path(result.work_dir), self._settings.debug_log_rel_path
        )
        result.debug_log_dir = str(log_dir)
        entries = collect_logs_since(
            log_dir,
            log_snapshot,
            started_at=result.started_at,
            max_file_bytes=self._settings.debug_log_max_bytes,
        )
        validation = validate_debug_logs(
            entries,
            completion_marker=self._settings.completion_marker,
            require_new_logs=self._settings.require_debug_logs,
        )
        result.log_validation = validation.to_dict()

    def _is_log_incomplete(self, result: CommandResult) -> tuple[bool, str]:
        """判斷 Log 是否不完整，作為 Crash 輔助條件。"""
        reasons: list[str] = []

        if result.return_code is not None and result.return_code != 0:
            reasons.append(f"return_code={result.return_code}")

        if result.log_validation and not result.log_validation.get("ok", True):
            issues = result.log_validation.get("issues") or []
            reasons.extend(issues)

        marker = self._settings.completion_marker.strip()
        # 若未設定 REQUIRE_DEBUG_LOGS，仍可在 stdout/stderr 檢查標記
        if marker and not (result.log_validation and result.log_validation.get("files")):
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
        try:
            work_dir = self._resolve_work_dir()
        except FileNotFoundError as exc:
            result.crashed = True
            result.crash_reason = str(exc)
            result.finished_at = time.time()
            logger.error("無法解析工作目錄 [%s]: %s", command_id, exc)
            if self._on_crash:
                self._on_crash(result)
            return result

        result.work_dir = str(work_dir)
        log_dir = resolve_debug_log_dir(work_dir, self._settings.debug_log_rel_path)
        log_snapshot = snapshot_log_dir(log_dir)

        logger.info(
            "開始執行指令 [%s] cwd=%s: %s",
            command_id,
            work_dir,
            command,
        )

        # 使用 shell=True 以支援 n8n 傳入的完整 shell 指令 (例如 ./NVME para1 para2)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
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

        self._collect_debug_logs(result, log_snapshot)

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
