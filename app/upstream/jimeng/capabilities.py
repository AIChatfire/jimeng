#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端**模型能力表**的缓存 —— 把"写死的经验值"换成"可读的服务端数据"。

## 为什么需要这一层

2026-09-18 的教训：本层最初按"用户经验 1–4"把出图张数上界写死成 4，
而服务端实际声明 `high_aes_general_v50`（默认模型）是 **1..8**，只有
`high_aes_general_v50p_large` 才是 1..4 —— 对默认模型直接是错的。

而这张表**零成本可读**：`POST /mweb/v1/get_common_config`（body `{}`）按模型下发
`feats` / `generate_count_options` / `default_generate_count` / `resolution_map`
（比例 → **精确像素**，1k/2k/4k）/ `input_image_limit`，**只读、不出图、不扣积分**。

⇒ 纪律：**能读的就不许猜。** 代码里那份 `COUNT_OPTIONS_BY_MODEL` 只剩"探测失败时的兜底"，
且一旦用到兜底就**必须**在响应里如实标注降级。

## 三条设计约束

1. **失败不许静默**：探测不通时用兜底值，但每条受影响的响应都带 `degradations`。
2. **缓存要有界**：上游随时会调这张表；TTL 到期重取，取不到沿用旧值并留痕
   （旧值比"退回经验值"更接近事实）。
3. **形状不确定就不认**：`get_common_config` 的**响应形状**只在本层用防御式解析，
   认不出的字段一律当"读不到"，**绝不猜一个值**。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .client import DEFAULT_COUNT_OPTIONS, COUNT_OPTIONS_BY_MODEL, JimengClient

log = logging.getLogger(__name__)

#: 能力表缓存 TTL。上游调表频率未知，30min 是保守取值：
#: 代价是"上游刚改、我们最多 30min 后跟上"，收益是每 30min 才多一次只读请求。
DEFAULT_TTL = 1800.0

#: `model_list[]` 里认作"模型 key"的字段名，按优先级试。
#: ⚠️ 这是**防御式**读取：认不出时返回 None（= 读不到），不猜。
_KEY_FIELDS = ("model_req_key", "model_key", "model", "model_name", "key")
_LIST_FIELDS = ("model_list", "models", "model_config_list", "model_configs")


@dataclass
class ModelSpec:
    """一个模型的能力（只放**读到的**字段）。"""

    key: str
    count_options: tuple[int, ...] | None = None
    default_count: int | None = None
    resolution_map: dict[str, Any] | None = None
    input_image_limit: int | None = None
    feats: tuple[str, ...] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Snapshot:
    """一次成功读取的结果。"""

    specs: dict[str, ModelSpec]
    fetched_at: float
    #: 解析期发现的形状异常（如实记录，用于排障；不影响可用性）
    shape_notes: list[str] = field(default_factory=list)


class ModelConfigCache:
    """线程安全的模型能力表缓存。

    单例语义由调用方保证（`Service` 持有一个）。
    """

    def __init__(self, client: JimengClient, *, ttl: float = DEFAULT_TTL) -> None:
        self._client = client
        self._ttl = ttl
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None
        #: 最近一次刷新失败的原因（None = 最近一次成功）
        self.last_error: str | None = None
        #: 累计刷新尝试/失败次数（运维观测用）
        self.refresh_attempts = 0
        self.refresh_failures = 0

    # ------------------------------------------------------------------ 读取

    def snapshot(self, *, force: bool = False) -> Snapshot | None:
        """取当前快照；TTL 到期或 force 时刷新。**失败返回旧值，绝不抛。**"""
        with self._lock:
            fresh = self._snap is not None and (time.time() - self._snap.fetched_at) < self._ttl
            if fresh and not force:
                return self._snap
            try:
                snap = self._fetch()
            except Exception as e:  # noqa: BLE001
                self.refresh_failures += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("读取即梦模型能力表失败，沿用既有快照：%s", self.last_error)
                return self._snap
            self._snap = snap
            self.last_error = None
            return snap

    def _fetch(self) -> Snapshot:
        self.refresh_attempts += 1
        cfg = self._client.common_config()
        specs, notes = parse_common_config(cfg)
        return Snapshot(specs=specs, fetched_at=time.time(), shape_notes=notes)

    # ------------------------------------------------------------------ 查询

    def count_options(self, model: str) -> tuple[int, ...]:
        """该模型声明的张数选项。

        顺序：服务端实时值 → 冻结快照 → **全局下确界**（最严），
        后两者都算降级（调用方会在 `degradations` 里看到）。
        """
        spec = self._spec(model)
        if spec and spec.count_options:
            return spec.count_options
        return COUNT_OPTIONS_BY_MODEL.get(model, DEFAULT_COUNT_OPTIONS)

    def input_image_limit(self, model: str) -> int | None:
        spec = self._spec(model)
        return spec.input_image_limit if spec else None

    def resolution_map(self, model: str) -> dict[str, Any] | None:
        spec = self._spec(model)
        return spec.resolution_map if spec else None

    def feats(self, model: str) -> tuple[str, ...] | None:
        spec = self._spec(model)
        return spec.feats if spec else None

    def known_models(self) -> tuple[str, ...]:
        """服务端宣告的模型 key（升序）。读不到时返回空元组。"""
        snap = self.snapshot()
        if not snap:
            return ()
        return tuple(sorted(snap.specs))

    def degradation_note(self, model: str) -> str | None:
        """本次读取是否降级了？是则返回一句**可执行**的说明，否则 None。

        只在"服务端明明有这张表、我们却没读到这个模型"时说降级 ——
        模型本来就不在表里属于正常（新模型/未登记模型），不该报降级。
        """
        snap = self.snapshot()
        if snap is None:
            reason = self.last_error or "未知原因"
            return (f"未能读取即梦服务端模型能力表（{reason}），"
                    f"本次的张数选项取自代码内冻结快照"
                    f"（{list(COUNT_OPTIONS_BY_MODEL.get(model, DEFAULT_COUNT_OPTIONS))}），"
                    f"可能与服务端当前声明不一致。")
        if snap.specs and model not in snap.specs and COUNT_OPTIONS_BY_MODEL.get(model):
            return (f"服务端能力表里没有模型 {model}（表内有 {len(snap.specs)} 个模型），"
                    f"本次张数选项取自代码内冻结快照。")
        return None

    def stats(self) -> dict:
        snap = self._snap
        return {
            "models": len(snap.specs) if snap else 0,
            "age_s": round(time.time() - snap.fetched_at, 1) if snap else None,
            "refresh_attempts": self.refresh_attempts,
            "refresh_failures": self.refresh_failures,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ 内部

    def _spec(self, model: str) -> ModelSpec | None:
        snap = self.snapshot()
        return snap.specs.get(model) if snap else None


# ---------------------------------------------------------------------------
# 解析（防御式）
# ---------------------------------------------------------------------------


def parse_common_config(cfg: dict) -> tuple[dict[str, ModelSpec], list[str]]:
    """把 `get_common_config` 的响应解析成 `{model_key: ModelSpec}`。

    **认不出的字段一律当读不到**，不猜值、不给默认 —— 猜出来的值会被当真事实用。

    返回 (specs, shape_notes)。`shape_notes` 记录形状异常，便于上游改字段名时排障。
    """
    notes: list[str] = []
    data = cfg.get("data")
    if not isinstance(data, dict):
        if isinstance(data, list):
            # 少数接口把 data 直接给成列表
            return _parse_model_list(data, notes), notes
        notes.append(f"响应里没有 dict 形态的 data（实得 {type(data).__name__}）")
        return {}, notes

    raw_list: list | None = None
    for k in _LIST_FIELDS:
        v = data.get(k)
        if isinstance(v, list):
            raw_list = v
            break
    if raw_list is None:
        notes.append(
            f"data 里找不到模型列表（试过 {list(_LIST_FIELDS)}，实得键 {sorted(data)}）")
        return {}, notes
    return _parse_model_list(raw_list, notes), notes


def _parse_model_list(items: list, notes: list[str]) -> dict[str, ModelSpec]:
    specs: dict[str, ModelSpec] = {}
    skipped = 0
    for it in items:
        if not isinstance(it, dict):
            skipped += 1
            continue
        key = None
        for f in _KEY_FIELDS:
            v = it.get(f)
            if isinstance(v, str) and v.strip():
                key = v.strip()
                break
        if key is None:
            skipped += 1
            continue

        opts = _as_count_options(it.get("generate_count_options"))
        if opts is None:
            opts = _as_count_options(it.get("count_options"))

        rmap = it.get("resolution_map")
        if not isinstance(rmap, dict):
            rmap = None

        feats_raw = it.get("feats")
        feats: tuple[str, ...] | None = None
        if isinstance(feats_raw, dict):
            # feats 可能是 {name: bool} 或 {name: {...}} —— 只保留真值项
            feats = tuple(sorted(k for k, v in feats_raw.items() if v))
        elif isinstance(feats_raw, list):
            feats = tuple(sorted(str(x) for x in feats_raw))

        specs[key] = ModelSpec(
            key=key,
            count_options=opts,
            default_count=_as_int(it.get("default_generate_count")),
            resolution_map=rmap,
            input_image_limit=_as_int(it.get("input_image_limit")),
            feats=feats,
            raw=it,
        )
    if skipped:
        notes.append(f"模型列表里有 {skipped} 项无法解析出模型 key，已跳过")
    if not specs:
        notes.append("模型列表解析结果为空")
    return specs


def _as_count_options(v: Any) -> tuple[int, ...] | None:
    """张数选项必须是**正整数元组**；形状不对返回 None（= 读不到）。"""
    if not isinstance(v, (list, tuple)):
        return None
    out: list[int] = []
    for x in v:
        n = _as_int(x)
        if n is not None and n > 0:
            out.append(n)
    return tuple(sorted(set(out))) if out else None


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


__all__ = ["ModelSpec", "Snapshot", "ModelConfigCache", "parse_common_config",
           "DEFAULT_TTL"]
