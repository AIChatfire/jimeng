#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端能力表（`get_common_config`）解析门禁。

核心纪律：**认不出的字段一律当"读不到"，绝不猜一个值。**
猜出来的值会被当真事实用 —— 那正是"用经验值替代可读的服务端数据"的翻版。
"""
from __future__ import annotations

from app.upstream.jimeng.capabilities import (
    ModelConfigCache,
    parse_common_config,
)

#: 真实形状的近似样本（字段名取自 `probe_model_config.py` 的实测输出）。
SAMPLE = {
    "ret": "0",
    "data": {
        "model_list": [
            {
                "model_req_key": "high_aes_general_v50",
                "feats": {"generate": True, "byte_edit": True, "pro_hd": True},
                "generate_count_options": [1, 2, 3, 4, 5, 6, 7, 8],
                "default_generate_count": 4,
                "resolution_map": {"1:1": {"1k": [1024, 1024], "2k": [2048, 2048]}},
                "input_image_limit": 1,
            },
            {
                "model_req_key": "high_aes_general_v50p_large",
                "generate_count_options": [1, 2, 3, 4],
                "default_generate_count": 2,
            },
        ]
    },
}


def test_parses_model_list_with_real_field_names():
    specs, notes = parse_common_config(SAMPLE)
    assert notes == []
    assert set(specs) == {"high_aes_general_v50", "high_aes_general_v50p_large"}

    v50 = specs["high_aes_general_v50"]
    assert v50.count_options == (1, 2, 3, 4, 5, 6, 7, 8)
    assert v50.default_count == 4
    assert v50.input_image_limit == 1
    assert v50.resolution_map == {"1:1": {"1k": [1024, 1024], "2k": [2048, 2048]}}
    assert v50.feats == ("byte_edit", "generate", "pro_hd")

    # 🔴 这一条是那张表的**全部价值**：默认模型是 1..8，不是"经验值 1..4"
    assert max(v50.count_options) == 8
    assert max(specs["high_aes_general_v50p_large"].count_options) == 4


def test_string_numbers_are_normalised_but_not_invented():
    """上游把数字写成字符串是常态；要能读，但**读不出就不给值**。"""
    specs, _ = parse_common_config({
        "data": {"model_list": [
            {"model_req_key": "m1", "generate_count_options": ["1", "2"]},
            {"model_req_key": "m2", "generate_count_options": "1,2,3"},
        ]}})
    assert specs["m1"].count_options == (1, 2)
    assert specs["m2"].count_options is None, "逗号串不是合法形状 ⇒ 当读不到"


def test_unknown_shape_degrades_loudly_instead_of_guessing():
    specs, notes = parse_common_config({"ret": "0", "data": {"something_else": 1}})
    assert specs == {}
    assert notes and "找不到模型列表" in notes[0]


def test_non_dict_data_is_reported_not_guessed():
    specs, notes = parse_common_config({"data": "oops"})
    assert specs == {}
    assert "没有 dict 形态的 data" in notes[0]


def test_entries_without_a_model_key_are_skipped_and_counted():
    specs, notes = parse_common_config({"data": {"model_list": [
        {"foo": "bar"}, {"model_req_key": "ok", "generate_count_options": [1]}]}})
    assert set(specs) == {"ok"}
    assert any("无法解析出模型 key" in n for n in notes)


def test_boolean_is_not_accepted_as_a_number():
    """`True` 是 1 —— 但它不是"张数选项 1"，是形状不对。"""
    specs, _ = parse_common_config({"data": {"model_list": [
        {"model_req_key": "m", "generate_count_options": [True, 2]}]}})
    assert specs["m"].count_options == (2,)


# ---------------------------------------------------------------------------
# 缓存行为
# ---------------------------------------------------------------------------


class _StubClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    def common_config(self, **_kw):
        self.calls += 1
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def test_cache_reads_once_then_serves_from_memory():
    c = _StubClient(SAMPLE)
    cache = ModelConfigCache(c, ttl=999)
    assert cache.count_options("high_aes_general_v50") == (1, 2, 3, 4, 5, 6, 7, 8)
    assert cache.count_options("high_aes_general_v50") == (1, 2, 3, 4, 5, 6, 7, 8)
    assert c.calls == 1, "TTL 内不应重复打上游"


def test_probe_failure_falls_back_to_frozen_snapshot_and_reports_degradation():
    """探测失败 ⇒ 用冻结快照，**但必须明说**（静默降级 = 让人按 A 的预期为 B 付费）。"""
    c = _StubClient(RuntimeError("boom"))
    cache = ModelConfigCache(c, ttl=0)
    opts = cache.count_options("high_aes_general_v50")
    assert opts == (1, 2, 3, 4, 5, 6, 7, 8), "冻结快照里 v50 就是 1..8"
    note = cache.degradation_note("high_aes_general_v50")
    assert note and "未能读取" in note
    assert cache.stats()["last_error"]


def test_unknown_model_is_not_reported_as_degradation():
    """模型本来就不在服务端表里属正常（新模型），不该报降级 —— 那会让告警失去意义。"""
    c = _StubClient(SAMPLE)
    cache = ModelConfigCache(c, ttl=999)
    cache.snapshot()
    from app.upstream.jimeng.client import DEFAULT_COUNT_OPTIONS

    assert cache.count_options("brand_new_model") == DEFAULT_COUNT_OPTIONS
    assert cache.degradation_note("brand_new_model") is None
