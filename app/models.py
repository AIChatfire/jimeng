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
    credits_measured: int | None = None
    notes: str = ""

    @property
    def api_id(self) -> str:
        """对外 `model` 取值形态：`jimeng-t2i`。"""
        return f"jimeng-{self.name}"


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="jimeng:t2i", name="t2i", title="文生图",
        accepts_image=False, image_required=False, prompt_required=True,
        credits_measured=44,
        notes="上游模型 high_aes_general_v50；异步建任务→轮询，**建任务即计费**。"
              "实测一次出图端到端 17.9-19s（1 张 2048×2048）。",
    ),
    Capability(
        key="jimeng:i2i", name="i2i", title="图生图（blend）",
        accepts_image=True, image_required=True, prompt_required=True,
        jimeng_tool="blend", credits_measured=40,
        notes="输入图由本服务自动上传成即梦资产 uri；**必须给 prompt**"
              "（描述要怎么改）——这是它与后编辑三工具的关键区别。",
    ),
    Capability(
        key="jimeng:hd", name="hd", title="超清（SuperDefinition）",
        accepts_image=True, image_required=True, prompt_required=False,
        jimeng_tool="normal_hd", credits_measured=9,
        notes="实测 2048×2048 → **4096×4096**，**9 积分**。同一族里最便宜的，"
              "且出图最大 —— 别按名字选工具。",
    ),
    Capability(
        key="jimeng:pro-hd", name="pro-hd", title="智能超清（ProHD）",
        accepts_image=True, image_required=True, prompt_required=False,
        jimeng_tool="pro_hd", credits_measured=91,
        notes="实测 2048×2048 → **2160×2160**，**91 积分**。"
              "**又贵又小**（超清 4096 只要 9 积分）。",
    ),
    Capability(
        key="jimeng:outpaint", name="outpaint", title="扩图（OutPaint）",
        accepts_image=True, image_required=True, prompt_required=False,
        jimeng_tool="outpaint", credits_measured=28,
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


def resolve(model: str | None, *, has_image: bool) -> tuple[Capability, str | None]:
    """解析 `model`，返回 (能力, 上游模型 key 或 None)。

    `has_image` 参与两件事：① 默认能力推导；② **能力与请求形态的一致性校验**
    （没给图却指定了吃图的能力 → 400，而不是跑到上游才发现）。

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
        _check_shape(cap, raw=raw, has_image=has_image)
        return cap, upstream_model

    # ---- 默认能力：只在无歧义时给 ----
    cands = [c for c in CAPABILITIES
             if c.accepts_image is has_image and not (has_image and not c.image_required)]
    if len(cands) == 1:
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


def _check_shape(cap: Capability, *, raw: str, has_image: bool) -> None:
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
        "`core_param`）都返回 `status=30 generate_failed`，且**照样计费**。抓包里它是"
        "链式第 4 跳、带 `item_id`/`origin_history_id`（引用账号里已有作品）——"
        "独立草稿形态大概不满足其前置条件。按「不制造假能力」摘除，"
        "工具描述仍留在 client.POST_EDIT_TOOLS 供将来续查。"
    ),
}


__all__ = [
    "Capability", "CAPABILITIES", "REGISTRY", "ALIASES", "catalog",
    "resolve", "is_placeholder", "DELIBERATE_ABSENCES",
    "DEFAULT_UPSTREAM_MODEL", "UPSTREAM_MODEL_KEYS", "PLACEHOLDER_MODELS",
]
