#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签名算法门禁：11 条真实抓包向量 + 两个最容易写错的点。

上游改版改掉 `9e2c`/`11ac`、或改掉"末 7 字符"的切片长度时，这里必须**立刻红**，
而不是等到线上 403/`1014` 才发现。
"""
from __future__ import annotations

from app.upstream.jimeng.sign import (
    SELF_TEST_VECTORS,
    sign_headers,
    sign_payload,
    verify_vectors,
)


def test_all_capture_vectors_pass():
    assert verify_vectors() == [], "签名算法与真实抓包不一致 —— 上游改版了？"


def test_vectors_are_not_trivially_few():
    """向量要覆盖**跨端点**，否则只是"碰巧对上一条"。"""
    paths = {p for p, _ts, _sig in SELF_TEST_VECTORS}
    assert len(paths) >= 3, f"向量只覆盖了 {paths}，不足以钉住算法"
    assert len(SELF_TEST_VECTORS) >= 11


def test_tail_slice_is_last_7_chars_of_pathname():
    """`o.slice(-7)` 取的是 **pathname 末 7 字符**，不是能力名、不是末 7 个路径段。

    `/mweb/v1/aigc_draft/generate` → `enerate`（注意没有 `g`）。
    写错一个字符就是 100% 不通，而这是个极隐蔽的错。
    """
    payload = sign_payload("/mweb/v1/aigc_draft/generate", ts=1789742348)
    assert "|enerate|" in payload
    assert "generate|" not in payload.replace("|enerate|", "|")


def test_device_time_comes_from_the_same_ts_as_sign():
    """`sign` 与 `device-time` 是**同一个原子的两半**，拆开算就失败。"""
    h = sign_headers("/mweb/v1/get_history_by_ids", ts=1789742352)
    assert h["device-time"] == "1789742352"
    # 换一秒 ⇒ 签名必须变
    h2 = sign_headers("/mweb/v1/get_history_by_ids", ts=1789742353)
    assert h2["sign"] != h["sign"]


def test_signature_ignores_query_string():
    """签名只看 pathname —— 带 query 与不带必须算出同一个值。"""
    a = sign_headers("/mweb/v1/aigc_draft/generate", ts=1789742348)
    b = sign_headers("/mweb/v1/aigc_draft/generate?aid=513695&region=cn",
                     ts=1789742348)
    assert a["sign"] == b["sign"]


def test_headers_are_the_full_six():
    h = sign_headers("/mweb/v1/aigc_draft/generate", ts=1789742348)
    assert set(h) == {"sign", "sign-ver", "device-time", "pf", "appvr", "tdid"}
    assert h["sign-ver"] == "1"
