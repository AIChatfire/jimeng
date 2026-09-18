#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接线门禁 —— 专门盯「代码互相之间接对了没有」。

本文件的存在原因是一次真实事故（两个 bug 都能过 `compileall`、也都能正常
`import`，所以谁都没发现）：

1. 🔴 `app/upstream/jimeng/client.py::_report()` 引用 `OBS` 却**从没 import 过**，
   而它外面那层 `except Exception: pass` 把 `NameError` 一起吞了
   ⇒ **上游埋点从来没生效，且日志里一个字都没有**。
2. 🔴 `app/service.py::fingerprint_secret()` 还在调 SQLite 时代的
   `store._connect()`（SQLModel 版根本没这个方法）
   ⇒ **`Service()` 一构造就 `AttributeError`**，整个服务起不来。

⇒ 两类门禁：
   A. **静态**：用 ruff 的正确性规则集（F/E9/BLE/S110/PLC0415/SLF001）守"未定义名 /
      跨层摸私有成员 / 静默吞异常"；
   B. **动态**：本文其余用例**真的把它们跑一遍** —— 因为"接线"这件事只有跑才证明得了。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from loguru import logger

from app.config import Settings
from app.errors import AdapterError
from app.observability import OBS

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


# ---------------------------------------------------------------------------
# A. 静态门禁：ruff
# ---------------------------------------------------------------------------


def test_ruff_correctness_gate_passes():
    """ruff 的正确性规则集必须全绿。

    ⚠️ **缺 ruff 时本用例失败而不是跳过** —— 跳过会让"没检查"看起来像"检查过了"。
    ruff 是 dev 依赖，见 `requirements-dev.txt`。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "app", "tests", "scripts"],
        cwd=ROOT, capture_output=True, text=True)
    if "No module named ruff" in (proc.stderr or ""):
        pytest.fail(
            "缺少 ruff —— 它是本仓的接线门禁（曾靠 F821/SLF001 抓到两个真 bug）。\n"
            "  pip install -r requirements-dev.txt", pytrace=False)
    assert proc.returncode == 0, (
        "ruff 报出了正确性问题（未定义名 / 跨层私有访问 / 静默吞异常 …）：\n"
        + (proc.stdout or "") + (proc.stderr or ""))


# ---------------------------------------------------------------------------
# B. 配置接线：读得到、且没有死旋钮
# ---------------------------------------------------------------------------


def _settings_reads() -> set[str]:
    """收集 app/ 里所有配置读取点。

    两种写法都要认：`settings.X` 与 `self.settings.X`（后者是本仓协调器/服务的写法）。
    ⇒ 这条门禁也是"为什么 `__main__` 里的局部变量必须叫 `settings`"的原因：
    叫 `_s` 会让 `host`/`port` 被误判成没人读的死旋钮。
    """
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            if isinstance(base, ast.Name) and base.id == "settings":
                found.add(node.attr)
            elif isinstance(base, ast.Attribute) and base.attr == "settings":
                found.add(node.attr)
    return found


def _settings_self_reads() -> set[str]:
    """`Settings` 自己的 property 体里用 `self.X` —— 也算"有人读"。"""
    tree = ast.parse((APP / "config.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            found.add(node.attr)
    return found


def test_every_settings_read_exists():
    """🔴 每个被读的配置项都必须在 `Settings` 上真的存在。

    这真是 `store._connect()` 那类缺陷在配置层的对应物：
    读一个不存在的属性 ⇒ `AttributeError`，而**静态导入一切正常**。

    ⚠️ 判据要把**方法**也算上（`settings.validate()` 是合法读取）。
    只认字段/属性的话，这条门禁会把正常调用误报成缺陷 ——
    门禁误报一次，人就会开始无视它。
    """
    fields = set(Settings.__dataclass_fields__)
    props = {k for k, v in vars(Settings).items() if isinstance(v, property)}
    methods = {k for k, v in vars(Settings).items() if callable(v)}
    unknown = sorted(_settings_reads() - fields - props - methods)
    assert not unknown, (
        f"这些配置项被读但 Settings 上没有：{unknown} —— 会 AttributeError。"
        f"要么加字段，要么改读取点。")


def test_no_dead_settings_knobs():
    """每个配置项都必须**有人读** —— 没人读的旋钮要删掉。

    理由不是洁癖：一个"看起来能配但不生效"的旋钮，会让人以为自己已经调优/关停了
    某个行为（例如以为关闭了某个危险路径），而实际什么都没发生。这属于**假配置**。
    """
    fields = set(Settings.__dataclass_fields__)
    read = _settings_reads() | _settings_self_reads()
    dead = sorted(f for f in fields if f not in read)
    assert not dead, (
        f"这些配置项没有任何读取点（假配置）：{dead} —— 请在 app/ 里接上，"
        f"或从 Settings 删掉（连带 .env.example 与 docs）。")


def test_settings_constructs_and_properties_are_readable():
    """默认构造 + 三个派生属性可读（`upstream_configured`/`auth_enabled`/`db_target`）。"""
    s = Settings()
    assert s.upstream_configured is False
    assert s.auth_enabled is False
    assert s.db_target.startswith("postgresql")


def test_startup_warnings_surface_the_two_dangerous_defaults(monkeypatch):
    """未配上游凭据 / 未开鉴权都必须给出启动告警。

    这两件事都能让服务"起得来但很危险"：前者接了活必然失败，后者等于把
    计费的生成能力对所有人开放。`startup_warnings` 只在 `from_env()` 里填充
    （仓促改配置的人正是走环境变量那条路）。
    """
    for key in ("JIMENG_SESSIONID", "JIMENG_COOKIE", "API_KEYS"):
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    text = " ".join(s.startup_warnings)
    assert "JIMENG_SESSIONID" in text, "未配上游凭据要告警"
    assert "API_KEYS" in text or "鉴权" in text, "未开鉴权要告警"


def test_from_env_reads_every_declared_knob(monkeypatch):
    """`from_env()` 必须构造成功 —— 避免"字段加了但没在 from_env 里接上"。"""
    for key in ("TASK_DB", "JIMENG_SESSIONID", "API_KEYS", "LOGFIRE_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert isinstance(s, Settings)
    assert s.task_db


def test_task_db_must_be_postgres():
    """本服务只支持 PostgreSQL：写错地址必须**当场报错**，绝不静默退回别的后端。"""
    from app.store import build_engine

    with pytest.raises(ValueError):
        build_engine("sqlite:///tmp/x.db")
    assert build_engine("postgresql://u:p@h:5432/d").dialect.name == "postgresql"


# ---------------------------------------------------------------------------
# B. 可观测性接线：真的接上了才叫接上了
# ---------------------------------------------------------------------------


def test_upstream_client_actually_emits_observability():
    """🔴 驱动 `JimengClient._post` 走一次，断言**真的产生了一条上报**。

    这一条专门守着上面那个事故：`OBS` 没 import 时，`NameError` 被吞掉，
    任何"只看返回值"的用例都会绿 —— 只有"断言真的记下了"才抓得住。
    """
    from app.upstream.jimeng.client import PATH_HISTORY, JimengClient

    rows: list[dict] = []
    sink_id = logger.add(lambda m: rows.append(dict(m.record["extra"])),
                         level="INFO", format="{message}")
    try:
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"ret": "0",
                                                   "data": {"sid-1": {}}}))
        client = JimengClient(sessionid="s", transport=transport)
        client.fetch("sid-1")
    finally:
        logger.remove(sink_id)

    assert rows, ("上游调用没有产生任何上报 —— 埋点没接上（历史上是漏了 "
                  "`from ...observability import OBS`，且被 try/except 吞掉）")
    row = rows[-1]
    assert row["upstream"] == "jimeng"
    assert row["http_path"] == PATH_HISTORY, "只给 pathname，不许带 query"
    assert row["http_method"] == "POST"
    assert "upstream_response_json" in row, "响应体要做成可展开的属性"
    for banned in ("authorization", "cookie", "sessionid"):
        assert banned not in row, f"凭据不许进属性：{banned}"


def test_report_does_not_swallow_wiring_errors(monkeypatch):
    """`_report` 里**不许**再包一层 try/except：接线错误必须当场炸。

    否则症状就是"埋点静默不生效"，比业务报错难查得多。
    `Observability._emit` 自己已经保证绝不抛。
    """
    from app.upstream.jimeng import client as client_mod
    from app.upstream.jimeng.client import JimengClient

    class _Boom:
        def upstream(self, **_kw):
            raise RuntimeError("wiring broken")

    monkeypatch.setattr(client_mod, "OBS", _Boom())
    c = JimengClient(sessionid="s",
                     transport=httpx.MockTransport(
                         lambda _r: httpx.Response(200, json={"ret": "0"})))
    with pytest.raises(RuntimeError):
        c.fetch("x")


# ---------------------------------------------------------------------------
# B. 公开面冒烟：每个对外入口都真的能调
# ---------------------------------------------------------------------------


def _record(**kw):
    from app.store import TaskRecord
    base = dict(task_id="jimeng_x", credential_id="c", model="jimeng-t2i",
                cap_key="jimeng:t2i", prompt="p", size="2048x2048", n=1,
                created_at=1, updated_at=1)
    base.update(kw)
    return TaskRecord(**base)


def test_response_view_is_callable_for_every_state():
    """`view()` 是响应的**唯一出口** —— 每个状态都必须能渲染出来。"""
    from app.service import view

    for status, expected_code in (("queued", 202), ("in_progress", 202),
                                  ("canceled", 200), ("failure", 200),
                                  ("success", 200)):
        kw = {"status": status}
        if status == "success":
            kw.update(images=[{"url": "https://x/1.png"}], credits=44, finished_at=9)
        if status == "failure":
            kw.update(error={"message": "boom"})
        code, body = view(_record(**kw))
        assert code == expected_code, f"{status} 的 HTTP 码不对"
        assert isinstance(body, dict) and body


def test_error_mapping_covers_every_upstream_error_class():
    """每个上游异常都必须有对外映射 —— 漏一个就会让调用方看到 500。"""
    from app.service import to_adapter_error
    from app.upstream.jimeng import (
        JimengAuthError, JimengContentError, JimengError, JimengParamError,
        JimengQuotaError, JimengRateLimitError, JimengRiskError, JimengTimeout,
    )

    cases = [
        (JimengAuthError("x"), 503),
        (JimengRateLimitError("x"), 429),
        (JimengQuotaError("x"), 429),
        (JimengRiskError("x"), 429),
        (JimengContentError("x"), 400),
        (JimengParamError("x"), 400),
        (JimengTimeout("x"), 504),
        (JimengError("x"), 502),
        (RuntimeError("x"), 502),          # 未预期异常也要有兜底，不许冒 500
    ]
    for exc, want in cases:
        got = to_adapter_error(exc)
        assert isinstance(got, AdapterError), type(exc).__name__
        assert got.status_code == want, (type(exc).__name__, got.status_code, want)
        assert got.to_error()["error"]["message"], "错误体必须有可读消息"


def test_risk_and_quota_are_not_marked_retryable():
    """风控重试会延长标记、额度耗尽重试无效 —— 都不能标成可重试。"""
    from app.service import to_adapter_error
    from app.upstream.jimeng import JimengQuotaError, JimengRiskError

    assert to_adapter_error(JimengRiskError("x")).retryable is False
    assert to_adapter_error(JimengQuotaError("x")).retryable is False


def test_capability_catalog_and_gate_stats_are_callable():
    from app.gate import build_gate
    from app.models import DELIBERATE_ABSENCES, catalog

    ids = {m["id"] for m in catalog()}
    assert "jimeng-t2i" in ids
    assert not (ids & set(DELIBERATE_ABSENCES)), "刻意缺席的能力不许出现在清单里"

    stats = build_gate(Settings()).stats()
    assert stats["name"] == "jimeng" and "cooling_for" in stats


def test_coordinator_stats_without_touching_the_store():
    """`Coordinator.stats()` 不该依赖存储 —— 运维探针要能随时读。"""
    from app.coordinator import Coordinator

    co = Coordinator(service=object(), settings=Settings(), owner="x")  # type: ignore[arg-type]
    st = co.stats()
    assert st["owner"] == "x" and st["running"] is False


def test_singleton_is_the_only_observability_entrypoint():
    """全服务只有一份实现 —— 各处 `import logfire` 会让配置静默分叉。"""
    import app.upstream.jimeng.client as c

    assert c.OBS is OBS


# ---------------------------------------------------------------------------
# 部署接线
# ---------------------------------------------------------------------------


def test_dockerfile_cmd_target_resolves():
    """🔴 `Dockerfile` 里 CMD 指向的启动目标必须真的存在。

    **这条门禁是补的，因为它漏过一次真缺陷**：CMD 原先写的是
    `app.main:app`（当时模块里**没有**模块级 `app`，只有 `create_app` 工厂）。
    后果是镜像**永远起不来** —— gunicorn 报
    `Failed to find attribute 'app' in 'app.main'` / `App failed to load.`
    而**所有单测都是绿的**：它们直接调 `create_app()`，没有一条碰过 Dockerfile。

    这正是"接线门禁"要覆盖的那一类：静态导入一切正常，只有真正启动时才炸。
    本地复现命令（不需要 Docker）：
        gunicorn -c gunicorn_conf.py app.main:app        # 失败
        gunicorn -c gunicorn_conf.py "app.main:create_app()"   # 成功
    """
    import importlib
    import json

    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd_lines = [ln for ln in text.splitlines() if ln.strip().startswith("CMD ")]
    assert cmd_lines, "Dockerfile 里找不到 CMD"
    argv = json.loads(cmd_lines[-1].strip()[len("CMD "):])
    target = argv[-1]

    module_name, _, attr = target.partition(":")
    assert module_name and attr, f"CMD 末尾应是 module:attr 形态，实得 {target!r}"
    is_factory = attr.endswith("()")
    attr_name = attr[:-2] if is_factory else attr

    mod = importlib.import_module(module_name)
    assert hasattr(mod, attr_name), (
        f"CMD 指向 {target!r}，但 {module_name} 上没有 {attr_name!r} —— "
        f"镜像会以 `Failed to find attribute` 启动失败（单测看不出来）。")
    if is_factory:
        assert callable(getattr(mod, attr_name)), f"{target!r} 的工厂不可调用"


def test_dockerfile_needs_no_baked_credentials():
    """镜像里不许烤进带账号密码的连接串 —— 那是部署期信息。

    早先 `ENV TASK_DB=postgresql+psycopg2://jimeng:jimeng@db:5432/jimeng`
    把凭据固化进了镜像层，还会让人误以为"直接 docker run 就能用"。
    """
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for ln in text.splitlines():
        if ln.strip().startswith("ENV") or "TASK_DB=" in ln:
            assert "://" not in ln, f"Dockerfile 里出现了带密码的 DSN：{ln.strip()[:90]}"


def test_compose_required_env_vars_are_documented():
    """compose 用 `${VAR:?}` **强制要求**的变量，必须在 `.env.example` 里有条目。

    否则 `cp .env.example .env && docker compose up` 这条**写在文档里的首跑路径**
    会在 compose 插值阶段直接失败（实测缺口：`POSTGRES_PASSWORD` 当时没列进去，
    而 `.env` 被 gitignore ⇒ 新克隆一定没有它）。
    """
    import re

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    required = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*):\?", compose))
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = {ln.split("=", 1)[0].strip()
                for ln in example.splitlines()
                if "=" in ln and not ln.strip().startswith("#")}
    missing = sorted(required - declared)
    assert not missing, (
        f"compose 强制要求但 .env.example 没列出的变量：{missing} —— "
        f"照文档首跑会失败在 compose 插值那一步。")
