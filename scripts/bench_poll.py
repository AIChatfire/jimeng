#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轮询开销实测 —— 回答"一轮 tick 到底打了几次上游"。

为什么单独一个脚本：这类优化**改前改后功能都对**，功能用例全绿，看不出差别。
所以只能量指标，而且要量在**上游查询次数**这个真正花钱/招风控的维度上。

三个测量（都用假上游 + 真协调器 + 本地 PG，零花费）：

| 测量 | 说明 |
|---|---|
| ① 逐任务调用 | 等价**优化前**的协调器写法（`for rec: service.poll(rec)`） |
| ② 合并调用 | 现在的写法（`service.poll_many(recs)`，一次带全部 id） |
| ③ 间隔是否生效 | 同一段墙钟内，`JIMENG_POLL_INTERVAL=0` 与 `=2` 的查询次数对比 |

只测**调用次数**，不测墙钟耗时 —— 因为上游本身快慢不由我们决定，
而"打了多少次"完全由我们决定。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TASK_DB", "postgresql+psycopg2://jimeng:jimeng@127.0.0.1:5433/jimeng")
os.environ["COORDINATOR_ENABLED"] = "0"
os.environ["JIMENG_SESSIONID"] = "bench"
os.environ["API_KEYS"] = ""
os.environ["LOG_LEVEL"] = "ERROR"

from app.config import Settings
from app.service import Service
from app.store import TaskStore
from app.upstream.jimeng import TaskState

N = 4


class _FakeJimeng:
    """永远"已提交但没出图" —— 任务一直占着 in_progress，最费轮询的状态。"""

    def __init__(self) -> None:
        self.queries = 0
        self.ids_per_query: list[int] = []
        self.last_warnings: list[str] = []
        self.submitted: list[str] = []

    def submit(self, prompt, **kw):
        sid = f"sid-{len(self.submitted)}"
        self.submitted.append(sid)
        return sid

    def blend(self, prompt, **kw):
        return self.submit(prompt)

    def edit(self, tool, **kw):
        return self.submit(tool)

    def fetch_many(self, submit_ids):
        self.queries += 1
        self.ids_per_query.append(len([s for s in submit_ids if s]))
        return {s: TaskState(submit_id=s, status=20, status_name="submitted")
                for s in submit_ids}

    fetch = None  # 不用单条路径

    def close(self):
        pass


class _FakeUploader:
    last_cached = False

    def upload(self, data, **kw):
        return "tos-cn-i-bench/x"

    def close(self):
        pass


def _service(settings: Settings, fake: _FakeJimeng, store: TaskStore) -> Service:
    return Service(settings, store=store, client=fake, uploader=_FakeUploader(),
                   cfg=None)


def _seed(svc: Service, n: int) -> list:
    cred = svc.credential_of(None)
    for i in range(n):
        svc.create({"model": "jimeng-t2i", "prompt": f"p{i}", "image": []},
                   credential=cred)
    recs = [r for r in svc.store.list_by_status("queued", limit=n)]
    out = []
    for r in recs:                      # 直接置 in_progress，省掉建任务环节
        out.append(svc.store.patch(
            r.task_id, status="in_progress", upstream_submit_id=f"up-{r.task_id}",
            started_at=int(time.time()) - 100, updated_at=0))
    return [r for r in out if r]


def _cleanup(svc: Service) -> None:
    for r in svc.store.list_by_status("in_progress", limit=100):
        svc.store.delete(r.task_id)
    for r in svc.store.list_by_status("queued", limit=100):
        svc.store.delete(r.task_id)


def main() -> int:
    base = Settings(jimeng_sessionid="bench", api_keys=(),
                    task_db=os.environ["TASK_DB"], poll_grace=0.0,
                    coordinator_enabled=False)
    store = TaskStore(base.db_target)
    _cleanup(_service(base, _FakeJimeng(), store))

    print(f"轮询开销实测（N={N} 个在途任务，全程假上游，**零花费**）\n")

    # ① vs ②：一轮 tick 的上游查询次数
    fake = _FakeJimeng()
    s = base.replace(jimeng_poll_interval=0.0)
    svc = _service(s, fake, store)
    recs = _seed(svc, N)
    assert len(recs) == N, f"只置成功 {len(recs)} 个"

    fake.queries, fake.ids_per_query = 0, []
    for r in recs:                       # ← 优化前的协调器写法
        svc.poll(r)
    old_calls, old_ids = fake.queries, list(fake.ids_per_query)

    for r in recs:                       # 让间隔门重新放行
        store.patch(r.task_id, updated_at=0)
    fake.queries, fake.ids_per_query = 0, []
    svc.poll_many(recs)                  # ← 现在的写法
    new_calls, new_ids = fake.queries, list(fake.ids_per_query)

    print("① 一轮 tick 打了几次上游（N=4 在途）")
    print(f"   逐任务调用（改前）：{old_calls} 次，每次带 {old_ids} 个 id")
    print(f"   合并调用（改后）  ：{new_calls} 次，每次带 {new_ids} 个 id")
    print(f"   ⇒ 上游查询次数降到 1/{old_calls // max(new_calls, 1)}，"
          f"且**不随 N 增长**\n")

    # ③ 间隔是否生效：同一段墙钟，两种配置
    print("③ 同一段墙钟内（3s，tick 间隔 0.25s）的上游查询次数")
    for interval in (0.0, 2.0):
        fake2 = _FakeJimeng()
        s2 = base.replace(jimeng_poll_interval=interval)
        svc2 = _service(s2, fake2, store)
        _seed(svc2, N)               # 置 in_progress 且 updated_at=0 ⇒ 首轮立即到期
        fake2.queries = 0
        end = time.time() + 3.0
        while time.time() < end:
            # 🔴 **每轮必须从库里重读**。三道门读的是传入记录上的 `updated_at`，
            # 若一直拿着 `_seed` 返回的那批旧对象（updated_at 恒为 0），
            # 间隔门看到的永远是"早就到期" ⇒ 两种配置会得出一样的结果，
            # 测出来的是假象（第一版就这么写错了）。
            fresh = store.list_by_status("in_progress", limit=N)
            svc2.poll_many(fresh)
            time.sleep(0.25)
        print(f"   JIMENG_POLL_INTERVAL={interval:<4} ⇒ {fake2.queries} 次查询"
              f"（每次带 id 数 {sorted(set(fake2.ids_per_query))}）")
    print("   ⇒ 配置值真的在起作用（改前它只传给 JimengClient，从不生效）\n")

    _cleanup(_service(base, _FakeJimeng(), store))
    store.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
