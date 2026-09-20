#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文生视频（jimeng-t2v / Seedance t2v）链路用例 —— **零真实上游调用**。

三条主线：
1. **提交侧逐字段对抓包**（2026-09-20 实抓：Seedance 4.0 Mini，720p×4s）；
2. **计费档位白名单**：没有抓包依据的 (resolution, duration) 一律拒绝 ——
   benefit_type/amount 写错 = 按错档位扣积分；
3. **全链路接线**：受理 → 协调器派发（假上游记录收到过什么）→ 轮询 → 响应形状。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, text

from app.store import TaskStore
from app.upstream.jimeng import (
    DEFAULT_VIDEO_MODEL,
    JimengClient,
    JimengParamError,
    build_video_draft,
    parse_task,
    resolve_video_commerce,
)

from conftest import AUTH, ok_state


# ---------------------------------------------------------------------------
# 草稿构造
# ---------------------------------------------------------------------------


def test_build_video_draft_matches_capture():
    """草稿结构逐字段对 2026-09-20 实抓（t2v）。"""
    metrics = {"k": "v"}
    draft = json.loads(build_video_draft(
        prompt="iphone100", resolution="720p", duration_ms=4000,
        aspect_ratio="16:9", seed=1671729512, metrics=metrics))
    assert draft["type"] == "draft"
    assert draft["is_from_tsn"] is True
    comp = draft["component_list"][0]
    assert comp["type"] == "video_base_component"          # 不是 image_base_component
    assert comp["generate_type"] == "gen_video"
    assert comp["process_type"] == 1
    assert comp["id"] == draft["main_component_id"]
    abilities = comp["abilities"]
    gen = abilities["gen_video"]
    inp = gen["text_to_video_params"]["video_gen_inputs"][0]
    assert inp["prompt"] == "iphone100"
    assert inp["video_mode"] == 2
    assert inp["fps"] == 24
    assert inp["duration_ms"] == 4000
    assert inp["resolution"] == "720p"
    assert inp["idip_meta_list"] == []
    assert gen["text_to_video_params"]["video_aspect_ratio"] == "16:9"
    assert gen["text_to_video_params"]["seed"] == 1671729512
    assert gen["text_to_video_params"]["model_req_key"] == DEFAULT_VIDEO_MODEL
    # video_task_extra 是 metrics 的原样复本（字符串）
    assert json.loads(gen["video_task_extra"]) == metrics
    # 🔴 视频草稿**没有张数字段**（实抓确认无 gen_option）
    assert "gen_option" not in abilities


def test_build_video_draft_requires_prompt():
    with pytest.raises(JimengParamError):
        build_video_draft(prompt="  ")


# ---------------------------------------------------------------------------
# 计费档位白名单
# ---------------------------------------------------------------------------


def test_resolve_video_commerce_whitelist():
    assert resolve_video_commerce("720p", 4) == \
        ("seedance_20_mini_720p_output_5s", 4)


def test_resolve_video_commerce_rejects_unverified_tier():
    # 1080p / 5s 没有抓包依据 —— 拒绝构造计费字段，绝不猜
    with pytest.raises(JimengParamError):
        resolve_video_commerce("1080p", 4)
    with pytest.raises(JimengParamError):
        resolve_video_commerce("720p", 5)


def test_submit_video_dry_run_never_sends():
    """dry_run：走完构造与档位校验，但一个字节都不发（建任务=计费）。"""
    calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ret": 0, "data": {}})

    with JimengClient(sessionid="s", transport=httpx.MockTransport(handler)) as c:
        sid = c.submit_video("一只猫", dry_run=True)
        assert sid
        assert c.last_draft                      # 草稿仍可取回（审计用）
    assert calls == []                           # 🔴 零上游往返


def test_submit_video_body_matches_capture():
    """真实路径（MockTransport）：请求体字段对实抓 —— extend 计费字段 /
    metrics_extra 视频专用形态 / draft_content 双重编码。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ret": 0, "data": {}})

    with JimengClient(sessionid="s", workspace_id=22052346345484,
                      transport=httpx.MockTransport(handler)) as c:
        sid = c.submit_video("iphone100", resolution="720p", duration_ms=4000,
                             aspect_ratio="16:9", seed=1671729512)
        body = captured["body"]

    assert captured["url"].startswith("https://jimeng.jianying.com/mweb/v1/aigc_draft/generate")
    assert body["submit_id"] == sid
    # 计费字段：amount/benefit_type 按白名单档位
    commerce = body["extend"]["m_video_commerce_info"]
    assert commerce == {"amount": 4, "benefit_type": "seedance_20_mini_720p_output_5s",
                        "resource_id": "generate_video", "resource_id_type": "str",
                        "resource_sub_type": "aigc"}
    assert body["extend"]["m_video_commerce_info_list"] == [commerce]
    assert body["extend"]["root_model"] == DEFAULT_VIDEO_MODEL
    assert body["extend"]["workspace_id"] == 22052346345484
    assert body["http_common_info"] == {"aid": 513695}
    # draft_content 是「JSON 字符串」而不是对象（双重编码，写成对象会被 1002 拒）
    draft = json.loads(body["draft_content"])
    assert isinstance(body["draft_content"], str)
    assert draft["component_list"][0]["type"] == "video_base_component"
    assert body["draft_content"] == c.last_draft
    # metrics_extra：视频专用形态（与图片族的 enterFrom=click 不同）
    metrics = json.loads(body["metrics_extra"])
    assert metrics["enterFrom"] == "ai_feature"
    assert metrics["functionMode"] == "omni_reference"
    assert metrics["batchNumber"] == 1
    assert metrics["originSubmitId"] == sid
    scene = json.loads(metrics["sceneOptions"])[0]
    assert scene["resolution"] == "720p"
    assert scene["videoDuration"] == 4
    assert scene["modelReqKey"] == DEFAULT_VIDEO_MODEL


# ---------------------------------------------------------------------------
# 结果解析（回包结构未实抓 —— 尽力而为，解析不到当失败）
# ---------------------------------------------------------------------------


def test_parse_task_video_item():
    node = {
        "task": {"status": 50, "submit_id": "sid"},
        "item_list": [{
            "common_attr": {"id": "item-1"},
            "video": {"video_url": "https://tos.example.com/v.mp4",
                      "width": 1280, "height": 720, "format": "mp4"},
        }],
    }
    st = parse_task("sid", node)
    assert st.ok
    assert len(st.images) == 1
    assert st.images[0].url == "https://tos.example.com/v.mp4"
    assert st.images[0].width == 1280
    # 未实抓验证的解析必须留痕（产物上如实标注）
    assert st.images[0].note


def test_parse_task_video_without_url_is_not_success_payload():
    """视频项存在但解析不出 URL ⇒ 零产物（上层会按失败处理，不伪装成功）。"""
    node = {
        "task": {"status": 50},
        "item_list": [{"video": {"width": 1280}}],
    }
    st = parse_task("sid", node)
    assert st.ok
    assert st.images == []


# ---------------------------------------------------------------------------
# 能力路由
# ---------------------------------------------------------------------------


def test_resolve_video_default_and_alias():
    from app import models

    cap, upstream = models.resolve(None, has_image=False, video=True)
    assert cap.api_id == "jimeng-t2v"
    assert upstream is None                       # 视频模型 key 由 client 携带
    cap, _ = models.resolve("文生视频", has_image=False, video=True)
    assert cap.api_id == "jimeng-t2v"
    cap, _ = models.resolve("jimeng-t2v", has_image=False, video=True)
    assert cap.key == "jimeng:t2v"


def test_resolve_keeps_image_pool_untouched():
    """🔴 回归门禁：视频能力加入后，图片端点的默认推导**必须不变**。"""
    from app import models

    cap, upstream = models.resolve(None, has_image=False, video=False)
    assert cap.api_id == "jimeng-t2i"
    assert upstream == models.DEFAULT_UPSTREAM_MODEL
    # 视频能力混进图片端点 = 传错了地方
    with pytest.raises(Exception, match="视频"):
        models.resolve("jimeng-t2v", has_image=False, video=False)


# ---------------------------------------------------------------------------
# 全链路（假上游）
# ---------------------------------------------------------------------------


def test_video_accept_and_dispatch(app_and_client, fake_jimeng, client_state):
    """受理 → 协调器派发：假上游收到 submit_video，参数透传正确。"""
    _, client, _ = app_and_client
    r = client.post("/async/v1/videos/generations", headers=AUTH,
                    json={"prompt": "一只猫在跳舞", "resolution": "720p",
                          "duration": 4, "aspect_ratio": "16:9", "seed": 7})
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"task_id"}               # 只回一个 id
    task_id = body["task_id"]

    client_state.coordinator.tick()               # 第 1 轮：建任务
    calls = fake_jimeng.of("submit_video")
    assert len(calls) == 1
    call = calls[0]
    assert call["prompt"] == "一只猫在跳舞"
    assert call["resolution"] == "720p"
    assert call["duration_ms"] == 4000
    assert call["aspect_ratio"] == "16:9"
    assert call["seed"] == 7
    rec = client_state.service.store.get(task_id)
    assert rec.status == "in_progress"
    assert rec.model == "jimeng-t2v"
    assert rec.duration_ms == 4000
    assert rec.aspect_ratio == "16:9"


def test_video_defaults_and_n_degradation(app_and_client, fake_jimeng, client_state):
    """不传可选参数 ⇒ 取实抓档位；n>1 ⇒ 降级留痕（草稿无张数字段）。"""
    _, client, _ = app_and_client
    r = client.post("/async/v1/videos/generations", headers=AUTH,
                    json={"prompt": "x", "n": 3})
    assert r.status_code == 202
    client_state.coordinator.tick()
    call = fake_jimeng.of("submit_video")[0]
    assert call["resolution"] == "720p"
    assert call["duration_ms"] == 4000
    assert call["aspect_ratio"] == "16:9"
    rec = client_state.service.store.get(r.json()["task_id"])
    assert rec.n == 1
    assert any("n=3" in d and "n=1" in d for d in rec.degradations)


def test_video_rejects_unverified_tier_at_accept(client):
    """没抓包依据的档位在**受理时**就 400 —— 不进队列、不碰上游、不扣积分。"""
    r = client.post("/async/v1/videos/generations", headers=AUTH,
                    json={"prompt": "x", "resolution": "1080p"})
    assert r.status_code == 400
    assert "抓包" in r.json()["error"]["message"]


def test_video_rejects_image_fields_on_video_endpoint_and_vice_versa(client):
    r = client.post("/async/v1/videos/generations", headers=AUTH,
                    json={"prompt": "x", "size": "2048x2048"})
    assert r.status_code == 400
    r = client.post("/async/v1/images/generations", headers=AUTH,
                    json={"prompt": "x", "duration": 4})
    assert r.status_code == 400


def test_video_end_to_end_success(app_and_client, fake_jimeng, client_state):
    """派发 → 轮询到成功：响应形状 usage.videos（不是 images）。"""
    _, client, _ = app_and_client
    fake_jimeng.states = [ok_state(["https://tos.example.com/v.mp4"], cost=4)]
    r = client.post("/async/v1/videos/generations", headers=AUTH,
                    json={"prompt": "x"})
    task_id = r.json()["task_id"]
    for _ in range(2):
        client_state.coordinator.tick()
    r = client.get(f"/async/v1/videos/generations/{task_id}")
    assert r.status_code == 200
    payload = r.json()
    assert payload["data"] == [{"url": "https://tos.example.com/v.mp4"}]
    assert payload["usage"]["videos"] == 1
    assert "images" not in payload["usage"]


def test_models_catalog_lists_video_capability(client):
    r = client.get("/async/v1/models")
    ids = {m["id"]: m for m in r.json()["data"]}
    assert "jimeng-t2v" in ids
    assert ids["jimeng-t2v"]["media"] == "video"
    # 诚实边界写进了 notes：未端到端实跑
    assert "端到端实跑未验证" in ids["jimeng-t2v"]["notes"]


# ---------------------------------------------------------------------------
# 存储迁移（老库补列）
# ---------------------------------------------------------------------------


def test_store_readds_video_columns_for_legacy_table(db_dsn):
    """模拟老库：列被删掉（= 老版本建的表）后重连，启动期必须自动补列。

    对应的真实事故面：`create_all` 只建表不加列，老库忘 ALTER 的症状是
    `store.patch()` 报 column does not exist（只在视频任务上炸）。
    """
    eng = create_engine(db_dsn)
    TaskStore(eng)                                # 首次：建全量表
    with eng.begin() as c:
        c.execute(text("ALTER TABLE tasks DROP COLUMN duration_ms"))
        c.execute(text("ALTER TABLE tasks DROP COLUMN aspect_ratio"))
    eng.dispose()

    store = TaskStore(db_dsn)                     # 重连：必须幂等补回两列
    rec = store.put(_TaskRecord_for_test())
    patched = store.patch(rec.task_id, duration_ms=4000, aspect_ratio="16:9")
    assert patched is not None
    assert patched.duration_ms == 4000
    assert patched.aspect_ratio == "16:9"


def _TaskRecord_for_test():
    from app.store import TaskRecord
    import time
    import uuid
    return TaskRecord(task_id=f"jimeng_{uuid.uuid4().hex}", credential_id="c",
                      model="jimeng-t2v", cap_key="jimeng:t2v", status="queued",
                      prompt="x", n=1, created_at=int(time.time()),
                      updated_at=int(time.time()))
