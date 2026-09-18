#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可观测性门禁。

埋点刻意"绝不抛异常"，所以**写错了也只会安静地什么都不记**。
⇒ 断言不能只问"没报错"，必须问"真的记下了 / 真的没改写 / 真的没上报"。

这些用例**完全离线**：不装 SDK、不连网络、不读 token 也能跑
（用一个假 `logfire` 模块照抄真 SDK 的签名，见 §5）。
"""
from __future__ import annotations

import contextlib
import sys

import pytest
from loguru import logger

from app.config import Settings
from app.observability import (
    LOG_PARSE_LIMIT,
    OBS,
    PROBE_PATHS,
    Observability,
    excluded_urls,
    is_probe_path,
    parsed_json,
    prepare_attrs,
    should_log_path,
)

# ---------------------------------------------------------------------------
# 假 SDK（签名照抄真 logfire 5.0）
# ---------------------------------------------------------------------------


class FakeLogfire:
    """假 logfire。**能记录调用参数** —— 只断言"没报错"什么也测不到。"""

    def __init__(self) -> None:
        self.configure_kw: dict | None = None
        self.loguru_handler_calls = 0
        self.spans: list[tuple[str, dict]] = []
        self.flush_calls = 0

    def configure(self, **kw):
        self.configure_kw = kw
        return None

    def loguru_handler(self):
        self.loguru_handler_calls += 1
        return {"sink": lambda _m: None, "format": "{message}"}

    @contextlib.contextmanager
    def span(self, name, **kw):
        self.spans.append((name, kw))
        yield self

    def set_attribute(self, _k, _v):
        return None

    def force_flush(self, timeout_millis: int = 3000) -> bool:
        self.flush_calls += 1
        return True


@pytest.fixture
def fake_lf(monkeypatch) -> FakeLogfire:
    fake = FakeLogfire()
    monkeypatch.setitem(sys.modules, "logfire", fake)
    return fake


# ---------------------------------------------------------------------------
# 口径：不脱敏（2026-09-19 定）
# ---------------------------------------------------------------------------


def test_scrubbing_is_off_by_default(fake_lf):
    """观测面**不脱敏** ⇒ `configure(scrubbing=False)`。

    打开 SDK 自带 scrubber 会让即梦的 TOS 预签名产物 URL
    （必含 `X-Tos-Credential=`）整条被打成 `[Scrubbed due to 'Credential']`，
    每一条结果 URL 都不可读 —— 比不脱敏更糟。
    """
    obs = Observability()
    st = obs.init(Settings(otel_service_name="s", otel_token="tok"))
    assert fake_lf.configure_kw is not None
    assert fake_lf.configure_kw["scrubbing"] is False
    assert st["scrubbing"] is False
    assert st["egress"] == "otlp"


def test_scrubbing_can_be_turned_on_explicitly(fake_lf):
    """想对外分享 trace 时可以开 —— 但它只影响 SDK 自带 scrubber，本模块仍不改写。"""
    obs = Observability()
    st = obs.init(Settings(otel_service_name="s", otel_token="tok",
                           otel_scrubbing=True))
    assert fake_lf.configure_kw["scrubbing"] is True
    assert st["scrubbing"] is True


def test_attributes_are_never_rewritten():
    """🔴 本模块**不做任何事后改写**：不改名、不删键、不替换值。

    事后改写会让人对着面板排查一个上游并不存在的字段 ——
    排障时被自己骗，比没有埋点更坏。
    """
    attrs = {
        "authorization": "Bearer sk-abcdefghijklmnop",
        "sessionid": "plain-session-value",
        "cookie": "sessionid=abc; other=1",
        "token": "t-123456",
        "task_id": "jimeng_deadbeef",
        "http_status": 200,
    }
    assert prepare_attrs(attrs) == attrs


def test_shrink_keeps_key_names_and_marks_truncation():
    """瘦身只做体量控制，**键名保真**，截断留显式标记而不是悄悄改值。"""
    out = prepare_attrs({"body": {"a": "x" * 900, "b": [1, 2, 3]}})
    assert set(out["body"]) == {"a", "b"}, "键名不许被改写"
    assert out["body"]["a"].endswith("…")
    assert out["body"]["b"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 凭据不进属性（**另一条**纪律，靠实现约束而非事后过滤）
# ---------------------------------------------------------------------------


def test_upstream_event_carries_no_credentials_and_drops_the_query():
    """上游埋点只给 pathname：query 丢掉、头与 cookie 从不传入。

    这是"密钥不上报"的实现方式。⚠️ 新增埋点时不得把凭据塞进属性 ——
    本用例就是那条约束的守门人。
    """
    rows: list[dict] = []
    sid = logger.add(lambda m: rows.append(dict(m.record["extra"])),
                     level="INFO", format="{message}")
    try:
        OBS.upstream(method="POST", path="/mweb/v1/aigc_draft/generate",
                     status=200, duration_ms=12.5,
                     body={"draft_content": "{}"},
                     response={"ret": "0", "data": {}})
    finally:
        logger.remove(sid)

    assert rows, "上游埋点没有产生记录"
    row = rows[-1]
    assert row["http_path"] == "/mweb/v1/aigc_draft/generate", "不许带 query"
    assert "upstream_request_json" in row and "upstream_response_json" in row
    for banned in ("authorization", "cookie", "sessionid", "api_key", "token"):
        assert banned not in row, f"埋点里出现了凭据字段 {banned}"


def test_span_attrs_are_not_scrubbed_either(fake_lf):
    """span 属性同样原样上报（口径一致，不搞两套）。"""
    obs = Observability()
    obs.init(Settings(otel_service_name="s", otel_token="tok"))
    with obs.span("task.dispatched", sessionid="raw-value", task_id="t1"):
        pass
    name, kw = fake_lf.spans[-1]
    assert name == "task.dispatched"
    assert kw["sessionid"] == "raw-value"
    assert kw["task_id"] == "t1"


# ---------------------------------------------------------------------------
# 探活路径：span 与日志两条通道同源
# ---------------------------------------------------------------------------


def test_probe_paths_are_exact_matches_not_substrings():
    assert is_probe_path("/healthz")
    assert not is_probe_path("/healthzz"), "子串匹配会让 /healthzz 被误伤"
    assert not is_probe_path("/healthz/extra"), "未锚定会吃掉子路径"
    assert not is_probe_path("/async/v1/images/generations")
    assert not should_log_path("/healthz")
    assert should_log_path("/async/v1/images/generations")


def test_root_special_case_does_not_match_everything():
    """`"/"` 若被当成"前缀匹配"，就会命中每一个路径 —— 静默关掉全站。"""
    from app.observability import _path_matches

    assert _path_matches("/", "/")
    assert not _path_matches("/healthz", "/")
    assert not _path_matches("/async/v1/x", "/")


def test_trailing_slash_entry_means_subtree():
    from app.observability import _path_matches

    assert _path_matches("/metrics/x", "/metrics/")
    assert not _path_matches("/metrics", "/metrics/")


def test_excluded_urls_is_fully_anchored():
    """🔴 `excluded_urls` 是正则且上游用 `re.search`（**子串匹配**）。

    写 `"/"` 会命中每一个 URL（任何 URL 都含 `/`）⇒ 全站追踪被静默关掉，
    比不排除更糟。所以必须全锚定。

    ⚠️ 这是**逗号分隔的多个正则**，不是一个正则 —— 逐条编译。
    在整串上 `re.compile` 会把它当成含逗号的字面量，于是"全不匹配"，
    看起来像实现坏了（本用例第一版就犯了这个错）。
    """
    import re

    urls = excluded_urls(PROBE_PATHS)
    parts = urls.split(",")
    assert len(parts) == len(PROBE_PATHS), "路径表里每条都该生成一条正则"
    for part in parts:
        assert part.startswith("^") and part.endswith("$"), f"未锚定：{part}"

    def hits(u: str) -> bool:
        return any(re.compile(p).search(u) for p in parts)

    assert hits("http://127.0.0.1:8200/healthz")
    assert hits("http://127.0.0.1:8200/readyz")
    assert not hits("http://127.0.0.1:8200/healthzz")
    assert not hits("http://127.0.0.1:8200/async/v1/images/generations")
    assert not hits("http://127.0.0.1:8200/healthz?deep=1"), "URL 不含 query"


def test_excluded_urls_matches_upstream_semantics():
    """用上游**同一个**解析器判定语义，别自己重写正则匹配。"""
    from opentelemetry.util.http import parse_excluded_urls

    parsed = parse_excluded_urls(excluded_urls(PROBE_PATHS))
    assert parsed.url_disabled("http://h:1/healthz") is True
    assert parsed.url_disabled("http://h:1/readyz") is True
    assert parsed.url_disabled("http://h:1/async/v1/images/generations") is False


# ---------------------------------------------------------------------------
# 结构化属性
# ---------------------------------------------------------------------------


def test_parsed_json_makes_bodies_queryable():
    assert parsed_json('{"ret":"0","data":{"n":2}}') == {"ret": "0",
                                                         "data": {"n": 2}}


def test_parsed_json_never_raises_and_returns_none_on_junk():
    assert parsed_json(b"not json") is None
    assert parsed_json(b"") is None
    assert parsed_json(None) is None
    assert parsed_json(b"\xff\xfe\x00") is None
    assert parsed_json(12345) is None


def test_parsed_json_rejects_non_object_toplevel():
    """顶层是数组/标量时没有可命名的字段，展开无从谈起 ⇒ 返回 None。"""
    assert parsed_json("[1,2,3]") is None
    assert parsed_json('"just a string"') is None


def test_parsed_json_skips_oversized_payloads():
    big = '{"a":"' + "x" * (LOG_PARSE_LIMIT + 10) + '"}'
    assert parsed_json(big) is None, "超液体不解析（解析成本与体量同阶）"


def test_parsed_json_marks_inline_base64():
    node = parsed_json('{"image":"data:image/png;base64,AAAA"}')
    assert node is not None
    assert node["image"].startswith("<data-uri"), node


# ---------------------------------------------------------------------------
# 不阻塞：未初始化 / 未装 SDK 时全 no-op
# ---------------------------------------------------------------------------


def test_uninitialised_observability_is_a_safe_noop():
    obs = Observability()
    assert obs.enabled is False
    assert obs.status()["reason"]
    with obs.span("x", task_id="t") as sp:
        assert sp is None
    obs.info("hello", a=1)
    obs.warning("warn")
    obs.error("err")
    obs.upstream(method="POST", path="/p", status=200, duration_ms=1.0)
    assert obs.flush() is False


def test_init_without_a_token_degrades_visibly(fake_lf, capsys):
    """没配 token ⇒ 只本地留 span，且**必须说清原因**（否则人只看到空面板）。"""
    obs = Observability()
    st = obs.init(Settings(otel_service_name="jimeng-service", otel_token=""))
    assert st["egress"] == "none"
    assert st["reason"] and "LOGFIRE_TOKEN" in st["reason"]
    assert "未配置" in capsys.readouterr().err


def test_missing_sdk_degrades_to_noop(monkeypatch, capsys):
    """SDK 没装也得能起服务 —— 观测反噬业务是最不该发生的。"""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "logfire":
            raise ImportError("no logfire")
        return real_import(name, *a, **kw)

    monkeypatch.delitem(sys.modules, "logfire", raising=False)
    monkeypatch.setattr(builtins, "__import__", _boom)
    obs = Observability()
    st = obs.init(Settings(otel_service_name="s", otel_token="tok"))
    assert st["enabled"] is False and "未安装" in st["reason"]
    assert "未启用" in capsys.readouterr().err


def test_singleton_exists():
    """全服务只有一份实现 —— 各处 `import logfire` 会让配置静默分叉。"""
    from app import observability

    assert observability.OBS is OBS


def test_sink_attach_is_idempotent(fake_lf):
    """`logger.remove()` 会把 SDK sink 一并摘掉 ⇒ 必须能精确重挂。

    摘掉后忘记重挂 = 之后全进程都没有上报，且**毫无报错**。
    """
    obs = Observability()
    obs.init(Settings(otel_service_name="s", otel_token="tok"))
    obs.attach_loguru_sink()
    obs.attach_loguru_sink()
    assert fake_lf.loguru_handler_calls >= 2, "第二次要重新挂（先摘后挂）"
    assert len(obs._sink_ids) <= 1, "不许重复堆积 sink"


# ---------------------------------------------------------------------------
# 接线：探活真的不被记录
# ---------------------------------------------------------------------------


def test_probe_requests_produce_no_spans_and_no_logs(settings, fake_jimeng,
                                                     fake_uploader, monkeypatch):
    """探活会被容器每 30s 打一次。**span 与日志两条通道都要摘** ——
    只摘 span 会留下一半噪音（这是最容易"以为关掉了"的地方）。
    """
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.service import Service as RealService

    monkeypatch.setattr(
        main_mod, "Service",
        lambda s: RealService(s, client=fake_jimeng, uploader=fake_uploader,
                              cfg=None))
    # ⚠️ sink 必须在 create_app **之后**挂：create_app 会调 setup_logging()，
    # 而它第一句就是**全局** `logger.remove()` —— 先挂的 sink 会被一并摘掉，
    # 于是 captured 永远是空的（第一版就这么踩的：断言全红，却看不出真正原因）。
    app = main_mod.create_app(settings)
    captured: list[str] = []
    sink_id = logger.add(lambda m: captured.append(m), level="INFO",
                         format="{message}")
    try:
        with TestClient(app) as c:
            c.get("/healthz")
            c.get("/healthzz")                     # 对照组：**必须**留下日志
            c.get("/async/v1/images/generations")  # 业务路径也要留
    finally:
        # ⚠️ app 启动时 `setup_logging()` 会调**全局** `logger.remove()`，
        # 把我们这个测试 sink 一并摘掉 ⇒ 这里必须容忍"它已经不在了"。
        # 这正是技能里点名的 loguru 全局状态陷阱（也是 `attach_loguru_sink`
        # 必须幂等的原因）。不这样写会报 "no existing handler with id N"。
        with contextlib.suppress(ValueError):
            logger.remove(sink_id)
    joined = "\n".join(captured)
    # ⚠️ 断言要带分隔标记：`GET /healthz` 是 `GET /healthzz` 的子串，
    # 只做子串断言会被自己的对照组骗过去。
    assert "GET /healthz ->" not in joined, "探活路径不该留日志"
    assert "GET /healthzz ->" in joined, "对照组必须留日志（否则是整体没接上）"
    assert "GET /async/v1/images/generations ->" in joined
