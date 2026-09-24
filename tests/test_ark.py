#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""火山方舟（Ark）契约门面用例 —— **零真实上游调用**。

对照 2026-09-20 方舟文档抓取的契约：
  · POST /api/v3/contents/generations/tasks → {"id": "…"}（只回 id）
  · GET  /api/v3/contents/generations/tasks/{id} →
    {id, model, status, error, content{video_url}, usage?, created_at, ...}
  · status 枚举：queued / running / succeeded / failed
"""
from __future__ import annotations

import json

import pytest

from app.ark import STATUS_TO_ARK, ark_task_view, translate_ark_create
from app.errors import InvalidParameterError
from app.store import TaskRecord

from conftest import AUTH, ok_state

ARK_MODEL = "doubao-seedance-2-0-mini-260615"


# ---------------------------------------------------------------------------
# 请求翻译
# ---------------------------------------------------------------------------


def test_translate_minimal_create():
    our, deg = translate_ark_create({
        "model": ARK_MODEL,
        "content": [{"type": "text", "text": "一只猫"}],
    })
    # 🔴 方舟名**原样保留**（内部能力解析在受理端）—— 对外只有方舟 model
    assert our["model"] == ARK_MODEL
    assert our["prompt"] == "一只猫"
    # 分流留痕：方舟名 → 即梦侧实际档位（不出现内部能力名）
    assert any("doubao-seedance-2-0-mini-260615" in d and "mini" in d
               for d in deg)


def test_translate_maps_ratio_duration_seed():
    our, _ = translate_ark_create({
        "model": ARK_MODEL,
        "content": [{"type": "text", "text": "x"}],
        "ratio": "16:9", "duration": 4, "resolution": "720p", "seed": 7,
    })
    assert our["aspect_ratio"] == "16:9"
    assert our["duration"] == 4
    assert our["resolution"] == "720p"
    assert our["seed"] == 7


def test_translate_adaptive_ratio_degrades_to_default():
    our, deg = translate_ark_create({
        "model": ARK_MODEL,
        "content": [{"type": "text", "text": "x"}], "ratio": "adaptive",
    })
    assert "aspect_ratio" not in our
    assert any("adaptive" in d and "16:9" in d for d in deg)


def test_translate_seed_minus1_equals_random():
    our, _ = translate_ark_create({
        "model": ARK_MODEL,
        "content": [{"type": "text", "text": "x"}], "seed": -1,
    })
    assert "seed" not in our


def test_translate_reference_roles_become_omni_materials():
    """方舟 r2v/i2v 角色 → 即梦全能参考素材（image/video/audio 数组）。"""
    our, deg = translate_ark_create({
        "model": ARK_MODEL,
        "content": [
            {"type": "text", "text": "全程使用视频1的构图，音频1作为背景音乐"},
            {"type": "image_url", "image_url": {"url": "https://x/a.png"},
             "role": "first_frame"},
            {"type": "image_url", "image_url": {"url": "https://x/b.png"},
             "role": "reference_image"},
            {"type": "video_url", "video_url": {"url": "https://x/v.mp4"},
             "role": "reference_video"},
            {"type": "audio_url", "audio_url": {"url": "https://x/a.mp3"},
             "role": "reference_audio"},
        ],
    })
    assert our["image"] == ["https://x/a.png", "https://x/b.png"]
    assert our["video"] == ["https://x/v.mp4"]
    assert our["audio"] == ["https://x/a.mp3"]
    assert any("全能参考" in d and "2 图 / 1 视频 / 1 音频" in d for d in deg)
    # 角色语义不逐一对应 —— 必须留痕
    assert any("first_frame" in d or "参考图" in d for d in deg)


def test_translate_unsupported_params_degrade_not_reject():
    """watermark/generate_audio/callback_url 等方舟常规参数：降级留痕不挡人。"""
    our, deg = translate_ark_create({
        "model": ARK_MODEL,
        "content": [{"type": "text", "text": "x"}],
        "watermark": False, "generate_audio": True,
        "callback_url": "https://cb.example.com",
        "return_last_frame": True,
    })
    for k in ("watermark", "generate_audio", "callback_url", "return_last_frame"):
        assert any(k in d for d in deg)
    assert our["prompt"] == "x"


def test_translate_rejects_unknown_model():
    with pytest.raises(InvalidParameterError, match="model"):
        translate_ark_create({"model": "doubao-seedream-4-0",
                              "content": [{"type": "text", "text": "x"}]})


# ---------------------------------------------------------------------------
# 查询视图
# ---------------------------------------------------------------------------


def _rec(**kw) -> TaskRecord:
    base = dict(task_id="jimeng_abc", credential_id="c", model="jimeng-t2v",
                cap_key="jimeng:t2v", status="queued", prompt="x", n=1,
                created_at=1000, updated_at=2000,
                duration_ms=4000, aspect_ratio="16:9", size="720p",
                extra_json=json.dumps({"ark_model": ARK_MODEL}))
    base.update(kw)
    return TaskRecord(**base)


def test_ark_view_mapping():
    v = ark_task_view(_rec())
    assert v["id"] == "jimeng_abc"
    assert v["model"] == ARK_MODEL          # 回显方舟侧原始模型名
    assert v["status"] == "queued"
    assert v["error"] is None and v["content"] is None
    assert v["duration"] == 4 and v["ratio"] == "16:9" and v["resolution"] == "720p"
    assert "usage" not in v                 # 🔴 token 口径无法伪造 ⇒ 不给


def test_ark_view_success_has_video_url():
    v = ark_task_view(_rec(status="success",
                           images=[{"url": "https://tos/v.mp4", "width": 1280}],
                           credits=4, duration_ms=4000, aspect_ratio="16:9",
                           size="720p"))
    assert v["status"] == "succeeded"
    assert v["content"] == {"video_url": "https://tos/v.mp4"}
    assert v["usage"] == {"forecast_credits": 4}   # 扩展字段，不冒充 tokens
    assert "completion_tokens" not in v.get("usage", {})


def test_ark_view_failure_shape():
    v = ark_task_view(_rec(status="failure",
                           error={"code": "upstream", "message": "生成失败"}))
    assert v["status"] == "failed"
    assert v["error"] == {"code": "upstream", "message": "生成失败"}
    assert v["content"] is None


def test_status_table_covers_all_states():
    assert set(STATUS_TO_ARK) == {"queued", "in_progress", "success",
                                  "failure", "canceled"}
    assert STATUS_TO_ARK["success"] == "succeeded"


# ---------------------------------------------------------------------------
# 端到端（假上游）
# ---------------------------------------------------------------------------


def test_ark_create_and_poll(app_and_client, fake_jimeng, client_state):
    """方舟形态受理 → 派发 → 轮询到成功，全程走方舟契约。"""
    _, client, _ = app_and_client
    fake_jimeng.states = [ok_state(["https://tos.example.com/v.mp4"], cost=4)]
    r = client.post("/api/v3/contents/generations/tasks", headers=AUTH,
                    json={"model": ARK_MODEL,
                          "content": [{"type": "text", "text": "一只猫"}],
                          "ratio": "16:9", "duration": 4, "resolution": "720p"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id"}                     # 方舟创建响应只回 id
    task_id = body["id"]

    client_state.coordinator.tick()                # 建任务
    call = fake_jimeng.of("submit_video")[0]
    assert call["prompt"] == "一只猫"
    assert call["resolution"] == "720p"
    assert call["duration_ms"] == 4000
    assert call["aspect_ratio"] == "16:9"
    rec = client_state.service.store.get(task_id)
    assert rec is not None and rec.model == "jimeng-t2v"
    assert rec.extra_json and ARK_MODEL in rec.extra_json

    for _ in range(2):
        client_state.coordinator.tick()            # 轮询到终态
    g = client.get(f"/api/v3/contents/generations/tasks/{task_id}", headers=AUTH)
    assert g.status_code == 200
    v = g.json()
    assert v["id"] == task_id
    assert v["model"] == ARK_MODEL
    assert v["status"] == "succeeded"
    assert v["content"]["video_url"] == "https://tos.example.com/v.mp4"
    # 模型映射降级必须在查询里可见
    # 模型分流留痕必须在查询里可见（方舟名 → 即梦侧档位说明）
    assert any("mini" in d for d in v.get("degradations", []))


def test_ark_vfi_signal_routes_to_frame_interpolation(app_and_client, fake_jimeng,
                                                       client_state):
    """门面 vfi 入口：content[] 带 video_url + target_fps（视频生视频 = 插帧）。

    方舟契约没有补帧概念 —— 本服务以 `source_task_id`/`target_fps` 作为
    显式信号把请求路由到补帧链路（单视频、内容不变，与全能参考不同）。
    """
    from app.upstream.jimeng import GeneratedImage, TaskState

    vid = "v02870g10004danqpu27dld82i49g5r0"
    vid_ref = "data:video/mp4;base64," + __import__("base64").b64encode(
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64).decode()

    def _video_ok() -> TaskState:
        st = TaskState(submit_id="up-submit-vfi-src", status=50,
                       status_name="success", finished=True, failed=False, cost=4)
        st.images = [GeneratedImage(url="https://tos.example.com/v.mp4",
                                    width=1280, height=720, format="mp4",
                                    item_id="7687552710358420760", vid=vid,
                                    note="视频产物")]
        st.history_record_id = "44854324452620"
        return st

    _, client, _ = app_and_client
    fake_jimeng.states = [_video_ok()]
    r = client.post("/api/v3/contents/generations/tasks", headers=AUTH,
                    json={"model": ARK_MODEL,
                          "content": [
                              {"type": "text", "text": "把这段视频补到 60fps"},
                              {"type": "video_url",
                               "video_url": {"url": vid_ref}},
                          ],
                          "target_fps": 60})
    assert r.status_code == 200, r.text
    rec = client_state.service.store.get(r.json()["id"])
    assert rec.model == "jimeng-vfi"
    assert rec.extra_json and "local_video" in rec.extra_json
    assert any("补帧" in d for d in rec.degradations)

    client_state.coordinator.tick()
    call = fake_jimeng.of("submit_video_vfi")[0]
    assert call["target_fps"] == 60
    assert call["vid"]                              # 本地视频经 VOD 上传 → vid
