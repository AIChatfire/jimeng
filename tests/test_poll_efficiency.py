#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轮询效率门禁 —— **每一条都能测出优化前后的差异**。

为什么单独一个文件：这类改动"改前改后功能都对"，普通功能用例全绿，
**看不出差别**。所以断言的是**上游查询次数**这个可观测的量化指标，
而不是"任务最终成功了"。

被守住的三件事：

| # | 性质 | 优化前 | 优化后 |
|---|---|---|---|
| 1 | 并发 N 个在途任务，一轮 tick 的上游查询次数 | **N** | **1**（`submit_ids` 批量） |
| 2 | 距上次推进不足 `JIMENG_POLL_INTERVAL` 时 | 照样查（配置无效） | **不查** |
| 3 | 已超时的任务 | 还去问一次上游 | **不问**，本地判死 |
"""
from __future__ import annotations

import time

from app.coordinator import Coordinator
from app.service import Service
from tests.conftest import ok_state, submitted_state


def _make(service: Service, settings, n: int, *, model: str = "jimeng-t2i"):
    """建 n 个任务并全部推进到 in_progress，返回 Coordinator。"""
    cred = service.credential_of("sk-test-key-0000000000000000")
    for i in range(n):
        service.create({"model": model, "prompt": f"p{i}", "image": []},
                       credential=cred)
    co = Coordinator(service, settings, owner="bench")
    return co


def _running(service: Service) -> int:
    return service.store.count_by_status("in_progress")


# ---------------------------------------------------------------------------
# 1. 批量轮询
# ---------------------------------------------------------------------------


def test_one_tick_polls_all_in_flight_tasks_in_a_single_request(
        service, settings, fake_jimeng):
    """**N 个在途任务 → 一轮 tick 只发 1 次上游查询**，且那一次带上全部 id。

    优化前这里是 N 次独立 `fetch`。这条断言就是那个差异。
    """
    fake_jimeng.states = [submitted_state()]        # 永不终态 ⇒ 一直占着 in_progress
    s4 = settings.replace(jm_concurrency=4, jimeng_poll_interval=0.0,
                          poll_grace=0.0)
    co = _make(service, s4, 4)
    co.tick()
    assert _running(service) == 4, "并发上限 4 应把 4 个都推上去"

    queries = fake_jimeng.of("fetch_many")
    assert len(queries) == 1, (
        f"一轮 tick 只该发 1 次上游查询，实得 {len(queries)} 次"
        f"（={len(fake_jimeng.of('fetch')) + len(queries)} 次单条查询）")
    assert len(queries[0]["ids"]) == 4, "那一次应该把 4 个 id 一起问"
    assert not fake_jimeng.of("fetch"), "不该再走单条查询路径"


def test_batching_scales_with_n_tasks(service, settings, fake_jimeng):
    """在途任务数从 2 涨到 3，上游查询次数**仍然是 1**（不随 N 增长）。"""
    for n in (2, 3):
        fake_jimeng.calls.clear()
        fake_jimeng.states = [submitted_state()]
        s = settings.replace(jm_concurrency=n, jimeng_poll_interval=0.0,
                             poll_grace=0.0)
        svc = Service(s, store=service.store, client=fake_jimeng,
                      uploader=service.uploader, cfg=service.cfg)
        co = _make(svc, s, n)
        co.tick()
        assert _running(svc) == n
        assert len(fake_jimeng.of("fetch_many")) == 1, f"n={n} 时不该按任务数放大"
        # 清干净，避免影响下一轮
        for rec in svc.store.list_by_status("in_progress"):
            svc.store.delete(rec.task_id)


# ---------------------------------------------------------------------------
# 2. 轮询间隔真的生效
# ---------------------------------------------------------------------------


def test_poll_interval_is_respected(service, settings, fake_jimeng):
    """`JIMENG_POLL_INTERVAL` 必须**真的**控制节奏 —— 此前它是"假配置"。

    它只被传给了 `JimengClient(poll_interval=…)`，而服务从不调
    `client.wait()`/`generate()` ⇒ 协调器每 tick（1s）就打一次上游，
    比配置值勤一倍，配置项等于没有效果。
    """
    fake_jimeng.states = [submitted_state()]
    s = settings.replace(jm_concurrency=2, poll_grace=0.0,
                         jimeng_poll_interval=60.0)      # 一分钟内只该问一次
    svc = Service(s, store=service.store, client=fake_jimeng,
                  uploader=service.uploader, cfg=service.cfg)
    co = _make(svc, s, 1)
    co.tick()                                            # 建任务；updated_at=now

    # 刚推进过 ⇒ 间隔未到 ⇒ 不该问上游
    assert not fake_jimeng.of("fetch_many"), \
        "距上次推进不足间隔就不该问上游（这正是配置该起的作用）"

    # 把"上次推进时间"推到两小时前 ⇒ 立刻应当问
    for rec in svc.store.list_by_status("in_progress"):
        svc.store.patch(rec.task_id, updated_at=int(time.time()) - 7200)
    co.tick()
    assert len(fake_jimeng.of("fetch_many")) == 1, "间隔已过就该问了"

    for rec in svc.store.list_by_status("in_progress"):
        svc.store.delete(rec.task_id)


def test_poll_due_predicate_matches_the_gates(service, settings):
    """`poll_due` 不产生副作用，供运维观测 —— 它的判断必须与三道门一致。

    ⚠️ 用一个**真实量级**的间隔（60s）：夹具默认 0.01s 太短，
    而 `updated_at` 是取整的秒 ⇒ "刚刚更新过"也会被判成"已到期"，
    断言就失去意义了。
    """
    s = settings.replace(jimeng_poll_interval=60.0)
    svc = Service(s, store=service.store, client=service.client,
                  uploader=service.uploader, cfg=service.cfg)
    store = svc.store
    rec = svc.create({"model": "jimeng-t2i", "prompt": "x", "image": []},
                     credential=svc.credential_of(None))
    now = time.time()

    # ① 还没建任务（没有 upstream_submit_id）⇒ 压根无从问起
    assert svc.poll_due(rec, now=now) is False, "没 dispatch 过就不该被轮询"

    # ② 刚建完任务 ⇒ 距上次推进不足间隔
    rec = store.patch(rec.task_id, status="in_progress",
                      upstream_submit_id="sid", started_at=int(now) - 10,
                      updated_at=int(now)) or rec
    assert svc.poll_due(rec, now=now) is False, "刚更新过 ⇒ 未到间隔"

    # ③ 把上次推进时间推到两小时前 ⇒ 该问了
    rec = store.patch(rec.task_id, updated_at=int(now) - 7200) or rec
    assert svc.poll_due(rec, now=now) is True

    # ④ 已超时：即使间隔已过也不该问（本地判死更便宜）
    rec = store.patch(rec.task_id, started_at=int(now) - 10 ** 6) or rec
    assert svc.poll_due(rec, now=now) is False, "已超时的任务不该再去问上游"
    store.delete(rec.task_id)


# ---------------------------------------------------------------------------
# 3. 超时任务不发上游请求
# ---------------------------------------------------------------------------


def test_expired_task_is_failed_without_touching_upstream(service, settings,
                                                          fake_jimeng):
    """看门狗判超时**不发上游请求** —— 已经太久没结果，再打一次也白打。

    ⚠️ 注意 `task_timeout` 是"总时长"而非"距上次推进"。这里把它设成负数，
    等价于"任何在途任务都已超时"。
    """
    fake_jimeng.states = [submitted_state()]
    s = settings.replace(task_timeout=-1.0, poll_grace=0.0,
                         jimeng_poll_interval=0.0)
    svc = Service(s, store=service.store, client=fake_jimeng,
                  uploader=service.uploader, cfg=service.cfg)
    co = _make(svc, s, 1)
    co.tick()                                   # 建任务（同轮轮询会判超时）

    recs = svc.store.list_by_status("failure")
    assert recs, "超时任务应被判 failure"
    assert recs[0].error["code"] == "upstream_timeout"
    assert not fake_jimeng.of("fetch_many"), "判超时不该产生上游查询"
    for rec in recs:
        svc.store.delete(rec.task_id)


# ---------------------------------------------------------------------------
# 4. 批量结果按 id 正确落位（别把 A 的结果安到 B 上）
# ---------------------------------------------------------------------------


def test_batched_results_are_matched_by_submit_id(service, settings,
                                                  fake_jimeng):
    """合并查询后必须**按 submit_id 对号入座**。

    批量最容易出的错就是"把结果按顺序贴回去" —— 那样一旦某个 id 上游没返回，
    后面所有任务的结果都会错位一格，而产出看起来完全正常。
    """
    fake_jimeng.states = [submitted_state()]
    s = settings.replace(jm_concurrency=3, jimeng_poll_interval=0.0,
                         poll_grace=0.0)
    svc = Service(s, store=service.store, client=fake_jimeng,
                  uploader=service.uploader, cfg=service.cfg)
    co = _make(svc, s, 3)
    co.tick()
    recs = svc.store.list_by_status("in_progress")
    assert len({r.upstream_submit_id for r in recs}) == 3

    # 让假上游只对其中一个 id 给出「成功」，其余仍非终态
    target = recs[0]
    def routing(ids):
        out = {}
        for i in ids:
            out[i] = ok_state(["https://cdn/only-this.png"]) \
                if i == target.upstream_submit_id else submitted_state()
        return out
    fake_jimeng.fetch_many = routing       # type: ignore[method-assign]

    for r in recs:
        svc.store.patch(r.task_id, updated_at=int(time.time()) - 7200)
    co.tick()

    done = svc.store.get(target.task_id)
    assert done.status == "success"
    assert [i["url"] for i in done.images] == ["https://cdn/only-this.png"]
    others = [r for r in recs if r.task_id != target.task_id]
    for r in others:
        assert svc.store.get(r.task_id).status == "in_progress", \
            "别人的结果不许被安到它头上"
    for r in recs:
        svc.store.delete(r.task_id)


def test_real_client_sends_one_request_with_all_ids():
    """**真客户端**层面也必须是"一次请求带全部 id"。

    上面几条用的是假上游（测的是编排层），这一条用 `httpx.MockTransport` 拦住
    真实 `JimengClient` 的出口，断言它**只发一个 POST**、且 body 里
    `submit_ids` 是复数、含全部 id —— 否则编排层再对也没用。
    """
    import json

    import httpx

    from app.upstream.jimeng.client import PATH_HISTORY, JimengClient

    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ret": "0", "data": {"a": {"task": {"status": 20}}}})

    c = JimengClient(sessionid="s", transport=httpx.MockTransport(handler))
    out = c.fetch_many(["a", "b", "c"])
    assert len(seen) == 1, f"应只发 1 个请求，实得 {len(seen)}"
    assert PATH_HISTORY in str(seen[0].url)
    body = json.loads(seen[0].content)
    assert body["submit_ids"] == ["a", "b", "c"], body
    assert set(out) == {"a", "b", "c"}, "每个 id 都要有返回（没落库的给 init 态）"
    assert out["b"].status_name == "init", "上游没回的那个 id 要退化成本地 init 态"

    # 空输入不该发请求
    seen.clear()
    assert c.fetch_many([]) == {}
    assert not seen
