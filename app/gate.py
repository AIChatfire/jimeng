#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下游节奏闸门（**同步实现**，与协调器的线程模型一致）。

为什么必须有这一层：上游的槽位是「占到底」的。即梦、文心、qwen 都有按节奏触发的
风控/限流（即梦的 `1018/1019/1021`），直连转发会在第 N 个请求就开始报错。

三类约束（都从 settings 读）：
  L1 并发上限   —— 由 `TaskStore.count_active() < JM_CONCURRENCY` 实现
                   （**不在这里**：并发数放在库里数，重启后依然正确；见 coordinator.py）
  L2 节奏器     两次「建任务」的最小间隔 + 60s 滑动窗口上限（防瞬时冲高）
  L3 熔断冷却   命中风控后进入冷却，冷却期内**不发任何请求**

L3 刻意选择「快速失败」而不是「排队等待」：风控是风险评分型，持续施压会延长标记，
把请求挂在队列里等冷却结束等于换个姿势施压。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from .errors import RiskControlError, UpstreamRateLimitError


@dataclass
class GateStats:
    admitted: int = 0
    rejected: int = 0
    risk_hits: int = 0
    #: 额度耗尽（与风控命中**刻意分开计数**：前者与我们的行为无关，后者是我们触发的）
    quota_holds: int = 0
    waits: int = 0
    total_wait_s: float = 0.0
    last_at: float = 0.0
    cooling_for: float = 0.0

    def as_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "rejected": self.rejected,
            "risk_hits": self.risk_hits,
            "quota_holds": self.quota_holds,
            "waits": self.waits,
            "total_wait_s": round(self.total_wait_s, 2),
            "cooling_for": round(self.cooling_for, 1),
        }


class UpstreamGate:
    """按上游分桶的节奏闸门。线程安全。"""

    def __init__(self, name: str, *, min_interval: float = 0.0,
                 per_minute: int = 0, cooldown: float = 0.0,
                 max_wait: float = 120.0) -> None:
        self.name = name
        self.min_interval = max(0.0, min_interval)
        self.per_minute = max(0, per_minute)
        self.cooldown = max(0.0, cooldown)
        self.max_wait = max(0.0, max_wait)

        self._lock = threading.Lock()
        self._window: deque[float] = deque()
        self._last_at = 0.0
        self._cooldown_until = 0.0
        self._stats = GateStats()

    # ---------------------------------------------------------------- 内部

    def _cooling_for(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def _pace(self) -> float:
        """节奏器：返回实际等待秒数。**记账只在真正放行时做一次**。"""
        waited = 0.0
        while True:
            with self._lock:
                now = time.time()
                while self._window and now - self._window[0] > 60.0:
                    self._window.popleft()

                need = 0.0
                if self._last_at and self.min_interval:
                    need = max(need, self.min_interval - (now - self._last_at))
                if self.per_minute and len(self._window) >= self.per_minute:
                    need = max(need, 60.0 - (now - self._window[0]) + 0.05)

                if need <= 0:
                    self._last_at = now
                    self._window.append(now)
                    if waited > 0:
                        self._stats.waits += 1
                        self._stats.total_wait_s += waited
                    return waited

                if waited + need > self.max_wait:
                    self._stats.rejected += 1
                    raise UpstreamRateLimitError(
                        f"{self.name} 上游节奏已满，请稍后重试",
                        upstream=self.name,
                        retry_after=need,
                        wait_so_far=round(waited, 1),
                        min_interval=self.min_interval,
                        per_minute=self.per_minute,
                    )
            step = min(need, 2.0)
            time.sleep(step)
            waited += step

    # ---------------------------------------------------------------- 生命周期

    def acquire(self) -> float:
        """取得一次「发请求」的许可；返回等待秒数。失败抛 AdapterError 子类。"""
        cooling = self._cooling_for()
        if cooling > 0:
            with self._lock:
                self._stats.rejected += 1
            raise RiskControlError(
                f"{self.name} 上游处于风控冷却中，{cooling:.0f}s 内不会接受请求；"
                f"请勿重试（持续施压会延长风控标记），必要时人工过一次验证",
                upstream=self.name,
                retry_after=cooling,
            )
        waited = self._pace()
        with self._lock:
            self._stats.admitted += 1
        return waited

    def mark_risk_hit(self) -> None:
        """命中上游风控 —— 进入冷却期。"""
        with self._lock:
            self._stats.risk_hits += 1
            if self.cooldown > 0:
                self._cooldown_until = time.time() + self.cooldown

    def mark_quota_exhausted(self, seconds: float) -> None:
        """额度耗尽 → 进入**静默期**（`seconds` 秒内不再发起请求）。

        与 `mark_risk_hit()` 刻意分开，两件事性质不同：
          · 风控命中 = **我们的节奏**触发了上游保护机制，计 `risk_hits`；
          · 额度耗尽 = 与我们的行为无关，只是一个**不会因重试而变好的计数器**，
            计 `quota_holds`。

        时长刻意**有界**：不做"锁到明天"—— 上游重置时刻未必是本地零点，
        静默期到点自然重试，能自愈。
        """
        if seconds <= 0:
            return
        with self._lock:
            self._stats.quota_holds += 1
            self._cooldown_until = max(self._cooldown_until, time.time() + seconds)

    def reset_cooldown(self) -> None:
        with self._lock:
            self._cooldown_until = 0.0

    def stats(self) -> dict:
        with self._lock:
            self._stats.cooling_for = self._cooling_for()
            self._stats.last_at = self._last_at
            return {"name": self.name,
                    "min_interval": self.min_interval,
                    "per_minute": self.per_minute,
                    **self._stats.as_dict()}


def build_gate(settings) -> UpstreamGate:
    """按配置构造即梦闸门。"""
    return UpstreamGate(
        "jimeng",
        min_interval=settings.jm_min_interval,
        per_minute=settings.jm_per_minute,
        cooldown=settings.jm_cooldown,
        max_wait=settings.jm_max_wait,
    )


__all__ = ["UpstreamGate", "GateStats", "build_gate"]
