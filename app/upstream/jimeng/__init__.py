#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""即梦上游适配 —— **唯一的一份实现**。

## 为什么是"唯一一份"

上游参考项目（reverse-proxy）里有两份重写的实现：研究侧 `jimeng/client.py`
（探针用）与服务侧 `biz-api/app/upstreams/jimeng.py`（容器只 `COPY app/`，
跨目录 import 在镜像里不成立）。代价是**必须靠 AST 断言逐条比对两侧的
sign 向量 / 比例表 / 状态表 / 五张错误码表**，任一侧漂移就红。

本仓是独立微服务，不存在"镜像只 COPY app/"这个约束 ⇒ 收敛为一份，
AST 一致性门禁随之取消。**少一份实现，少一类"两份悄悄漂移"的缺陷。**

## 模块

| 模块 | 职责 |
|---|---|
| `sign.py` | 请求签名（纯 MD5，离线可复现）+ 11 条抓包向量自检 |
| `client.py` | 建任务 / 取任务 / 草稿构造 / 错误分类 / 比例归并 |
| `upload.py` | 本地图片 → 即梦可引用的 `image_uri`（火山 ImageX 四段式，纯 stdlib AWS4） |
| `capabilities.py` | 服务端能力表（零成本 `get_common_config`）缓存 |
"""

from .client import (
    DEFAULT_MODEL,
    DEFAULT_SIZE,
    DEFAULT_VIDEO_ASPECT_RATIO,
    DEFAULT_VIDEO_MODEL,
    DEFAULT_VIDEO_RESOLUTION,
    DEFAULT_VFI_TARGET_FPS,
    ERR_NO,
    IMAGE_RATIOS,
    POST_EDIT_TOOLS,
    TASK_STATUS,
    VIDEO_ASPECT_RATIOS,
    VIDEO_COMMERCE,
    VIDEO_RESOLUTIONS,
    GeneratedImage,
    JimengAuthError,
    JimengClient,
    JimengContentError,
    JimengError,
    JimengParamError,
    JimengQuotaError,
    JimengRateLimitError,
    JimengRiskError,
    JimengTimeout,
    TaskState,
    build_blend_draft,
    build_draft,
    build_post_edit_draft,
    build_video_draft,
    build_video_omni_draft,
    build_video_vfi_draft,
    classify_reject,
    parse_size,
    parse_task,
    raise_for_ret,
    ratio_for_size,
    resolve_video_commerce,
)
from .upload import ImageXError, ImageXUploader, VodUploader

__all__ = [
    "JimengClient", "TaskState", "GeneratedImage", "ImageXUploader", "ImageXError",
    "VodUploader",
    "JimengError", "JimengAuthError", "JimengRateLimitError", "JimengQuotaError",
    "JimengRiskError", "JimengContentError", "JimengParamError", "JimengTimeout",
    "build_draft", "build_blend_draft", "build_post_edit_draft", "build_video_draft",
    "build_video_omni_draft", "build_video_vfi_draft",
    "parse_task",
    "parse_size", "ratio_for_size", "raise_for_ret", "classify_reject",
    "POST_EDIT_TOOLS", "TASK_STATUS", "IMAGE_RATIOS", "ERR_NO",
    "DEFAULT_MODEL", "DEFAULT_SIZE",
    "DEFAULT_VIDEO_MODEL", "DEFAULT_VIDEO_RESOLUTION",
    "DEFAULT_VIDEO_ASPECT_RATIO", "DEFAULT_VFI_TARGET_FPS",
    "VIDEO_COMMERCE", "VIDEO_RESOLUTIONS", "VIDEO_ASPECT_RATIOS",
    "resolve_video_commerce",
]
