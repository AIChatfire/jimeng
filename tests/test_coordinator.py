#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协调器与流水线门禁：受理 → 建任务 → 轮询 → 终态。

这些用例里的每一条都对应一个**真实踩过的坑**，注释里写明了是哪个。
"""
from __future__ import annotations

import pytest

from app.errors import RiskControlError
from app.upstream.jimeng import JimengQuotaError, JimengRateLimitError, JimengRiskError
from tests.conftest import AUTH, failed_state, ok_state, submitted_state

BASE = "/async/v1/images/generations"


def _create(client, **body):
    body.setdefault("model", "jimeng-t2i")
    body.setdefault("prompt", "飞上天")
    r = client.post(BASE, json=body, headers=AUTH)
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


# ---------------------------------------------------------------------------
# 正常流水线
# ---------------------------------------------------------------------------


def test_full_pipeline_queued_to_success(client, client_state, fake_jimeng, service):
    """受理 → 建任务 → 轮询 → 终态。

    ⚠️ 假上游要**先给一个非终态**：协调器一轮 tick 里会「建任务 + 立刻轮询」，
    若第一次 `fetch` 就返回终态，任务会一跳即 success，
    既不符合真实链路（真上游要几十秒），也测不到"非终态不被判死"那一段。
    生产里 `POLL_GRACE`（默认 3s）也会把这两段分开。
    """
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    tid = _create(client)

    assert service.store.get(tid).status == "queued"

    client_state.coordinator.tick()          # 建任务（同一轮里的那次轮询仍非终态）
    rec = service.store.get(tid)
    assert rec.status == "in_progress"
    assert rec.upstream_submit_id == fake_jimeng.submitted[0]
    assert rec.finished_at is None, "非终态不许写 finished_at"

    client_state.coordinator.tick()          # 轮询 → success
    rec = service.store.get(tid)
    assert rec.status == "success"
    assert [i["url"] for i in rec.images] == ["https://cdn/a.png"]
    assert rec.finished_at


def test_dispatch_passes_the_right_upstream_model_and_count(client, client_state,
                                                             fake_jimeng):
    """断言的是**上游收到了什么**，不是"我们的函数返回对了"。

    只测后者测不到"归一化写完了但没接进调用路径"这类装配缺陷。
    """
    fake_jimeng.states = [ok_state(["https://cdn/a.png"])]
    _create(client, model="jimeng-t2i", prompt="一只猫", size="2048x2048", n=3,
            seed=42)
    client_state.coordinator.tick()
    assert len(fake_jimeng.of("submit")) == 1
    call = fake_jimeng.of("submit")[0]
    assert call["model"] == "high_aes_general_v50"
    assert call["count"] == 3
    assert call["seed"] == 42
    assert call["prompt"] == "一只猫"


def test_i2i_uploads_the_input_image_before_submitting(client, client_state,
                                                       fake_jimeng, fake_uploader,
                                                       service, settings):
    """图生图必须**先上传**拿到 `image_uri`，再建任务。

    即梦的草稿只认自己存储里的资产（`tos-cn-i-<bucket>/<hash>`），
    外链喂不进去 —— 顺序反了会得到上游参数错误。
    """
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    data_uri = ("data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                "nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    tid = _create(client, model="jimeng-i2i", prompt="改成海边",
                  image=[data_uri])
    client_state.coordinator.tick()

    assert fake_uploader.uploads, "输入图没有被上传"
    calls = fake_jimeng.kinds()
    assert calls.index("blend") >= 0
    assert fake_jimeng.of("blend")[0]["image_uri"] == fake_uploader.uri
    assert service.store.get(tid).status == "in_progress"


@pytest.mark.parametrize("model,expect_tool", [
    ("jimeng-hd", "normal_hd"),
    ("jimeng-pro-hd", "pro_hd"),
    ("jimeng-outpaint", "outpaint"),
])
def test_post_edit_family_routes_to_the_right_tool(client, client_state,
                                                   fake_jimeng, service, settings,
                                                   model, expect_tool):
    """后编辑四工具**共享同一端点**，差异只在 `generate_type` 字符串。

    路由选错工具 = 花了 91 积分却以为在做超清（或反之）。
    """
    fake_jimeng.states = [ok_state(["https://cdn/a.png"])]
    data_uri = ("data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                "nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    _create(client, model=model, image=[data_uri])
    client_state.coordinator.tick()
    edits = fake_jimeng.of("edit")
    assert len(edits) == 1
    assert edits[0]["tool"] == expect_tool


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_generate_failed_is_reported_as_failure_with_reason(client, client_state,
                                                            fake_jimeng):
    """🔴「被接受」≠「能跑通」：上游 `ret=0` 之后任务仍可能生成失败，**而且照样计费**。"""
    fake_jimeng.states = [failed_state("generate_failed boom")]
    tid = _create(client)
    client_state.coordinator.tick()
    client_state.coordinator.tick()

    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert body["status"] == "failure"
    assert "generate_failed" in body["error"]["message"]
    assert "计费" in body["error"]["message"], "要提醒调用方这次失败是花过钱的"


def test_retryable_submit_error_requeues_without_failing(client, client_state,
                                                         fake_jimeng, service):
    """限流可退避重试 ⇒ 回队列，**不判死**。"""
    fake_jimeng.fail_submit = JimengRateLimitError("upstream busy", code=2020)
    tid = _create(client)
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "queued", "可重试的错误不该让任务直接失败"
    assert rec.attempts == 1


def test_retryable_error_stops_after_max_attempts(client, client_state,
                                                  fake_jimeng, service):
    """重试一个**付费**动作的成本是非线性的 ⇒ 必须封顶。"""
    fake_jimeng.fail_submit = JimengRateLimitError("upstream busy", code=2020)
    tid = _create(client)
    for _ in range(5):
        client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "failure"
    assert rec.error["code"] == "upstream_rate_limited"


def test_quota_error_fails_immediately_and_does_not_retry(client, client_state,
                                                          fake_jimeng, service):
    """额度耗尽重试一万次也不会变好，而且每次都是付费动作。"""
    fake_jimeng.fail_submit = JimengQuotaError("no credits", code=1006)
    tid = _create(client)
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "failure"
    assert rec.error["code"] == "upstream_quota_exhausted"
    assert fake_jimeng.of("submit"), "至少试过一次"


def test_risk_error_enters_cooldown_and_fails(client, client_state, fake_jimeng,
                                             service, settings):
    """风控命中 ⇒ 失败 + 进入冷却；冷却期内**不再打上游**（持续施压会延长标记）。"""
    fake_jimeng.fail_submit = JimengRiskError("punish", code=1018)
    tid = _create(client)
    client_state.coordinator.tick()
    assert service.store.get(tid).status == "failure"
    assert service.gate.stats()["cooling_for"] > 0

    before = len(fake_jimeng.calls)
    _create(client, prompt="另一个任务")
    client_state.coordinator.tick()
    assert len(fake_jimeng.calls) == before, "冷却期内不该再调上游"


def test_auth_error_is_a_deployment_problem_not_the_callers(client, client_state,
                                                            fake_jimeng, service):
    from app.upstream.jimeng import JimengAuthError

    fake_jimeng.fail_submit = JimengAuthError("session expired", code=1015)
    tid = _create(client)
    client_state.coordinator.tick()
    body = client.get(f"{BASE}/{tid}", headers=AUTH).json()
    assert body["status"] == "failure"
    # 上游凭据失效是**部署问题** ⇒ 提示文案要指导"联系服务方"，而不是让调用方改参数
    assert "JIMENG_SESSIONID" in body["error"]["message"]


# ---------------------------------------------------------------------------
# 并发上限：按**库计数**，不是进程内信号量
# ---------------------------------------------------------------------------


def test_concurrency_limit_is_enforced_from_the_store(client, client_state,
                                                      fake_jimeng, service,
                                                      settings):
    """上限判据是 `count(in_progress)` —— 重启安全、跨 worker 也正确。

    用进程内信号量的话，重启后信号量归零而库里的任务还在上游跑，会**超发**。
    """
    fake_jimeng.states = []          # 永远非终态 ⇒ 一直占着 in_progress
    for i in range(3):
        _create(client, prompt=f"task-{i}")
    client_state.coordinator.tick()
    assert service.store.count_by_status("in_progress") == settings.jm_concurrency
    assert fake_jimeng.of("submit"), "至少提交一个"
    # 并发 1 时后续 tick 不该再提交第二个
    before = len(fake_jimeng.of("submit"))
    client_state.coordinator.tick()
    assert len(fake_jimeng.of("submit")) == before


def test_gate_can_block_dispatch_without_consuming_attempts(service, client,
                                                            client_state,
                                                            fake_jimeng):
    """闸门拒绝（冷却中）时**不消耗重试次数** —— 那还没轮到上游说话。"""
    tid = _create(client)
    service.gate.mark_risk_hit()      # 直接把闸门打进冷却
    rec_before = service.store.get(tid).attempts
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "queued"
    assert rec.attempts == rec_before, "闸门拦下的不该记作一次失败尝试"
    assert not fake_jimeng.of("submit")


def test_gate_raises_risk_control_error_when_cooling(service):
    service.gate.mark_risk_hit()
    with pytest.raises(RiskControlError) as e:
        service.gate.acquire()
    assert e.value.retry_after and e.value.retry_after > 0
    assert e.value.retryable is False, "风控不可重试（重试会延长标记）"


# ---------------------------------------------------------------------------
# 超时看门狗
# ---------------------------------------------------------------------------


def test_watchdog_expires_a_task_that_never_reaches_terminal(service, client,
                                                             client_state,
                                                             fake_jimeng):
    """没人查就永远卡在 in_progress —— 惰性方案下看门狗永不触发，所以需要后台协调器。"""
    fake_jimeng.states = []
    tid = _create(client)
    client_state.coordinator.tick()
    assert service.store.get(tid).status == "in_progress"

    service.settings.task_timeout = -1.0     # 立刻超时
    client_state.coordinator.tick()
    rec = service.store.get(tid)
    assert rec.status == "failure"
    assert rec.error["code"] == "upstream_timeout"


# ---------------------------------------------------------------------------
# 选主：防重复提交 = 防重复计费
# ---------------------------------------------------------------------------


def test_lease_prevents_two_coordinators_from_dispatching_the_same_task(
        service, settings, client):
    """两个协调器同时跑时，只有一个能推进。

    没有租约的话，两个进程会同时提交同一个任务 —— 而建任务是**计费动作**。
    """
    from app.coordinator import Coordinator

    tid = _create(client, prompt="only once")
    c1 = Coordinator(service, settings, owner="c1")
    c2 = Coordinator(service, settings, owner="c2")

    c1.tick()
    rec = service.store.get(tid)
    assert rec.status == "in_progress"

    # c2 在这轮里被租约挡住；即便抢到也不该重复提交（任务已离开 queued）
    before = service.store.get(tid).upstream_submit_id
    c2.tick()
    assert service.store.get(tid).upstream_submit_id == before


# ---------------------------------------------------------------------------
# 未配凭据
# ---------------------------------------------------------------------------


def test_coordinator_skips_when_upstream_not_configured(settings, store,
                                                        fake_jimeng,
                                                        fake_uploader):
    from app.service import Service
    from app.coordinator import Coordinator

    blank = settings.replace(jimeng_sessionid="", jimeng_cookie="")
    svc = Service(blank, store=store, client=fake_jimeng,
                  uploader=fake_uploader, cfg=None)
    co = Coordinator(svc, blank, owner="x")
    co.tick()                       # 不该抛，也不该调上游
    assert fake_jimeng.calls == []
