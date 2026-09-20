#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""火山方舟（Ark）视频生成 API 的**契约门面**。

对外形态逐字段对齐方舟《创建视频生成任务》/《查询视频生成任务》
（`POST|GET /api/v3/contents/generations/tasks`），底层翻译到本服务既有的
即梦 Seedance 视频链路 —— 方舟 SDK / 既有调用方无需改代码即可切换。

## 为什么是"门面"而不是代理

本服务的上游是**即梦网页协议**（sessionid，免费/低价额度），不是方舟。
门面只对齐**契约形态**，计费与能力边界都是即梦侧的：

· 模型名 `doubao-seedance-*` → 映射到 `jimeng-t2v`（即梦 Seedance 4.0 Mini），
  **降级留痕**（请求的模型 ≠ 实际服务的模型，必须让调用方看见）；
· `content[]`：`text` ⇒ 文生视频（t2v）；带 `image_url`/`video_url`/`audio_url`
  ⇒ 翻译成即梦**全能参考**（omni_reference，material_list+meta_list）——
  方舟的角色语义（first_frame 等）不逐一对应，降级留痕；
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

#: 方舟模型名前缀 → 本服务能力。`doubao-seedance-*` 全家都映射到 t2v
#: （即梦侧实际服务的模型以 `degradations` 说明为准）。
ARK_MODEL_PREFIX = "doubao-seedance"
ARK_TARGET_MODEL = "jimeng-t2v"

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
    if not (low == ARK_TARGET_MODEL or low.startswith(ARK_MODEL_PREFIX)):
        raise InvalidParameterError(
            f"model {model!r} 本服务不支持。支持的模型：{ARK_TARGET_MODEL}（即梦 "
            f"Seedance t2v）或任何 doubao-seedance-*（映射到前者，降级留痕）。",
            param="model")

    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise InvalidParameterError(
            "content 必须是非空数组（方舟形态：[{type: text, text: ...}, ...]）",
            param="content")
    degradations: list[str] = []
    if low != ARK_TARGET_MODEL:
        degradations.append(
            f"模型 {model} 已映射到 {ARK_TARGET_MODEL}"
            f"（即梦 Seedance 4.0 Mini，t2v）—— 实际服务的模型以本说明为准。")
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

    our: dict[str, Any] = {"model": ARK_TARGET_MODEL,
                           "prompt": "\n".join(prompt_parts)}
    if images:
        our["image"] = images
    if videos:
        our["video"] = videos
    if audios:
        our["audio"] = audios
    if images or videos or audios:
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
           "ARK_MODEL_PREFIX", "ARK_TARGET_MODEL"]
