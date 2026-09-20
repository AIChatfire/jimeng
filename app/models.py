#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力注册表：`model` 字符串 → 即梦的一个能力。

## 为什么用 `model` 承载能力

参考接口（`/async/v1/images/generations`）的 body 是
`{model, prompt, image}` —— `model` 是唯一能承载"要哪一个能力"的字段，
这与 OpenAI / seedream 的取向一致（同一个端点靠 model 分流）。

## 命名

即梦这条链路上"能力"与"上游模型"是**两个维度**：
  · **能力**决定走哪条草稿构造路径（文生图 / 图生图 blend / 后编辑四工具）；
  · **上游模型**（如 `high_aes_general_v50`）只在文生图族里有意义。

所以对外用 `<能力>`，并在需要时允许直接写上游模型 key（等价于"文生图 + 该模型"）。

## 🔴 默认能力：**只在无歧义时给默认**

没写 `model` 时：
  · 无输入图 → 只有 `jimeng-t2i` 能接 ⇒ 默认它；
  · 带输入图 → `i2i` / `hd` / `pro-hd` / `outpaint` 四个都能接 ⇒ **明确报错**，
    而不是随便挑一个。挑错等于替调用方做了他没做的决定，而**每个决定的花费差 10 倍**
    （实测：`hd` 9 积分 / `outpaint` 28 / `i2i` 40 / `pro-hd` **91**）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import InvalidParameterError

# ---------------------------------------------------------------------------
# 上游模型 key（用于文生图族；即梦服务端会下发能力表，见 capabilities.py）
# ---------------------------------------------------------------------------

#: 抓包实测用的默认模型（Seedream 5.0 Lite）。
DEFAULT_UPSTREAM_MODEL = "high_aes_general_v50"

#: 允许直接作为 `model` 写的上游 key。**只登记实测过的**：
#: `high_aes_general_v30l:general_v3.0_18b` 实测 `ret=1006` 权益不足，
#: 故不登记（写它会得到"未知模型"而不是一个会失败的模型）。
#: 完整取值仍可由 `GET /async/v1/models` 从服务端能力表读回（见 capabilities.py）。
UPSTREAM_MODEL_KEYS: tuple[str, ...] = (
    "high_aes_general_v50",
    "high_aes_general_v50p_large",
    "high_aes_general_v43",
    "high_aes_general_v42",
    "high_aes_general_v41",
    "high_aes_general_v40l",
    "high_aes_general_v40",
)


# ---------------------------------------------------------------------------
# 能力
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    key: str                       # "jimeng:hds" 形态的稳定 id（对外用 `jimeng-hd`）
    name: str                      # "t2i"
    title: str                     # 中文能力名
    accepts_image: bool = True
    image_required: bool = True
    prompt_required: bool = False
    #: 即梦后编辑工具名（对应 upstream/jimeng/client.py::POST_EDIT_TOOLS 的键）；
    #: `blend` 是图生图的特例（它不是 `POST_EDIT_TOOLS` 的成员）。
    jimeng_tool: str | None = None
    #: 本部署**已实测**的积分单价（张）。None = 未测，不报数。
        #: ⚠️ **只有实测过的才填。** 2026-09-20 校准：原先那批数（t2i 44 / i2i 59 /
        #: hd 9 / pro-hd 91 / outpaint 28）全部取自上游回执的 `forecast_generate_cost`，
        #: 而按 `submit_id` 对账（`/commerce/v1/benefits/user_credit_history`）实测**高估 4~9 倍**
        #: （i2i 报 55 / 实扣 **12**）。
        #: 🔴 **`0` 是「实测不扣分」，不是占位符**：t2i / hd 在 Seedream 5.0 Lite 上
        #: **没有产生任何消耗记录 ⇒ 一分没扣**（余额读数也一直没变）。
        #: 未实测的仍置 None —— 宁可「不报数」，也不报一个会让调用方算错成本的值。
    credits_measured: int | None = None
    #: 本次请求最多接受几张输入图（垫图）。
    #:
    #: 🔴 默认 **1**，且**超出必须响亮 400** —— 绝不许静默丢掉多余的图。
    #: 原先的行为是：`image` 收下 N 个 URL、全部下载，然后只用第 0 张，
    #: **既不报错也不留痕**；调用方以为用了 4 张、实际只用 1 张。
    #: 现在能力没声明支持几张，就只允许 1 张，多给的当场说清楚。
    max_images: int = 1
    notes: str = ""

    @property
    def api_id(self) -> str:
        """对外 `model` 取值形态：`jimeng-t2i`。"""
        return f"jimeng-{self.name}"


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="jimeng:t2i", name="t2i", title="文生图",
        accepts_image=False, image_required=False, prompt_required=True,
        credits_measured=0,
        notes="上游模型 high_aes_general_v50；异步建任务→轮询，**建任务即计费**。"
              "实测一次出图端到端 17.9-19s（1 张 2048×2048）。",
    ),
    Capability(
        key="jimeng:i2i", name="i2i", title="图生图（blend）",
        accepts_image=True, image_required=True, prompt_required=True,
        #: 🔴 2026-09-20 用户（账号所有者）确认：**i2i 实际也是免费的**。
        #: 我先前按一条消耗记录（09-20 11:05，`amount=12`，`submit_id` 对得上）
        #: 把它标成 12 —— **那是错的**，已更正为 0。
        #: ⚠️ 证据确有冲突（那条记录真实存在），可能是**免费期开始前**的调用、
        #: 或属于别的计费口径。**以账号所有者的口径为准**，但冲突本身记在这里，
        #: 免得后人再看到那条记录又改回去。
        jimeng_tool="blend", credits_measured=0,
        #: blend 是**唯一**原生用「列表」承载输入图的能力：
        #: 草稿里是 `abilities.blend.ability_list[0].image_uri_list`（列表）
        #: 与 `image_list`（列表）—— 所以多张垫图就是往这两个列表里多放元素，
        #: 不需要额外的组件串链（后编辑那三族才需要）。
        #: ⚠️ 4 是**保守值**：上游声明的比它宽 —— `get_common_config` 的
        #: `input_image_limit` = `[{'max_image_num': 10, 'ability_name': 'byte_edit'}]`
        #: （`byte_edit` 就是 blend）⇒ 5.0 Pro 允许 **10** 张垫图。
        #: 但那个字段我们**读不出来**（`capabilities.py` 用 `_as_int()` 读数组 ⇒ 恒 None），
        #: 所以先钉 4。要放开得先把解析改成按数组取 `max_image_num`，
        #: 并同步抬高全局 `MAX_INPUT_IMAGES`。详见 `docs/UPSTREAM.md` §11。
        max_images=4,
        notes="输入图由本服务自动上传成即梦资产 uri；**必须给 prompt**"
              "（描述要怎么改）——这是它与后编辑三工具的关键区别。"
              "支持**多张垫图**（最多 4 张，超出会明确报错）。"
              "支持**指定张数** `n`（走 `abilities.gen_option.gen_count`）。"
              "⚠️ 实测**积分与张数不成正比**：n=1 实测 59 积分、n=4 实测 55 积分，"
              "所以别按「张数 × 单价」估算成本。",
    ),
    Capability(
        key="jimeng:hd", name="hd", title="超清（SuperDefinition）",
        accepts_image=True, image_required=True, prompt_required=False,
        jimeng_tool="normal_hd", credits_measured=0,
        notes="实测 2048×2048 → **4096×4096**，**9 积分**。同一族里最便宜的，"
              "且出图最大 —— 别按名字选工具。",
    ),
    Capability(
        key="jimeng:pro-hd", name="pro-hd", title="智能超清（ProHD）",
        accepts_image=True, image_required=True, prompt_required=False,
        jimeng_tool="pro_hd", credits_measured=None,
        notes="实测 2048×2048 → **2160×2160**，**91 积分**。"
              "**又贵又小**（超清 4096 只要 9 积分）。",
    ),
    Capability(
        key="jimeng:outpaint", name="outpaint", title="扩图（OutPaint）",
        accepts_image=True, image_required=True, prompt_required=False,
        jimeng_tool="outpaint", credits_measured=None,
        notes="实测一次出 **4 张 4000×4000**，**与请求张数无关**（由上游决定），"
              "按 4 张计费 28 积分。",
    ),
)

REGISTRY: dict[str, Capability] = {c.key: c for c in CAPABILITIES}

#: 对外 `model` → 能力。`jimeng-t2i` / `jimeng-i2i` / `jimeng-hd` …
_BY_API_ID: dict[str, Capability] = {c.api_id: c for c in CAPABILITIES}
#: 裸能力名 → 能力（本服务只有一个上游，故裸名无歧义）
_BY_NAME: dict[str, Capability] = {c.name: c for c in CAPABILITIES}

#: 中文别名。**只给本服务确实拥有的能力加**，且刻意不做"改指"：
#: 别名一旦指向另一个能力，就会把既有调用方静默换到另一条链路（换个链路 = 换计费与手感）。
ALIASES: dict[str, str] = {
    "即梦": "jimeng-t2i",
    "jimeng": "jimeng-t2i",
    "文生图": "jimeng-t2i",
    "图生图": "jimeng-i2i",
    "超清": "jimeng-hd",
    "智能超清": "jimeng-pro-hd",
    "扩图": "jimeng-outpaint",
    # 常见英文写法
    "text2image": "jimeng-t2i",
    "image2image": "jimeng-i2i",
    "upscale": "jimeng-hd",
}

#: 第三方 SDK 常硬编码的占位模型名 —— 它们**不代表**调用意图。
#: 见到它们等价于"没写 model"，走默认能力推导（而不是报"未知模型"）。
PLACEHOLDER_MODELS = {
    "auto", "", "default", "none",
    "dall-e", "dall-e-2", "dall-e-3",
    "gpt-image-1", "gpt-image-1-mini", "gpt-image-2",
    "stable-diffusion", "sd", "sd3", "flux", "flux-1",
    "image-1", "openai",
}
PLACEHOLDER_PREFIXES = ("dall-e", "gpt-image", "sd-", "flux", "seedream", "doubao-seedream")


def is_placeholder(model: str | None) -> bool:
    low = (model or "").strip().lower()
    if low in PLACEHOLDER_MODELS:
        return True
    return any(low.startswith(p) for p in PLACEHOLDER_PREFIXES)


def _hint() -> str:
    ids = ", ".join(c.api_id for c in CAPABILITIES)
    return (f"model 取值：{ids}；"
            f"也可只写能力名（t2i / i2i / hd / pro-hd / outpaint）、"
            f"中文别名，或直接写上游模型 key（如 {DEFAULT_UPSTREAM_MODEL}）。"
            f"完整清单见 GET /async/v1/models")


def resolve(model: str | None, *, has_image: bool,
            n_images: int = 1) -> tuple[Capability, str | None]:
    """解析 `model`，返回 (能力, 上游模型 key 或 None)。

    `has_image` 参与两件事：① 默认能力推导；② **能力与请求形态的一致性校验**
    （没给图却指定了吃图的能力 → 400，而不是跑到上游才发现）。

    `n_images` 参与**张数**校验：每个能力声明自己最多接受几张垫图
    （`Capability.max_images`），**超出当场 400** —— 绝不静默丢掉多余的图。

    上游模型 key 只在文生图族有意义；其余能力返回 None（草稿构造里不带 `model`）。
    """
    raw = (model or "").strip()

    if raw and not is_placeholder(raw):
        low = raw.lower()
        cap: Capability | None = None
        upstream_model: str | None = None

        if low in ALIASES:
            cap = _BY_API_ID[ALIASES[low]]
        elif low in _BY_API_ID:
            cap = _BY_API_ID[low]
        elif low in _BY_NAME:
            cap = _BY_NAME[low]
        elif raw in UPSTREAM_MODEL_KEYS:
            cap = _BY_NAME["t2i"]
            upstream_model = raw
        else:
            raise InvalidParameterError(
                f"未知 model {raw!r}。{_hint()}", param="model")
        assert cap is not None
        _check_shape(cap, raw=raw, has_image=has_image, n_images=n_images)
        return cap, upstream_model

    # ---- 默认能力：只在无歧义时给 ----
    cands = [c for c in CAPABILITIES
             if c.accepts_image is has_image and not (has_image and not c.image_required)]
    if len(cands) == 1:
        # 默认分支同样要过形态校验（含张数上限）—— 否则"省掉 model"就成了绕过校验的口子
        _check_shape(cands[0], raw=cands[0].api_id, has_image=has_image,
                     n_images=n_images)
        return cands[0], (DEFAULT_UPSTREAM_MODEL if cands[0].name == "t2i" else None)

    kind = "带输入图" if has_image else "无输入图"
    if not cands:
        raise InvalidParameterError(
            f"本服务没有任何能力能处理该形态（{kind}），请检查请求。", param="model")
    raise InvalidParameterError(
        f"未指定 model，且{kind}时本服务有多个能力可选（"
        f"{', '.join(c.api_id for c in cands)}），无法确定用哪个 —— "
        f"它们的单价差可达 10 倍（hd 9 / outpaint 28 / i2i 40 / pro-hd 91 积分），"
        f"故不替你挑。请显式指定 model。",
        param="model")


def _check_shape(cap: Capability, *, raw: str, has_image: bool,
                 n_images: int = 1) -> None:
    """能力与请求形态必须自洽 —— 不自洽就在**发出上游请求之前**报 400。"""
    if cap.image_required and not has_image:
        raise InvalidParameterError(
            f"model {raw!r}（{cap.title}）需要输入图，但本次请求的 image 为空。"
            f"若想做文生图请用 jimeng-t2i。",
            param="image")
    if has_image and not cap.accepts_image:
        raise InvalidParameterError(
            f"model {raw!r}（{cap.title}）不接受输入图，但本次请求带了 image。"
            f"请改用 jimeng-i2i / jimeng-hd / jimeng-pro-hd / jimeng-outpaint。",
            param="image")
    if n_images > cap.max_images:
        multi = ("支持多张垫图，但" if cap.max_images > 1 else "")
        raise InvalidParameterError(
            f"model {raw!r}（{cap.title}）{multi}最多接受 {cap.max_images} 张输入图，"
            f"本次给了 {n_images} 张。"
            f"（本服务**不会静默忽略多余的图** —— 那会让你以为用了 {n_images} 张、"
            f"实际只用了 {cap.max_images} 张。"
            + ("要多张垫图请用 jimeng-i2i。" if cap.max_images == 1 else "")
            + "）",
            param="image")


def catalog() -> list[dict]:
    """`GET /async/v1/models` 用：本服务对外宣告的模型清单。

    ⚠️ 只列**已端到端验证过**的能力。刻意缺席的（如细节修复 `super_resolution`
    两次 `status=30 generate_failed`）不出现在这里 —— 那就是"制造假能力"。
    """
    return [
        {
            "id": c.api_id,
            "object": "model",
            "created": 0,
            "owned_by": "jimeng",
            "title": c.title,
            "accepts_image": c.accepts_image,
            "requires_image": c.image_required,
            "requires_prompt": c.prompt_required,
            "credits_measured": c.credits_measured,
            "notes": c.notes,
        }
        for c in CAPABILITIES
    ]


#: 刻意缺席的能力 —— 出现在文档与门禁里，不出现在 catalog 里。
DELIBERATE_ABSENCES: dict[str, str] = {
    "jimeng-detail-fix": (
        "细节修复（super_resolution）。2026-09-19 两次真实提交（第二次补上了抓包里的 "
        "`core_param`）都返回 `status=30 generate_failed`，且**照样计费**。"
        "2026-09-20 补抓真实 UI 包确认：它的 `postedit_param` **没有 origin_image**，"
        "输入图靠 `item_id`/`origin_history_id`（账号里已有作品）承载，"
        "且组件带 `parent_id` 挂在生成父组件下 —— 单组件 + origin_image 形态"
        "大概不满足其前置条件；公网直链（source_from=link）无证据支持。"
        "按「不制造假能力」摘除，工具描述仍留在 client.POST_EDIT_TOOLS 供将来续查"
        "（详见 docs/UPSTREAM.md §9.1）。"
    ),
}


__all__ = [
    "Capability", "CAPABILITIES", "REGISTRY", "ALIASES", "catalog",
    "resolve", "is_placeholder", "DELIBERATE_ABSENCES",
    "DEFAULT_UPSTREAM_MODEL", "UPSTREAM_MODEL_KEYS", "PLACEHOLDER_MODELS",
]
