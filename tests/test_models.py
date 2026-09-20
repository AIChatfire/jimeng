#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力解析门禁：默认能力只在**无歧义**时给；能力与请求形态必须自洽。"""
from __future__ import annotations

import pytest

from app.errors import InvalidParameterError
from app.models import (
    CAPABILITIES,
    DELIBERATE_ABSENCES,
    catalog,
    is_placeholder,
    resolve,
)


def test_no_image_defaults_to_t2i_unambiguously():
    cap, model = resolve(None, has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model, "文生图必须带一个上游模型 key"


def test_with_image_refuses_to_guess():
    """带图时四个能力都能接 ⇒ **必须报错**，不能替调用方挑。

    它们的单价差可达 10 倍（hd 9 / outpaint 28 / i2i 40 / pro-hd 91），
    挑错等于替调用方做了他没做的决定。
    """
    with pytest.raises(InvalidParameterError) as e:
        resolve(None, has_image=True)
    msg = e.value.message
    for api_id in ("jimeng-i2i", "jimeng-hd", "jimeng-pro-hd", "jimeng-outpaint"):
        assert api_id in msg, f"错误信息里必须列出候选，缺 {api_id}"
    assert "9" in msg and "91" in msg, "应给出单价量级，让人知道选择的代价"


@pytest.mark.parametrize("written,expect", [
    ("jimeng-t2i", "jimeng-t2i"),
    ("jimeng-hd", "jimeng-hd"),
    ("t2i", "jimeng-t2i"),
    ("hd", "jimeng-hd"),
    ("pro-hd", "jimeng-pro-hd"),
    ("outpaint", "jimeng-outpaint"),
    ("i2i", "jimeng-i2i"),
    ("即梦", "jimeng-t2i"),
    ("超清", "jimeng-hd"),
    ("智能超清", "jimeng-pro-hd"),
    ("扩图", "jimeng-outpaint"),
    ("JIMENG-HD", "jimeng-hd"),
])
def test_aliases_and_bare_names_resolve(written, expect):
    has_image = expect != "jimeng-t2i"
    cap, _ = resolve(written, has_image=has_image)
    assert cap.api_id == expect


def test_upstream_model_key_maps_to_t2i_with_that_model():
    cap, model = resolve("high_aes_general_v43", has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model == "high_aes_general_v43"


def test_unknown_model_is_rejected_with_hint():
    with pytest.raises(InvalidParameterError) as e:
        resolve("gpt-4o", has_image=False)
    assert "未知 model" in e.value.message
    assert "jimeng-t2i" in e.value.message, "报错要给出可用清单"


@pytest.mark.parametrize("placeholder", ["", "auto", "default", "dall-e-3",
                                         "gpt-image-1", "seedream-4-0",
                                         "doubao-seedream-5-0-pro-260628"])
def test_placeholders_count_as_unspecified(placeholder):
    """第三方 SDK 硬编码的占位名**不代表调用意图** ⇒ 走默认推导而不是报"未知模型"。"""
    assert is_placeholder(placeholder)
    cap, _ = resolve(placeholder, has_image=False)
    assert cap.api_id == "jimeng-t2i"


def test_image_required_capability_without_image_is_400_before_upstream():
    with pytest.raises(InvalidParameterError) as e:
        resolve("jimeng-hd", has_image=False)
    assert "需要输入图" in e.value.message


def test_non_image_capability_with_image_is_400():
    with pytest.raises(InvalidParameterError) as e:
        resolve("jimeng-t2i", has_image=True)
    assert "不接受输入图" in e.value.message


def test_catalog_hides_deliberately_absent_capabilities():
    """「不制造假能力」：实测会失败的能力不许出现在清单里。"""
    ids = {m["id"] for m in catalog()}
    # jimeng-detail-fix 曾在此列表（两次 origin_image 形态失败）；
    # 2026-09-20 引用形态真跑成功后**转正**（免费，两证吻合）。
    assert DELIBERATE_ABSENCES == {}, "缺席必须被显式记录，不是忘了"
    # 五个已端到端验证过的能力都在；t2v/vfi/omni/detail-fix 见各自 notes
    assert ids == {c.api_id for c in CAPABILITIES}
    assert len(ids) == 11
    assert {"jimeng-t2v", "jimeng-t2v-fast", "jimeng-t2v-pro", "jimeng-vfi",
            "jimeng-omni-video", "jimeng-detail-fix"} <= ids


def test_detail_fix_tool_description_is_kept_for_future_investigation():
    """工具描述要留着，否则下次得重新取证。转正后 registered 标志已删。"""
    from app.upstream.jimeng.client import POST_EDIT_TOOLS

    assert "detail" in POST_EDIT_TOOLS
    assert "registered" not in POST_EDIT_TOOLS["detail"]


def test_every_capability_has_measured_credits_where_claimed():
    """单价要么是**实测值**，要么就不写 —— 不许编一个"看起来合理"的数。

    2026-09-20 校准：原先 5 个能力的单价全部取自上游回执的 `forecast_generate_cost`，
    而按 `submit_id` 对账后实测**高估 4~9 倍**（i2i 报 59 / 实扣 **12**）。
    ⇒ 未实测的一律置 `None`：宁可"不报数"，也不能报一个让调用方算错成本的值。
    """
    measured = [c for c in CAPABILITIES if c.credits_measured is not None]
    assert measured, "至少要有一个实测价（i2i）"
    for c in measured:
        assert c.credits_measured >= 0, f"{c.api_id} 的实测价不能为负"
    # 实测免费的能力要**如实报 0**（而不是 None、也不是编一个数）
    free = [c.name for c in CAPABILITIES if c.credits_measured == 0]
    assert {"t2i", "i2i", "hd"} <= set(free), \
        f"Lite 上实测免费的应如实报 0，实得 {free}"
    for c in CAPABILITIES:
        assert c.notes, f"{c.api_id} 缺少依据说明"
