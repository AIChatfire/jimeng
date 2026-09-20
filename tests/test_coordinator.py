#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协调器与流水线门禁：受理 → 建任务 → 轮询 → 终态。

这些用例里的每一条都对应一个**真实踩过的坑**，注释里写明了是哪个。
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from app.errors import RiskControlError
from app.service import Service
from app.store import TaskRecord
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
    assert len(fake_uploader.uploads) == 1, "单张请求只该上传一次"
    calls = fake_jimeng.kinds()
    assert calls.index("blend") >= 0
    # blend 现在收的是**列表**（`image_uris`）—— 单张就是只含一个元素的列表
    assert fake_jimeng.of("blend")[0]["image_uris"] == fake_uploader.uris
    assert service.store.get(tid).status == "in_progress"


# ---------------------------------------------------------------------------
# 多张垫图
# ---------------------------------------------------------------------------

#: 三张**内容各不相同**的合法 PNG（2×2，红/绿/蓝）。
#: 内容不同才能验证"哪张换到了哪个 uri"。
#: ⚠️ 这三串是**程序生成并当场解回来验过**的 —— 手改 base64 会得到
#: "broken data stream" 的坏数据（我第一版就是手改的，三条用例一起挂在解码上）。
_PNGS = [
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg==",
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAE0lEQVR4nGNk+M/AwMDABCIYGAAMHgEDrNiLpwAAAABJRU5ErkJggg==",
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGNkYPjPwMDAxAAGAAsfAQMU4wsAAAAAAElFTkSuQmCC",
]


def _data_uri(b64: str) -> str:
    return "data:image/png;base64," + b64


def test_i2i_multi_reference_images_are_all_uploaded(client, client_state,
                                                     fake_jimeng, fake_uploader,
                                                     service):
    """🔴 i2i 的多张垫图必须**全部上传**（原先只上传第 1 张，其余静默丢掉）。"""
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    refs = [_data_uri(b) for b in _PNGS[:3]]

    tid = _create(client, model="jimeng-i2i", prompt="合成这三张", image=refs)
    client_state.coordinator.tick()

    assert len(fake_uploader.uploads) == 3, "三张垫图都该被上传"
    sent = fake_jimeng.of("blend")[0]["image_uris"]
    assert len(sent) == 3, "三张 uri 都要进草稿"
    assert len(set(sent)) == 3, "三张各自换到不同的 uri"
    assert set(sent) == set(fake_uploader.uris), "返回的 uri 都出自本次上传"
    assert service.store.get(tid).status == "in_progress"


def test_prepare_input_images_preserves_order_despite_out_of_order_completion(
        service, store, fake_uploader, settings):
    """🔴 并发上传但**保序** —— 返回的 uri 顺序必须与 `image_refs` 一一对应。

    顺序不是细节：草稿里 `image_uri_list` 的先后对生成语义有影响。

    做法：**关掉归一化**，这样"上传的字节"就等于"输入图的字节"，
    于是"哪张图换到哪个 uri"可以精确断言（`FakeUploader.pairs` 是字节→uri 的映射，
    与完成顺序无关）。再让 `delay_s` 按内容派生 ⇒ 完成顺序与提交顺序**不同**，
    所以"谁先传完谁排前面"的实现会在这条翻车。
    """
    fake_uploader.delay_s = 0.05
    svc = Service(settings.replace(normalize_uploads=False), store=store,
                  client=service.client, uploader=fake_uploader, cfg=None)
    refs = [_data_uri(b) for b in _PNGS[:3]]
    rec = store.put(TaskRecord(
        task_id="jimeng_order_test", credential_id="cred-a", model="jimeng-i2i",
        cap_key="jimeng:i2i", status="queued", prompt="p", image_refs=refs,
        size="2048x2048", n=1, created_at=0, updated_at=0))

    uris = svc._prepare_input_images(rec)

    expected = [fake_uploader.uri_for(base64.b64decode(b)) for b in _PNGS[:3]]
    assert uris == expected, f"顺序没保住：实得 {uris}，应为 {expected}"


def test_multi_image_uploads_run_in_parallel(
        client, client_state, fake_jimeng, fake_uploader, service):
    """多张垫图的上传**并发**跑 —— 3 张的耗时应远小于"串行 3 次"。

    每张按内容派生 1~3 倍 `delay_s`：串行下 3 张至少 3×0.05s；
    并发下接近"最慢的那一张"。门限给得宽松（只区分"并发"与"串行"两个量级），
    避免变成偶发用例。
    """
    fake_uploader.delay_s = 0.05
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    tid = _create(client, model="jimeng-i2i", prompt="并行",
                  image=[_data_uri(b) for b in _PNGS[:3]])

    t0 = time.time()
    client_state.coordinator.tick()
    elapsed = time.time() - t0

    assert len(fake_uploader.uploads) == 3
    assert elapsed < 0.28, f"3 张上传用了 {elapsed:.2f}s，看起来是串行的"
    assert service.store.get(tid).status == "in_progress"


def test_single_image_capabilities_reject_extra_images_instead_of_dropping_them(
        client, service):
    """🔴 只吃 1 张图的能力，多给必须**响亮 400**，不许静默丢图。

    原先的行为：`image` 收下 N 个 URL、全部下载、然后只用第 0 张，
    **既不报错也不留痕** —— 调用方以为用了 4 张、实际只用 1 张。
    这正是本仓一贯在防的"静默降级"。
    """
    r = client.post(BASE, json={"model": "jimeng-hd", "prompt": "x",
                                "image": [_data_uri(_PNGS[0]), _data_uri(_PNGS[1])]},
                    headers=AUTH)
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["param"] == "image"
    assert "最多接受 1 张" in err["message"]
    assert "不会静默忽略" in err["message"], "错误信息要说清为什么拒绝"
    assert "jimeng-i2i" in err["message"], "要指路：要多张垫图请用 i2i"


def test_blend_draft_carries_the_requested_count():
    """🔴 blend 的草稿必须带 `abilities.gen_option.gen_count` —— 否则张数不生效。

    实测教训：这个字段原先**漏了**，于是请求 `n=1` 时上游按**模型默认**出图
    （实测得到 **4 张**、按 4 张计费 55 积分），而调用方以为只要 1 张。
    `metrics_extra.generateCount` 只是**埋点计数**、不是控制字段。

    位置照文生图：`gen_option` 与 `blend` **平级**（即 `abilities.gen_option`），
    参考仓自检原文 `component_list[0]["abilities"]["gen_option"]["gen_count"] == 2`。
    """
    from app.upstream.jimeng.client import build_blend_draft

    for n in (1, 2, 8):
        d = json.loads(build_blend_draft(prompt="x", image_uri="tos-cn-i-x/a", count=n))
        ab = d["component_list"][0]["abilities"]
        assert "blend" in ab and "gen_option" in ab, "两者应平级"
        assert ab["gen_option"]["gen_count"] == n
        assert ab["gen_option"]["generate_all"] is False


def test_i2i_passes_the_requested_count_to_blend(client, client_state,
                                                 fake_jimeng, fake_uploader):
    """`n` 必须真的流到 `blend()` —— 不是"收下了却不用"。"""
    fake_jimeng.states = [submitted_state(), ok_state(["https://cdn/a.png"])]
    _create(client, model="jimeng-i2i", prompt="两张合成", n=2,
            image=[_data_uri(_PNGS[0]), _data_uri(_PNGS[1])])
    client_state.coordinator.tick()

    call = fake_jimeng.of("blend")[0]
    assert call["count"] == 2, "请求的 n 没有传到 blend"
    assert len(call["image_uris"]) == 2, "两张垫图要一起带上"


def test_image_count_has_a_global_ceiling(client, service):
    """全局合理性上限：一次塞 6 张直接拒（真正的额度由能力声明决定）。

    ⚠️ `jimeng-i2i` 的按能力上限（4）与这里全局上限相等，所以"i2i 超限"这条
    **不可单独到达** —— 全局那道先拦。两道并存的用意：全局那道挡住"一次塞几百个
    URL"，免得在解析出能力之前就先做昂贵的图片校验；能力那道负责精确额度。
    """
    r = client.post(BASE, json={"model": "jimeng-i2i", "prompt": "x",
                                "image": [_data_uri(b) for b in _PNGS] * 2},
                    headers=AUTH)
    assert r.status_code == 400, r.text
    msg = r.json()["error"]["message"]
    assert "最多 4 张" in msg
    assert "jimeng-i2i" in msg, "要指路：只有 i2i 支持多张垫图"


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


def test_accept_wakes_the_coordinator(client, client_state, monkeypatch):
    """受理必须**叫醒**协调器，否则这条要白等一个 tick（默认最多 1s）。

    ⚠️ 刻意**不做时序断言** —— 那类断言必然偶发（本仓踩过 1% 概率的假失败，
    根因是整秒存储的 `updated_at` 撞上亚秒间隔）。改成钉住**接线**：
    受理路径确实调用了 `wake()`。接线断了才是会静默退化的那种缺陷
    （变慢但不会报错），而"快多少"由 `Coordinator.wake` 的实现保证。
    """
    calls: list[int] = []
    monkeypatch.setattr(client_state.coordinator, "wake",
                        lambda: calls.append(1))

    _create(client)

    assert calls == [1], "受理路径没有叫醒协调器 —— 会退化成等下一个 tick"


def test_wake_is_harmless_and_idempotent(service, settings):
    """`wake()` 纯属优化：多叫几次、或没人听，都不该有任何副作用。

    丢掉一次唤醒最多慢一个 tick —— "该派发谁"始终由库里的状态决定，
    不由"谁叫过它"决定。这条钉住这个性质，免得有人把状态塞进唤醒信号里。
    """
    from app.coordinator import Coordinator

    co = Coordinator(service, settings, owner="x")
    co.wake()
    co.wake()
    co.wake()
    assert co.stats()["running"] is False      # 没 start 过，叫醒也不该把它跑起来
    co.tick()                                  # 没配置上游 ⇒ 直接返回，不抛


def test_prewarm_fetches_model_config_and_upload_token_once(service, settings,
                                                            fake_jimeng):
    """启动预热要把两次**只读**往返提前做掉（模型表 + 上传 STS）。

    这两样每个进程只需一次，但原先都是"第一个任务才付"（实测 ~200ms + ~440ms）。
    它只影响重启后第一个任务的延迟 —— 而那恰好是部署完立刻试用的一刻。
    """
    from app.coordinator import Coordinator

    svc = settings.replace(jimeng_sessionid="s", jimeng_cookie="")
    obj = Service(svc, store=service.store, client=fake_jimeng,
                  uploader=fake_jimeng, cfg=None)
    # 用替身记账，避免真的构造上游往返
    calls: list[str] = []

    class _Cfg:
        def snapshot(self):
            calls.append("model_config")
            return object()

    class _Up:
        def token(self):
            calls.append("upload_token")
            return {"k": "v"}

    obj.cfg = _Cfg()          # type: ignore[assignment]
    obj.uploader = _Up()      # type: ignore[assignment]
    Coordinator(obj, svc, owner="x")._prewarm()

    assert calls == ["model_config", "upload_token"], "两样都要预热，且各一次"


def test_prewarm_failure_never_breaks_startup(service, settings):
    """预热是**优化**，绝不能成为启动的前提条件 —— 失败只告警。"""
    from app.coordinator import Coordinator

    svc = settings.replace(jimeng_sessionid="s", jimeng_cookie="")

    class _Boom:
        def snapshot(self):
            raise RuntimeError("boom")

        def token(self):
            raise RuntimeError("boom")

    obj = Service(svc, store=service.store, client=None, uploader=_Boom(), cfg=None)
    obj.cfg = _Boom()          # type: ignore[assignment]
    Coordinator(obj, svc, owner="x")._prewarm()      # 不该抛

    # 没配置上游时整段跳过（省得在无凭据部署里空跑）
    blank = settings.replace(jimeng_sessionid="", jimeng_cookie="")
    obj2 = Service(blank, store=service.store, client=None, uploader=None, cfg=None)
    Coordinator(obj2, blank, owner="x")._prewarm()   # 也不该抛
