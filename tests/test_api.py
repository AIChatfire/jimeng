#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外契约门禁（`docs/INTERFACE.md` 的冻结部分）。

这些用例断言的是**响应体的键集与形状**，不是"值大概对"。
多一个键、少一个键、类型不对，都要红 —— 契约保真是类型级的。
"""
from __future__ import annotations

import pytest

from tests.conftest import AUTH, AUTH_B, KEY_B, ok_state

BASE = "/async/v1/images/generations"


# ---------------------------------------------------------------------------
# 受理
# ---------------------------------------------------------------------------


def test_create_returns_only_a_task_id(client):
    """受理**只回一个 id**（用户明确要求）。

    多回 `status`/`created_at`/`batch_key` 之类，会让调用方以为那些字段有语义。
    """
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "飞上天",
                                "image": []}, headers=AUTH)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"task_id"}, f"受理响应只应有 task_id，实得 {set(body)}"
    assert body["task_id"].startswith("jimeng_")
    assert len(body["task_id"]) == len("jimeng_") + 32


def test_create_sets_location_header(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    tid = r.json()["task_id"]
    assert r.headers["location"] == f"{BASE}/{tid}"


def test_create_does_not_touch_upstream(client, fake_jimeng):
    """受理在请求内**零上游往返** —— 建任务是计费动作，交给后台协调器。"""
    client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    assert fake_jimeng.calls == [], f"受理阶段不该调上游，实得 {fake_jimeng.kinds()}"


def test_placeholder_model_falls_back_to_t2i(client):
    """第三方 SDK 硬编码的占位名（dall-e-3 等）不代表调用意图。"""
    r = client.post(BASE, json={"model": "dall-e-3", "prompt": "x", "image": []},
                    headers=AUTH)
    assert r.status_code == 202


# ---------------------------------------------------------------------------
# 输入校验：全部在**发出上游请求之前**
# ---------------------------------------------------------------------------


def test_image_must_be_an_array_with_actionable_message(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "image": "https://a/b.png"}, headers=AUTH)
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert "必须是**数组**" in msg or "必须是数组" in msg
    assert "[]" in msg, "报错要给出正确写法，而不是只说「错了」"


def test_multiple_input_images_are_never_silently_truncated(client):
    """核心纪律不变：**绝不许**"收下 N 张、却只用 1 张"。

    契约按能力分（2026-09-20 起）：
      · `jimeng-i2i`（图生图）—— 上游草稿里 `image_uri_list` / `image_list`
        本来就是**列表**，所以**支持多张垫图**：全部上传、全部进草稿；
      · 后编辑三族（hd / pro-hd / outpaint）—— 上游用单个 `origin_image` 承载，
        多给必须**响亮 400**，不许静默丢；
      · 全局另有一道 4 张的合理性上限（挡住"一次塞几百个 URL"）。

    换句话说：**"支持"与"拒绝"必须是明确的两种行为，不允许有第三种（忽略）**。
    """
    # ① i2i：2 张是合法的（多张垫图）
    r = client.post(BASE, json={"model": "jimeng-i2i", "prompt": "改海边",
                                "image": ["https://a/1.png", "https://a/2.png"]},
                    headers=AUTH)
    assert r.status_code == 202, r.text

    # ② 后编辑族：多给仍然明确报错
    r = client.post(BASE, json={"model": "jimeng-hd",
                                "image": ["https://a/1.png", "https://a/2.png"]},
                    headers=AUTH)
    assert r.status_code == 400, r.text
    msg = r.json()["error"]["message"]
    assert "最多接受 1 张" in msg
    assert "不会静默忽略" in msg, "报错要说清为什么拒绝"


def test_unknown_field_is_rejected(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x", "foo": 1},
                    headers=AUTH)
    assert r.status_code == 400
    assert "未知字段" in r.json()["error"]["message"]


def test_known_but_unsupported_field_becomes_a_degradation(client, client_state):
    """**认得但做不到**的字段 ≠ 写错。前者进 `degradations`，后者 400。

    分不清这两者，调用方就会去查上游能力表，而真正的问题在请求体。
    """
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "watermark": True}, headers=AUTH)
    assert r.status_code == 202
    tid = r.json()["task_id"]
    got = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert got["status"] == "queued"
    assert any("watermark" in d for d in got["degradations"])


def test_capability_requiring_image_without_image_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-hd", "prompt": ""}, headers=AUTH)
    assert r.status_code == 400
    assert "需要输入图" in r.json()["error"]["message"]


def test_t2i_with_image_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "image": ["https://a/1.png"]}, headers=AUTH)
    assert r.status_code == 400
    assert "不接受输入图" in r.json()["error"]["message"]


def test_prompt_required_for_i2i(client):
    r = client.post(BASE, json={"model": "jimeng-i2i",
                                "image": ["https://a/1.png"]}, headers=AUTH)
    assert r.status_code == 400
    assert "需要 prompt" in r.json()["error"]["message"]


def test_bad_size_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x",
                                "size": "huge"}, headers=AUTH)
    assert r.status_code == 400
    assert "2048x2048" in r.json()["error"]["message"]


def test_bad_n_is_400(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x", "n": 0},
                    headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "n"


# ---------------------------------------------------------------------------
# 查询：状态与形状
# ---------------------------------------------------------------------------


def test_pending_task_returns_202_with_status(client):
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    r = client.get(f"{BASE}/{tid}", headers=AUTH)
    assert r.status_code == 202, "非终态必须是 202 —— 调用方据此继续轮询"
    assert r.json() == {"task_id": tid, "status": "queued"}


def test_success_body_matches_the_frozen_shape(client, client_state, fake_jimeng):
    """成功体 = `{data:[{url}], created, usage}`，且 `data[]` 里**只有 url**。"""
    fake_jimeng.states = [ok_state(["https://cdn/1.png", "https://cdn/2.png"],
                                   cost=44)]
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x", "n": 2},
                      headers=AUTH).json()["task_id"]

    client_state.coordinator.tick()   # 建任务
    client_state.coordinator.tick()   # 轮询到终态

    r = client.get(f"{BASE}/{tid}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"data", "created", "usage"}, f"键集不符：{set(body)}"
    assert [d["url"] for d in body["data"]] == ["https://cdn/1.png",
                                                "https://cdn/2.png"]
    assert set(body["data"][0]) == {"url"}, "data[] 里只该有 url（与冻结契约逐字一致）"
    assert isinstance(body["created"], int)
    # 🔴 名字里带 forecast：它是上游的**预估**，不是实际扣费
    # （实测 i2i 报 55 / 实扣 12，高估 4~9 倍）
    assert body["usage"] == {"images": 2, "forecast_credits": 44}


def test_usage_omits_unknown_fields_instead_of_inventing_them(client,
                                                              client_state,
                                                              fake_jimeng):
    """上游没给 `forecast_generate_cost` 时，`credits` 键**不出现**。

    给个 0 更坏：那是在声称"本次消耗 0 积分"，而不是"不知道"。
    """
    st = ok_state(["https://cdn/1.png"])
    st.cost = None
    fake_jimeng.states = [st]
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    client_state.coordinator.tick()
    client_state.coordinator.tick()
    usage = client.get(f"{BASE}/{tid}", headers=AUTH).json()["usage"]
    assert usage == {"images": 1}, "不知道的值不许编 —— 键不出现才对"
    assert "credits" not in usage and "forecast_credits" not in usage


def test_unknown_task_is_404_with_openai_error_envelope(client):
    r = client.get(f"{BASE}/jimeng_deadbeef", headers=AUTH)
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "task_not_found"
    assert err["message"]


def test_cross_credential_read_is_404_and_touches_no_upstream(client, fake_jimeng):
    """跨凭证读取必须**本地拦死**，而不是丢给上游去判。

    放行到上游就是"用错的钥匙去查"，返回的 404/空无法区分
    "任务真没了"与"钥匙不对"，而且把跨凭证隔离交给了别人的实现去兜。
    """
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    before = len(fake_jimeng.calls)
    r = client.get(f"{BASE}/{tid}", headers=AUTH_B)
    assert r.status_code == 404
    assert len(fake_jimeng.calls) == before, "跨凭证读取不该产生任何上游请求"


def test_list_is_scoped_to_the_calling_key(client):
    client.post(BASE, json={"model": "jimeng-t2i", "prompt": "a"}, headers=AUTH)
    client.post(BASE, json={"model": "jimeng-t2i", "prompt": "b"}, headers=AUTH_B)
    a = client.get(BASE, headers=AUTH).json()
    assert a["total"] == 1, "列表只按服务过滤会把别人的任务列给你"


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_delete_non_terminal_task_fails_loudly(client):
    """即梦没有取消端点 ⇒ 对未终态任务的删除必须**响亮失败**。

    本地删掉只会让"还在跑并继续计费"变成看不见的事。
    """
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    r = client.delete(f"{BASE}/{tid}", headers=AUTH)
    assert r.status_code == 400
    assert "没有取消端点" in r.json()["error"]["message"]


def test_delete_terminal_task_removes_the_record(client, client_state, fake_jimeng):
    fake_jimeng.states = [ok_state(["https://cdn/1.png"])]
    tid = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()["task_id"]
    client_state.coordinator.tick()
    client_state.coordinator.tick()
    assert client.delete(f"{BASE}/{tid}", headers=AUTH).json()["status"] == "DELETED"
    assert client.get(f"{BASE}/{tid}", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_missing_key_is_401(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_wrong_key_is_401(client):
    r = client.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                    headers={"Authorization": "Bearer sk-not-in-list"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 模型清单 / 运维端点
# ---------------------------------------------------------------------------


def test_models_endpoint_lists_only_verified_capabilities(client):
    data = client.get("/async/v1/models").json()
    ids = {m["id"] for m in data["data"]}
    assert ids == {"jimeng-t2i", "jimeng-i2i", "jimeng-hd",
                   "jimeng-pro-hd", "jimeng-outpaint"}
    assert "jimeng-detail-fix" not in ids, "刻意缺席的能力不许出现在清单里"


def test_healthz_is_dependency_free_and_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readyz_reports_upstream_configuration(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.service import Service

    blank = settings.replace(jimeng_sessionid="", jimeng_cookie="")
    monkeypatch.setattr(main_mod, "Service", lambda s: Service(s))
    app = main_mod.create_app(blank)
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 503
        body = c.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"},
                      headers=AUTH).json()
        assert body["error"]["code"] == "upstream_not_configured"


def test_unconfigured_upstream_is_503_not_401(client_factory):
    """上游凭据缺失是**部署问题**，不是调用方的身份问题 ⇒ 503 而非 401。"""
    c = client_factory(jimeng_sessionid="", jimeng_cookie="")
    r = c.post(BASE, json={"model": "jimeng-t2i", "prompt": "x"}, headers=AUTH)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "upstream_not_configured"


_ = (pytest, KEY_B)
