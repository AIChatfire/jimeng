#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""火山方舟（Ark）视频生成 API 的**契约门面**。

对外形态逐字段对齐方舟《创建视频生成任务》/《查询视频生成任务》
（`POST|GET /api/v3/contents/generations/tasks`），底层翻译到本服务既有的
即梦 Seedance 视频链路 —— 方舟 SDK / 既有调用方无需改代码即可切换。

## 为什么是"门面"而不是代理

本服务的上游是**即梦网页协议**（sessionid，免费/低价额度），不是方舟。
门面只对齐**契约形态**，计费与能力边界都是即梦侧的：

· 模型名 `doubao-seedance-*` → **按档位保守分流**（`ARK_MODEL_ROUTES`：
  2.0 mini→t2v / 2.0 fast→t2v-fast / 2.0 标准档→t2v-pro / 2.5→2.5 样片；
  未识别的方舟名按默认档 t2v 兜底），每条映射在 `degradations`
  **响亮留痕**（请求的模型 ≠ 实际服务的模型，必须让调用方看见）；
· `content[]`：`text` ⇒ 按模型分流；带 `image_url`/`video_url`/`audio_url`
  ⇒ 翻译成即梦**全能参考**（jimeng-omni-video，omni_reference，
  material_list+meta_list）—— 素材语义覆盖模型分流，同样留痕；
· `ratio "adaptive"` → 16:9 降级留痕；
· `duration`/`resolution` 过即梦计费档位白名单（720p×4s/5s）；
· `watermark` / `generate_audio` / `return_last_frame` / `callback_url` /
  `camera_fixed` / `output_format` / `frames` / `draft` / `service_tier` /
  `omni_reference_task_type` 等 → **认得但做不到**，降级留痕不报错
  （方舟客户端的常规参数不该把人挡在门外，但语义不能假装支持）。

## 诚实边界

· `usage`（tokens 计数）**不给** —— 即梦链路没有 token 口径，
  编一个数字等于伪造（`degradations` 里已说明按即梦积分口径）；
· 查询状态映射：queued→queued / in_progress→running / success→succeeded /
  failure|canceled→failed。
"""
from __future__ import annotations

import json
from typing import Any

from .errors import InvalidParameterError
from .store import TaskRecord

#: 方舟模型名前缀 → 本服务能力（**保守分流**，2026-09-24 起）。
#:
#: 🔴 不再"全家降级到同一个 t2v"——Seedance 本身是全能参考模型，
#: 各档位是**独立计费的链路**，映射错了等于替调用方换档。对齐依据 =
#: 即梦侧 key 实读（docs/UPSTREAM.md §16）：2.0 mini=`40_mini`、
#: 2.0 Fast VIP=`40_vision`、2.0 VIP=`40_pro_vision`、2.5=`45_pro_draft`。
#: 方舟与即梦计费体系独立（token vs 积分），每条映射都在 `degradations`
#: **响亮留痕**，实际服务的模型以留痕说明为准。
ARK_MODEL_PREFIX = "doubao-seedance"
ARK_DEFAULT_MODEL = "jimeng-t2v"
ARK_MODEL_ROUTES: tuple[tuple[str, str, str], ...] = (
    # (方舟名前缀, 本服务能力, 留痕里的即梦侧说明)
    ("doubao-seedance-2-0-mini", "jimeng-t2v",
     "即梦 Seedance 2.0 mini（720p × 4s/5s）"),
    ("doubao-seedance-2-0-fast", "jimeng-t2v-fast",
     "即梦 Seedance 2.0 Fast（720p × 5s，与方舟 fast 档同源）"),
    ("doubao-seedance-2-0", "jimeng-t2v-pro",
     "即梦 Seedance 2.0 VIP（720p × 5s，与方舟 2.0 标准档对齐）"),
    ("doubao-seedance-2-5", "jimeng-t2v-2.5-draft",
     "即梦 Seedance 2.5 样片（480p × 5s；2.5 正式版 1080p/4~30s 未接入）"),
)

#: 方舟状态 ⇄ 本服务任务状态。**只映射，不创造**。
STATUS_TO_ARK: dict[str, str] = {
    "queued": "queued",
    "in_progress": "running",
    "success": "succeeded",
    "failure": "failed",
    "canceled": "failed",
}

#: 方舟创建请求里**认得但本服务做不到**的字段 → 降级说明。
_ARK_UNSUPPORTED: dict[str, str] = {
    "watermark": "本服务不支持水印控制，参数已忽略（产物是否带水印由上游决定）。",
    "generate_audio": "即梦 t2v 链路没有音频开关（未抓包），参数已忽略。",
    "return_last_frame": "本服务不支持返回尾帧图，参数已忽略。",
    "callback_url": "本服务没有回调基础设施 —— 请轮询查询任务接口。",
    "camera_fixed": "即梦 t2v 链路没有固定摄像头参数（未抓包），已忽略。",
    "output_format": "输出格式由上游决定（实测 mp4），参数已忽略。",
    "frames": "即梦链路按时长（duration）而非帧数生成，参数已忽略。",
    "draft": "样片模式本服务不支持，参数已忽略。",
    "service_tier": "服务等级即梦侧不可配，参数已忽略。",
    "omni_reference_task_type": "全模态参考（r2v）本服务不支持（无参考输入能力），已忽略。",
    "execution_expires_after": "超时阈值由本服务 TASK_TIMEOUT 决定，参数已忽略。",
    "priority": "执行优先级本服务不支持，参数已忽略。",
    "safety_identifier": "用户标识透传无意义，参数已忽略。",
    "tools": "工具配置本服务不支持，参数已忽略。",
    "seed": None,   # seed 单独处理（会真正透传），不进降级表
}

#: 方舟 `content[]` 里**能翻译**的 role —— 当前只有 text。
_ARK_TEXT_TYPES = {"text"}


def resolve_ark_model(model: str) -> tuple[str, str]:
    """方舟模型名 → (本服务能力 api_id, 即梦侧说明)。

    前缀**长者优先**（`doubao-seedance-2-0-fast` 必须判在
    `doubao-seedance-2-0` 之前，否则 fast 会被标准档吃掉）。
    """
    low = (model or "").strip().lower()
    if low == ARK_DEFAULT_MODEL:
        return ARK_DEFAULT_MODEL, "即梦 Seedance 2.0 mini（720p × 4s/5s）"
    for prefix, cap_id, note in ARK_MODEL_ROUTES:
        if low.startswith(prefix):
            return cap_id, note
    return (ARK_DEFAULT_MODEL,
            "即梦 Seedance 2.0 mini（未识别的方舟模型名按默认档兜底）")


def translate_ark_create(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """方舟创建请求 → 本服务视频受理体 + 降级说明。

    返回 `(our_body, degradations)`。**契约不符当场 400**（content 角色、
    模型名、计费档位），**能力不及降级留痕**（watermark 等）。
    """
    if not isinstance(body, dict):
        raise InvalidParameterError("请求体必须是 JSON 对象")
    model = (body.get("model") or "").strip()
    if not model:
        raise InvalidParameterError("缺少 model（如 doubao-seedance-2-0-mini-260615）",
                                    param="model")
    low = model.lower()
    if not (low == ARK_DEFAULT_MODEL or low.startswith(ARK_MODEL_PREFIX)):
        raise InvalidParameterError(
            f"model {model!r} 本服务不支持。支持的模型：任何 doubao-seedance-*"
            f"（按档位分流到对应即梦链路）或 {ARK_DEFAULT_MODEL}。",
            param="model")

    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise InvalidParameterError(
            "content 必须是非空数组（方舟形态：[{type: text, text: ...}, ...]）",
            param="content")
    degradations: list[str] = []
    _, side_note = resolve_ark_model(model)
    degradations.append(
        f"model {model} 按「{side_note}」服务 —— 即梦侧实际模型与计费档位以本说明为准。")
    prompt_parts: list[str] = []
    images: list[str] = []
    videos: list[str] = []
    audios: list[str] = []
    for i, item in enumerate(content):
        if not isinstance(item, dict):
            raise InvalidParameterError(f"content[{i}] 必须是对象", param="content")
        t = item.get("type")
        if t == "text":
            txt = (item.get("text") or "").strip()
            if txt:
                prompt_parts.append(txt)
            continue
        if t in ("image_url", "video_url", "audio_url"):
            url = ((item.get(t) or {}) or {}).get("url")
            if not isinstance(url, str) or not url.strip():
                raise InvalidParameterError(
                    f"content[{i}] 的 {t} 缺少 url", param=f"content[{i}]")
            role = item.get("role") or ""
            if t == "image_url":
                images.append(url.strip())
                if role == "reference_image":
                    degradations.append(
                        f"content[{i}] role=reference_image：即梦全能参考没有"
                        f"『参考图』与『首帧』的角色区分，已按普通参考图处理。")
                elif role not in ("first_frame", "last_frame", ""):
                    degradations.append(
                        f"content[{i}] role={role!r} 不是方舟图片角色的标准取值，"
                        f"已按普通参考图处理。")
            elif t == "video_url":
                videos.append(url.strip())
                if role != "reference_video":
                    degradations.append(
                        f"content[{i}] role={role!r} 已按参考视频处理。")
            else:
                audios.append(url.strip())
            continue
        raise InvalidParameterError(
            f"content[{i}] 的 type {t!r} 不是方舟视频生成契约的取值。",
            param=f"content[{i}]")
    if not prompt_parts:
        raise InvalidParameterError("content 里缺少 text（prompt 不能为空）",
                                    param="content")

    # 🔴 `model` **原样保留方舟名**（2026-09-24 用户拍板"全部用方舟 model"）——
    # 内部能力解析在受理端（Service.create 的视频段）完成，`jimeng-*`
    # 内部名不出现在门面链路上。
    our: dict[str, Any] = {"model": model,
                           "prompt": "\n".join(prompt_parts)}
    if images:
        our["image"] = images
    if videos:
        our["video"] = videos
    if audios:
        our["audio"] = audios
    # 视频生视频（补帧/vfi）的**显式信号**：source_task_id / target_fps。
    # 方舟契约没有补帧概念，本服务以这两个字段表达"视频生视频 = 插帧"意图；
    # 受理端据此把请求路由到补帧链路（单 video_url 或直接引用源任务）。
    if body.get("source_task_id") is not None or body.get("target_fps") is not None:
        if images or audios or len(videos) > 1:
            raise InvalidParameterError(
                "补帧（视频生视频）只接受 1 条 video_url 素材，且不能与 "
                "image_url / audio_url 混用；引用本服务产物请用 source_task_id。",
                param="video")
        our["model"] = "jimeng-vfi"
        if body.get("source_task_id") is not None:
            our["source_task_id"] = str(body["source_task_id"])
            if videos:
                our.pop("video", None)
                degradations.append(
                    "source_task_id 与 video_url 同时给出 ⇒ 以源任务为准，"
                    "video_url 已忽略。")
        if body.get("target_fps") is not None:
            our["target_fps"] = int(body["target_fps"])
        degradations.append(
            "source_task_id/target_fps 指定 ⇒ 走**补帧**链路（视频生视频："
            "插帧到 60fps，内容不变）—— 方舟契约没有补帧概念，这是本服务的"
            "扩展语义；补帧不重画内容，与全能参考（模仿参考生成新片）不同。")
    elif images or videos or audios:
        degradations.append(
            f"参考素材已翻译为即梦**全能参考**（{len(images)} 图 / {len(videos)} 视频 / "
            f"{len(audios)} 音频）：方舟的角色语义（first_frame 等）不逐一对应，"
            f"素材以 material_list+meta_list 整体提交，效果以即梦实际生成为准。")
    for k in ("resolution", "duration", "aspect_ratio", "seed"):
        if body.get(k) is not None:
            our[k] = body[k]
    if our.get("seed") == -1:                  # 方舟默认 -1 = 随机 ⇒ 等价"没给"
        our.pop("seed")

    ratio = body.get("ratio")
    if ratio is not None:
        if ratio == "adaptive":
            our.pop("aspect_ratio", None)
            degradations.append(
                'ratio "adaptive" 本服务做不到（即梦需要显式比例）⇒ 已按默认 16:9 处理。')
        else:
            our["aspect_ratio"] = ratio

    for k, note in _ARK_UNSUPPORTED.items():
        if k in body and body[k] is not None and note:
            degradations.append(f"参数 {k}={body[k]!r}：{note}")
    return our, degradations


def ark_task_view(rec: TaskRecord) -> dict[str, Any]:
    """任务记录 → 方舟《查询视频生成任务》响应形状。

    · `id` 回本服务 task_id（不透明字符串，语义同方舟 `cgt-*`）；
    · `usage`（tokens）**不给** —— 即梦链路没有 token 口径，不伪造；
      预估积分在扩展字段 `degradations` 与本服务原生接口里；
    · `model` 回 Ark 门面收到的**原样**模型名（存于 `extra_json.ark_model`）；
      门面之前创建的任务回内部名。
    """
    status = STATUS_TO_ARK.get(rec.status, "failed")
    content: dict[str, Any] | None = None
    if rec.status == "success" and rec.images:
        content = {"video_url": rec.images[0].get("url")}
    error = None
    if rec.status == "failure":
        err = rec.error or {}
        error = {"code": err.get("code") or "internal",
                 "message": err.get("message") or "任务失败（原因未记录）"}
    extra: dict[str, Any] = {}
    if rec.extra_json:
        try:
            extra = json.loads(rec.extra_json)
        except ValueError:
            extra = {}
    out: dict[str, Any] = {
        "id": rec.task_id,
        "model": extra.get("ark_model") or rec.model,
        "status": status,
        "error": error,
        "content": content,
    }
    if rec.status == "success":
        usage: dict[str, Any] = {}
        if rec.credits is not None:
            # 🔴 这是即梦的 forecast（预估、高估），只能作为扩展信息存在 ——
            # 绝不冒充方舟的 completion_tokens/total_tokens（token 数无法伪造）。
            usage["forecast_credits"] = rec.credits
        if usage:
            out["usage"] = usage
    if rec.degradations:
        out["degradations"] = list(rec.degradations)   # 加性扩展，形状超集
    out.update({
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "seed": rec.seed,
        "duration": (rec.duration_ms // 1000) if rec.duration_ms else None,
        "ratio": rec.aspect_ratio,
        "resolution": rec.size,
    })
    return out


__all__ = ["translate_ark_create", "ark_task_view", "STATUS_TO_ARK",
           "ARK_MODEL_PREFIX", "ARK_DEFAULT_MODEL", "ARK_MODEL_ROUTES",
           "resolve_ark_model"]
