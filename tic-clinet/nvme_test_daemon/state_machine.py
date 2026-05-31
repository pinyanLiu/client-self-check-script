"""狀態機模組 — 管理 idle / testing / abnormal 三態轉換。"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class SystemState(str, enum.Enum):
    IDLE = "idle"
    TESTING = "testing"
    ABNORMAL = "abnormal"


@dataclass
class StateContext:
    """狀態附帶的執行中繼資料。"""

    current_command_id: Optional[str] = None
    current_pid: Optional[int] = None
    last_transition_reason: str = ""
    last_transition_at: float = field(default_factory=time.time)
    abnormal_reason: Optional[str] = None


class StateMachine:
    """
    執行緒安全的狀態機。

    合法轉換:
      idle     -> testing   (接受並開始指令)
      testing  -> idle      (指令正常完成)
      *        -> abnormal  (掉碟 / Crash)
      abnormal -> idle      (僅在碟片恢復且 allow_reset 時，由外部觸發)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = SystemState.IDLE
        self._ctx = StateContext()

    @property
    def state(self) -> SystemState:
        with self._lock:
            return self._state

    @property
    def context(self) -> StateContext:
        with self._lock:
            return StateContext(
                current_command_id=self._ctx.current_command_id,
                current_pid=self._ctx.current_pid,
                last_transition_reason=self._ctx.last_transition_reason,
                last_transition_at=self._ctx.last_transition_at,
                abnormal_reason=self._ctx.abnormal_reason,
            )

    def _transition(
        self,
        new_state: SystemState,
        reason: str,
        *,
        command_id: Optional[str] = None,
        pid: Optional[int] = None,
        abnormal_reason: Optional[str] = None,
    ) -> bool:
        with self._lock:
            old = self._state
            if old == new_state and old != SystemState.ABNORMAL:
                return False

            self._state = new_state
            self._ctx.last_transition_reason = reason
            self._ctx.last_transition_at = time.time()

            if new_state == SystemState.TESTING:
                self._ctx.current_command_id = command_id
                self._ctx.current_pid = pid
                self._ctx.abnormal_reason = None
            elif new_state == SystemState.IDLE:
                self._ctx.current_command_id = None
                self._ctx.current_pid = None
                self._ctx.abnormal_reason = None
            elif new_state == SystemState.ABNORMAL:
                self._ctx.abnormal_reason = abnormal_reason or reason

            return True

    def can_accept_command(self) -> bool:
        with self._lock:
            return self._state == SystemState.IDLE

    def start_testing(self, command_id: str, pid: int) -> bool:
        with self._lock:
            if self._state != SystemState.IDLE:
                return False
            self._transition(
                SystemState.TESTING,
                f"開始執行指令 {command_id}",
                command_id=command_id,
                pid=pid,
            )
            return True

    def update_pid(self, command_id: str, pid: int) -> None:
        with self._lock:
            if (
                self._state == SystemState.TESTING
                and self._ctx.current_command_id == command_id
            ):
                self._ctx.current_pid = pid

    def finish_testing(self, command_id: str) -> bool:
        with self._lock:
            if self._state != SystemState.TESTING:
                return False
            if self._ctx.current_command_id != command_id:
                return False
            self._transition(SystemState.IDLE, f"指令 {command_id} 正常完成")
            return True

    def enter_abnormal(self, reason: str) -> bool:
        with self._lock:
            if self._state == SystemState.ABNORMAL:
                return False
            self._transition(
                SystemState.ABNORMAL,
                reason,
                abnormal_reason=reason,
            )
            return True

    def reset_from_abnormal(self, reason: str = "手動復原") -> bool:
        with self._lock:
            if self._state != SystemState.ABNORMAL:
                return False
            self._transition(SystemState.IDLE, reason)
            return True

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "command_id": self._ctx.current_command_id,
                "pid": self._ctx.current_pid,
                "last_transition_reason": self._ctx.last_transition_reason,
                "last_transition_at": self._ctx.last_transition_at,
                "abnormal_reason": self._ctx.abnormal_reason,
            }
