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
                                         "gpt-image-1",
                                         "doubao-seedream-5-0-pro-260628"])
def test_placeholders_count_as_unspecified(placeholder):
    """第三方 SDK 硬编码的占位名**不代表调用意图** ⇒ 走默认推导而不是报"未知模型"。

    ⚠️ `seedream-4-0` **已从这里移出**：它同时是即梦 web 面板上的正式模型名
    （Seedream 4.0），2026-09-23 起登记为**精确别名**。
    带厂商前缀 + 日期后缀的 `doubao-seedream-5-0-pro-260628` 仍算占位 ——
    它命不中别名，而且**绝不能**被映射到 Pro（那等于替调用方悄悄换到
    8 积分/张的链路）。
    """
    from app.models import DEFAULT_UPSTREAM_MODEL

    assert is_placeholder(placeholder)
    cap, model = resolve(placeholder, has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model == DEFAULT_UPSTREAM_MODEL, "占位名要落到默认上游模型"


def test_every_web_alias_maps_to_a_registered_upstream_model():
    """别名表**逐条**自检（不人工列举：表改了门禁跟着走，漏登记当场红）。

    别名是"web 面板名 → 上游 key"的派生数据，源是服务端能力表的
    `model_name` / `generation_category_name`（2026-09-23 实读）。
    """
    from app.models import UPSTREAM_MODEL_ALIASES, UPSTREAM_MODEL_KEYS

    assert UPSTREAM_MODEL_ALIASES, "别名表不许为空"
    for written, key in UPSTREAM_MODEL_ALIASES.items():
        assert key in UPSTREAM_MODEL_KEYS, f"{written!r} 指向未登记的 {key}"
        cap, model = resolve(written, has_image=False)
        assert (cap.api_id, model) == ("jimeng-t2i", key), written


@pytest.mark.parametrize("written,expect", [
    ("Seedream 5.0 Flash", "high_aes_general_v50_flash"),
    ("seedream 5.0 flash", "high_aes_general_v50_flash"),
    ("SEEDREAM_5.0_FLASH", "high_aes_general_v50_flash"),
    ("Seedream_5_0_Flash", "high_aes_general_v50_flash"),
    ("图片・5.0 Pro", "high_aes_general_v50p_large"),
    ("图片·5.0 Pro", "high_aes_general_v50p_large"),
    ("5.0 Lite", "high_aes_general_v50"),
    ("4.7", "high_aes_general_v43"),
    ("图片・4.5", "high_aes_general_v40l"),
])
def test_web_panel_name_variants_are_normalised(written, expect):
    """大小写与 `-`/`_`/空格/`.`/`・`/`·` 的差异都要归一。

    ⚠️ `・`(U+30FB) 与 `·`(U+00B7) 是**两个**字符，面板分类名用的是前者 ——
    少归一一种就会出现"看着一模一样、查表却查不中"的静默失效。
    """
    cap, model = resolve(written, has_image=False)
    assert cap.api_id == "jimeng-t2i"
    assert model == expect, written


def test_unregistered_web_models_are_rejected_with_a_reason():
    """面板上点得到、本服务**没登记**的（Seedream 3.0 / 3.1）不许静默退化成默认模型。

    这条与"占位名走默认"是**相反**的处置，区别在于：占位名（`auto`/`dall-e-3`/
    `doubao-*`）不代表调用意图，而 `Seedream 3.1` 是调用方**指名道姓**。
    指名道姓就给明确理由（该系列实测 `ret=1006` 权益不足）。
    """
    from app.models import UNSUPPORTED_WEB_MODELS, UPSTREAM_MODEL_KEYS

    assert UNSUPPORTED_WEB_MODELS, "未登记面板名表不许为空"
    for written, key in UNSUPPORTED_WEB_MODELS.items():
        assert key not in UPSTREAM_MODEL_KEYS, f"{key} 不该同时又算已登记"
        with pytest.raises(InvalidParameterError) as e:
            resolve(written, has_image=False)
        assert "未登记" in e.value.message, written
        assert key in e.value.message, "报错要能追到上游 key"


def test_upstream_name_tables_do_not_drift():
    """`UPSTREAM_MODEL_KEYS` 与 `UPSTREAM_WEB_NAMES` 必须是**同一集合**。

    两张表漂移的典型症状：新模型登记进白名单、忘了补面板名 ⇒
    `/v1/models` 里 `web_name` 是空串，调用方按面板名传反而被拒。
    """
    from app.models import UPSTREAM_MODEL_KEYS, UPSTREAM_WEB_NAMES

    assert set(UPSTREAM_WEB_NAMES) == set(UPSTREAM_MODEL_KEYS)
    assert all(UPSTREAM_WEB_NAMES[k].strip() for k in UPSTREAM_MODEL_KEYS)


def test_upstream_model_credits_table_does_not_drift():
    """**按模型**的实测单价表必须与白名单同集合，且不许被能力级的值污染。

    🔴 这是最容易犯的错：`jimeng-t2i` 的能力级实测价是 0（**Lite 口径**），
    而同一个端点换模型就换价 —— Flash 实测 3、Pro 实测 8。
    把能力级的值抄给每个上游模型 = 对 Flash 报"免费"，调用方会算错成本。
    """
    from app.models import (UPSTREAM_MODEL_CREDITS, UPSTREAM_MODEL_KEYS,
                            UPSTREAM_WEB_NAMES)

    assert set(UPSTREAM_MODEL_CREDITS) == set(UPSTREAM_MODEL_KEYS)
    assert set(UPSTREAM_WEB_NAMES) == set(UPSTREAM_MODEL_KEYS)
    for key, cost in UPSTREAM_MODEL_CREDITS.items():
        assert cost is None or cost >= 0, key
    assert UPSTREAM_MODEL_CREDITS["high_aes_general_v50"] == 0, "Lite 实测免费"
    assert UPSTREAM_MODEL_CREDITS["high_aes_general_v50_flash"] == 3, \
        "Flash 2026-09-23 实跑实扣 3（2k/1 张）"


def test_catalog_exposes_upstream_models_only_for_the_t2i_family():
    """`upstream_models` 只挂文生图族 —— 别的能力没有"上游模型"这个维度。"""
    from app.models import UPSTREAM_MODEL_CREDITS, UPSTREAM_MODEL_KEYS

    for m in catalog():
        if m["id"] == "jimeng-t2i":
            assert [u["key"] for u in m["upstream_models"]] == list(UPSTREAM_MODEL_KEYS)
            assert all(u["web_name"] for u in m["upstream_models"]), \
                "面板名不许为空 —— 空说明门禁没跟上"
            #: 每项报的是**该模型**的实测价，不是能力级的 0
            for u in m["upstream_models"]:
                assert u["credits_measured"] == UPSTREAM_MODEL_CREDITS[u["key"]], u
        else:
            assert "upstream_models" not in m, f"{m['id']} 不该有 upstream_models"


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


def test_new_upstream_model_flash_is_registered_with_declared_count_options():
    """2026-09-23 服务端能力表实读新增的 **Seedream 5.0 Flash** 必须可路由。

    证据等级要说清楚：这是**上游自己宣告**的（`is_new_model: true`、`feats`
    含 `t2i`、`generate_count_options` 1..4、`default_resolution_type` 2k、
    benefit_type `image_basic_v50_flash_2k` / `_15k`），**不是端到端实跑**。
    所以这里只钉三件事：能解析、张数快照与服务端一致、**不报单价** ——
    绝不钉任何积分数值（未实测就不许报，见上一条门禁）。
    """
    from app.models import UPSTREAM_MODEL_KEYS
    from app.upstream.jimeng.client import COUNT_OPTIONS_BY_MODEL

    key = "high_aes_general_v50_flash"
    assert key in UPSTREAM_MODEL_KEYS, "服务端宣告的新模型必须登记才能被路由"
    #: 能读的就不许猜：张数快照必须与服务端声明一致（服务端 = 1..4）
    assert COUNT_OPTIONS_BY_MODEL[key] == (1, 2, 3, 4)

    cap, model = resolve(key, has_image=False)
    assert cap.api_id == "jimeng-t2i", "新模型是 t2i 族的一个选项，不是新能力"
    assert model == key
    assert key not in {c.api_id for c in CAPABILITIES}, \
        "上游模型 key 不该被注册成一个独立能力"

    #: 🔴 单价纪律：`credits_measured` 是**能力级**的（t2i 按默认模型 Lite 实测 0），
    #: 本服务**没有**"按上游模型分档的单价"字段 ⇒ 新模型不可能、也不许"被报一个价"。
    #: 这条断言防的是"后人顺手把 flash 的 amount=1 抄成单价"（§12 已证 amount≠实扣）。
    assert cap.credits_measured == 0, "t2i 的实测值仍是 Lite 口径，不该被新模型改写"
    #: 但它已经在 **2026-09-23 端到端实跑过**（2k/1 张，实扣 3）——
    #: notes 必须如实标明"实跑"，而不是还停在"服务端宣告、未验证"。
    assert key in cap.notes and "实跑" in cap.notes, \
        "已跑过的模型，notes 必须写明实跑记录"
