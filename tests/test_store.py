#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存储门禁（**打到真 PostgreSQL**，每个用例一个独立 schema）。

最重要的一条是 `test_cross_instance_visibility` —— 它专门钉住"任务必须跨实例可读"。
把任务库换成进程内 dict（或换实例后指向别处）时，**别的用例全绿，只有这条会红**，
因为那种缺陷只在**重启**那一刻暴露，平时完全看不出来。
"""
from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.store import TaskStore, build_engine, mask_dsn


def _rec(**kw) -> dict:
    now = int(time.time())
    base = dict(
        task_id=f"jimeng_{uuid.uuid4().hex}",
        credential_id="cred-a",
        model="jimeng-t2i",
        cap_key="jimeng:t2i",
        status="queued",
        prompt="a cat",
        image_refs=[],
        size="2048x2048",
        n=1,
        created_at=now,
        updated_at=now,
    )
    base.update(kw)
    return base


def mk(store: TaskStore, **kw):
    from app.store import TaskRecord

    return store.put(TaskRecord(**_rec(**kw)))


# ---------------------------------------------------------------------------
# 引擎配置
# ---------------------------------------------------------------------------


def test_engine_rejects_non_postgres_targets():
    """**写错 DSN 必须直接报错**，绝不静默退回别的后端。

    静默降级会让"任务不丢"在没人注意的时候失效 —— 事故级。
    """
    for bad in ("sqlite:///tmp/x.db", ":memory:", "/tmp/tasks.db", "mysql://a@b/c"):
        with pytest.raises(ValueError) as e:
            build_engine(bad)
        assert "PostgreSQL DSN" in str(e.value)


def test_bare_postgres_scheme_gets_the_psycopg2_driver():
    eng = build_engine("postgresql://u:p@127.0.0.1:5432/x")
    assert eng.url.drivername == "postgresql+psycopg2"


def test_legacy_postgres_scheme_is_upgraded():
    """`postgres://` 是 Heroku 时代的老写法，很多云厂商还在给 —— 照收。"""
    eng = build_engine("postgres://u:p@127.0.0.1:5432/x")
    assert eng.url.drivername == "postgresql+psycopg2"


def test_dsn_password_is_masked_in_stats():
    """DSN 会进 /stats 与启动日志 ⇒ **必须先掩码**。这是"凭据不上报"在存储层的落点。"""
    assert mask_dsn("postgresql+psycopg2://jimeng:s3cret@db:5432/j") == \
        "postgresql+psycopg2://jimeng:***@db:5432/j"
    assert mask_dsn("postgresql://u@db/j") == "postgresql://u@db/j", "无密码时不该改"
    assert mask_dsn("") == ""


def test_json_columns_are_jsonb_on_postgres():
    """PG 上 JSON 列必须是 **JSONB**（可索引、可按 key 查）。

    断言走**建表 DDL**，而不是比 `dialect_impl(...).__class__.__name__` ——
    后者返回的是 SQLAlchemy 的内部类名 `_PGJSONB` 而不是 `JSONB`（第一版就这么写错、
    被这条断言自己抓了出来）。DDL 才是真正发给数据库的东西。
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from app.store import TaskRecord

    ddl = str(CreateTable(TaskRecord.__table__).compile(
        dialect=postgresql.dialect()))
    assert "JSONB" in ddl, ddl
    assert "images JSONB" in ddl or "degradations JSONB" in ddl, ddl


# ---------------------------------------------------------------------------
# 🔴 核心：跨实例可读
# ---------------------------------------------------------------------------


def test_cross_instance_visibility(db_dsn):
    """**新实例必须读到旧实例写的数据。**

    这是"任务不丢"的唯一自动化证据。上一版用进程内 dict 时，
    为加载一处代码修正重启服务，正在轮询的任务立刻消失 ——
    而所有别的用例照样全绿。
    """
    a = TaskStore(db_dsn)
    rec = mk(a, prompt="跨实例可见")

    b = TaskStore(db_dsn)  # 模拟"重启后的新实例"
    got = b.get(rec.task_id)
    assert got is not None, "换实例后任务读不到 —— 持久化失效了"
    assert got.prompt == "跨实例可见"
    assert got.status == "queued"


def test_ping_true_on_reachable_db(db_dsn):
    assert TaskStore(db_dsn).ping() is True


def test_constructing_against_unreachable_db_fails_loudly():
    """任务库连不上就必须**在构造期就报错**，不能装作能服务。

    刻意不做「连不上就退回内存/别的后端」的降级 —— 那会让"任务不丢"
    在没人注意的时候悄悄失效。失败点在 `TaskStore(...)`（`create_all` 就要连库），
    而不是等到第一次 ping：**早失败比晚失败便宜得多**。
    """
    dead = build_engine("postgresql+psycopg2://u:p@127.0.0.1:1/nope",
                        connect_timeout=1)
    with pytest.raises(SQLAlchemyError):
        TaskStore(dead)


def test_ping_returns_false_instead_of_raising(db_dsn, monkeypatch):
    """就绪探针不能把探针自己打挂 —— 连不上要返回 False。"""
    store = TaskStore(db_dsn)

    def boom():
        raise OperationalError("SELECT 1", None, Exception("db down"))

    monkeypatch.setattr(store.engine, "connect", boom)
    assert store.ping() is False


# ---------------------------------------------------------------------------
# 凭证隔离
# ---------------------------------------------------------------------------


def test_scoped_get_hides_other_credentials_tasks(store):
    rec = mk(store, credential_id="cred-a")
    assert store.get_scoped(rec.task_id, "cred-a") is not None
    assert store.get_scoped(rec.task_id, "cred-b") is None, "跨凭证必须取不到"
    assert store.get_scoped("jimeng_nope", "cred-a") is None


def test_list_recent_is_filtered_by_credential(store):
    """列表只按服务过滤会把同渠道另一把钥匙的任务列给对方。"""
    mk(store, credential_id="cred-a")
    mk(store, credential_id="cred-a")
    mk(store, credential_id="cred-b")
    assert len(store.list_recent(credential_id="cred-a")) == 2
    assert len(store.list_recent(credential_id="cred-b")) == 1
    assert len(store.list_recent()) == 3


# ---------------------------------------------------------------------------
# patch / 状态计数
# ---------------------------------------------------------------------------


def test_patch_updates_only_given_fields(store):
    rec = mk(store)
    out = store.patch(rec.task_id, status="in_progress", upstream_submit_id="sid-1")
    assert out is not None
    assert out.status == "in_progress"
    assert out.upstream_submit_id == "sid-1"
    assert out.prompt == "a cat", "未指定的字段不该被动"


def test_patch_rejects_unknown_field(store):
    rec = mk(store)
    with pytest.raises(ValueError):
        store.patch(rec.task_id, nope=1)


def test_patch_missing_task_returns_none(store):
    assert store.patch("jimeng_nope", status="success") is None


def test_json_columns_roundtrip(store):
    rec = mk(store,
             images=[{"url": "http://x/1.png", "width": 2048, "height": 2048}],
             degradations=["吸附 5->4"], error={"message": "boom"})
    got = store.get(rec.task_id)
    assert got is not None
    assert got.images[0]["url"] == "http://x/1.png"
    assert got.degradations == ["吸附 5->4"]
    assert got.error == {"message": "boom"}
    # PG 上 JSONB **保持原生类型**：读回来还是 int，不是字符串
    assert got.images[0]["width"] == 2048 and isinstance(got.images[0]["width"], int)


def test_none_json_roundtrips_as_none(store):
    rec = mk(store)
    assert store.get(rec.task_id).error is None


def test_count_active_covers_both_non_terminal_states(store):
    mk(store, status="queued")
    mk(store, status="in_progress")
    mk(store, status="success")
    assert store.count_active() == 2
    assert store.count_by_status("in_progress") == 1
    assert store.count() == 3


def test_list_by_status_orders_oldest_first(store):
    """最早排队的最先被处理 —— 否则长队尾部的任务会被饿死。"""
    now = int(time.time())
    mk(store, prompt="newer", created_at=now)
    mk(store, prompt="older", created_at=now - 100)
    rows = store.list_by_status("queued", order="oldest")
    assert [r.prompt for r in rows] == ["older", "newer"]


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


def test_prune_removes_only_expired_terminal_tasks(store):
    old = int(time.time()) - 30 * 86400
    mk(store, status="success", created_at=old, updated_at=old)
    mk(store, status="queued", created_at=old, updated_at=old)
    mk(store, status="success", created_at=int(time.time()))

    assert store.prune(retention_days=7) == 1
    # 非终态任务**一律不删** —— 删掉在跑的任务等于让它凭空消失
    assert store.count_by_status("queued") == 1
    assert store.count() == 2


def test_stale_active_finds_stuck_tasks(store):
    mk(store, status="in_progress", updated_at=int(time.time()) - 999)
    mk(store, status="in_progress", updated_at=int(time.time()))
    assert len(store.stale_active(older_than_s=100)) == 1


# ---------------------------------------------------------------------------
# 租约（防重复提交 = 防重复计费）
# ---------------------------------------------------------------------------


def test_lease_is_exclusive_then_stealable_after_expiry(store):
    """⚠️ `seconds` 是"**要租多久**"，不是"租期到什么时候"。

    第一版我把它当成了后者，于是拿一个**有效**租约去断言"别人可以接手"，
    当然失败 —— 测试写错了。
    """
    assert store.acquire_lease("coordinator", "w1", 5.0) is True
    assert store.lease_owner("coordinator") == "w1"

    assert store.acquire_lease("coordinator", "w2", 5.0) is False, "别人持锁时抢不到"
    assert store.lease_owner("coordinator") == "w1"

    assert store.acquire_lease("coordinator", "w1", 5.0) is True, "持锁者自己续租可以"

    # 让**当前持锁者**把一个已过期的租约写进去（owner 相同 ⇒ 允许覆盖，until 落在过去）
    assert store.acquire_lease("coordinator", "w1", -1.0) is True
    assert store.lease_owner("coordinator") is None, "过期即视为无主"

    assert store.acquire_lease("coordinator", "w3", 5.0) is True, "于是别人能接手"
    assert store.lease_owner("coordinator") == "w3"


def test_release_only_by_owner(store):
    store.acquire_lease("coordinator", "w1", 30.0)
    store.release_lease("coordinator", "w2")
    assert store.lease_owner("coordinator") == "w1", "别人不能替你释放"
    store.release_lease("coordinator", "w1")
    assert store.lease_owner("coordinator") is None


def test_expired_lease_reads_as_free(store):
    store.acquire_lease("coordinator", "w1", -1.0)
    assert store.lease_owner("coordinator") is None


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


def test_meta_survives_across_instances(db_dsn):
    """凭证指纹密钥必须跨重启稳定 —— 否则历史任务会突然"不属于任何人"。"""
    TaskStore(db_dsn).set_meta("k", "v")
    assert TaskStore(db_dsn).get_meta("k") == "v"
    assert TaskStore(db_dsn).get_meta("missing") is None


def test_delete(store):
    rec = mk(store)
    assert store.delete(rec.task_id) is True
    assert store.delete(rec.task_id) is False
    assert store.get(rec.task_id) is None


# ---------------------------------------------------------------------------
# 状态大小写归一化 —— 守的是「任务不会静默冻结」
# ---------------------------------------------------------------------------


def _legacy(status: str) -> str:
    """把当前的小写状态"还原"成历史大写形态（用来模拟旧数据）。

    ⚠️ 刻意用 `.upper()` 而不是写大写**字面量**：本仓做过一次「状态全部改小写」
    的全局替换，字面量会被那个替换顺手改掉 —— 于是测试**不再模拟旧数据**，
    却依然"看起来合理"（我第一版就这么被自己的替换误伤了，两条新测试一起变红）。
    这里的大写是**语义要求**、不是笔误，所以要写成替换碰不到的形态。
    """
    return status.upper()


def test_legacy_uppercase_status_is_normalized_on_construction(store, db_dsn):
    """🔴 历史**大写**状态行必须在 `TaskStore` 构造时被归一化成小写。

    这条验的**不是**"数据整齐好看"，而是"任务不会凭空消失"：

    状态字面量改成小写后，代码里的比较与 `list_by_status("queued")` 都变了，
    而库里的历史行若还是大写，就永远匹配不上任何查询 ——
    **不报错、不前进**，协调器看起来一切正常，而任务全停住。
    这是最典型的"静默冻结"。

    所以归一化在构造时无条件跑，而不是指望运维记得手动洗数据。
    """
    legacy = mk(store, status=_legacy("queued"))     # 模拟历史遗留行
    assert [r.task_id for r in store.list_by_status("queued")] == [], \
        "归一化之前，小写查询确实找不到它 —— 这正是那个故障本身"

    TaskStore(db_dsn)                            # 新构造一次 ⇒ 触发归一化

    assert legacy.task_id in [r.task_id for r in store.list_by_status("queued")], \
        "归一化之后必须能被查到，否则任务静默冻结"
    assert store.get(legacy.task_id).status == "queued"
    # 其它状态也一样（不只 queued 这一个）
    done = mk(store, status=_legacy("success"))
    TaskStore(db_dsn)
    assert store.get(done.task_id).status == "success"


def test_status_normalization_is_idempotent(store):
    """幂等：第二次跑改 0 行 —— 每次构造都会跑它，不能越跑越贵。"""
    mk(store, status=_legacy("in_progress"))
    assert store.normalize_status_case() == 1
    assert store.normalize_status_case() == 0
    assert store.normalize_status_case() == 0
    assert [r.task_id for r in store.list_by_status("in_progress")]


def test_normalization_does_not_touch_already_lowercase_rows(store):
    """已经小写的行不该被"改"一遍（否则每次启动都产生无意义的写放大）。"""
    mk(store, status="queued")
    mk(store, status="success")
    assert store.normalize_status_case() == 0
