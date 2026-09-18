#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可观测性：**logfire（trace）+ loguru（日志）** 的单一收拢点。

## 四条不变量（评审依据）

| 不变量 | 反面（真实事故） |
|---|---|
| **绝不阻塞** | SDK 未装 / 无 token / 网络不通时抛异常，把请求带崩 —— 观测反噬业务 |
| **单一实现** | 各处自己 `import logfire` ⇒ 有的地方 configure 过、有的没有，静默分叉 |
| **耗时是实测值** | 为了让 span 好看，把 span 包在错误的区间上 —— 图表骗人 |
| **上报内容不改写** | 事后"顺手脱敏"改写业务字段 ⇒ 面板上的值与上游实际不符，排障时被自己骗 |

## 🔴 口径：观测面**不脱敏**（2026-09-19 定）

上下游原始报文、任务 id、告警、**名字像凭据的字段**都原样上报：

- `logfire.configure(scrubbing=False)` —— 关掉 SDK 自带 scrubber。
  **必须关**：它按**值子串**命中 `credential` / `token` / `auth`，而即梦产物是
  TOS 预签名 URL（必含 `X-Tos-Credential=`），默认脱敏会把**每一条结果 URL**
  变成 `[Scrubbed due to 'Credential']` —— 面板直接不可读，比不脱敏更糟。
- **本模块不做任何事后改写**：属性名、属性值一律原样进 span。
  没有"看起来像凭据的键就删掉""把 Bearer 换成 `***`"这类规则 ——
  那种规则会改掉上游的实际字段名，让人对着面板排查一个**不存在的**字段。

想改回脱敏：`OTEL_SCRUBBING=1`（只影响 SDK 自带 scrubber；本模块本身仍不改写）。

## 那凭据怎么办？（这是**另一条**纪律，不是脱敏）

**凭据在取值处就不进属性。** 三条实现约束，而不是事后的过滤器：

1. 客户端持有 cookie / `sessionid` 的地方是 `JimengClient.jar`，
   **从不作为 span 属性或日志字段传入**；
2. 上游 HTTP 埋点在 `http_path` 上**只给 pathname、丢掉整个 query**
   （签名类参数可能就藏在 query 里，且 query 对排障无价值）；
3. `instrument_fastapi(capture_headers=False)` —— 请求头整个不采集，
   于是 `Authorization` / `Cookie` 无从进入 span。

⇒ 这个口径下"面板可读"与"密钥不上报"**不冲突**：前者靠不脱敏，后者靠不去碰它。
⚠️ 代价是**新增埋点时不得把凭据塞进属性** —— 这条约束由
`tests/test_observability.py` 里"上游埋点不携带凭据"的用例守着。

## 探活端点：span 与日志**两条通道都要摘**

容器 HEALTHCHECK 每 30s 打一次。OpenTelemetry 的 `excluded_urls` 只摘 **span**；
日志侧该上报照上报。"关掉了"往往只关了一半。
⇒ 两条通道都从 `PROBE_PATHS` **一张表**派生，改一处不会漏另一处。
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
import threading
import time
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# 探活路径表 —— span 与日志两条通道的唯一来源
# ---------------------------------------------------------------------------

#: 探活/运维路径。**改动只在这里改**。
PROBE_PATHS: tuple[str, ...] = ("/healthz", "/readyz")


def _path_matches(path: str, pattern: str) -> bool:
    """路径匹配语义，**只定义一次**。

    · 普通条目（`/healthz`）= **精确**匹配；
    · 尾斜杠条目（`/metrics/`）= 子树匹配；
    · `"/"` 特例 = 只匹配根，**绝不是"匹配一切"**。
    """
    if pattern == "/":
        return path == "/"
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return path == pattern


def is_probe_path(path: str) -> bool:
    return any(_path_matches(path, p) for p in PROBE_PATHS)


def should_log_path(path: str) -> bool:
    """日志侧判据（与 span 侧同源）。"""
    return not is_probe_path(path)


def excluded_urls(paths: tuple[str, ...] = PROBE_PATHS) -> str:
    """由路径表**机械生成** logfire 的 `excluded_urls`。

    返回的是**逗号分隔的多个正则**（上游按逗号切分后逐条编译）。

    🔴 别手写这个字符串：`excluded_urls` 是正则且上游用 `re.search`（**子串匹配**），
    写 `"/"` 会命中**每一个** URL（任何 URL 都含 `/`）⇒ 全站追踪被静默关掉，
    比不排除更糟。故这里一律用**全锚定**形态：`^https?://[^/]+/healthz$`。
    """
    parts = [rf"^https?://[^/]+{re.escape(p)}$" for p in paths]
    return ",".join(parts)


# ---------------------------------------------------------------------------
# 结构化属性：让 JSON 体在面板里**可展开、可按字段过滤**
# ---------------------------------------------------------------------------

#: 超过这个体量不做 JSON 解析（解析成本与被测体量同阶，大体内联 b64 时纯白花）
LOG_PARSE_LIMIT = 256 * 1024
_MAX_DEPTH = 6
_MAX_STR = 512
_MAX_LIST = 32


def parsed_json(raw: Any) -> dict[str, Any] | None:
    """把响应/请求字节解析成**可命名维度**的 dict；不可结构化时返回 None。

    四条纪律（缺一条就会把面板搞成"字段都在、就是查不了"）：
      1. 超限不解析；2. 顶层非 dict 不做（没有可命名的字段）；
      3. 先瘦身再上报；4. **恒不抛**。
    """
    if raw is None or isinstance(raw, (dict, list)):
        node = raw
    else:
        if isinstance(raw, bytes):
            if not raw or len(raw) > LOG_PARSE_LIMIT:
                return None
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(raw, str) or not raw or len(raw) > LOG_PARSE_LIMIT:
            return None
        try:
            node = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(node, dict):
        return None
    shrunk = _shrink(node)
    return shrunk if isinstance(shrunk, dict) else None


def _shrink(node: Any, depth: int = 0) -> Any:
    """瘦身：深度/单串/数组条目都有上限；`data:...;base64` 换成长度标记。

    ⚠️ **只做体量控制，不改写业务字段的语义**：截断会显式留下 `…` /
    `<data-uri N chars>` 标记，而不是悄悄把值换掉。**键名一律保真** ——
    改名就等于在面板上凭空造出一个上游并不存在的字段。
    """
    if depth >= _MAX_DEPTH:
        return "<max-depth>"
    if isinstance(node, dict):
        return {str(k): _shrink(v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        cut = node[:_MAX_LIST]
        out = [_shrink(v, depth + 1) for v in cut]
        if len(node) > _MAX_LIST:
            out.append(f"<+{len(node) - _MAX_LIST} more>")
        return out
    if isinstance(node, str):
        if node.startswith("data:") and ";base64," in node:
            return f"<data-uri {len(node)} chars>"
        return node if len(node) <= _MAX_STR else node[:_MAX_STR] + "…"
    return node


def prepare_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """属性出口的**唯一**处理：把 dict/list 转成可展开的结构，其余原样。

    🔴 **这里不做脱敏、不改名、不删键。** 见模块 docstring 的口径一节：
    事后改写会让人对着面板排查一个上游并不存在的字段。
    """
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        out[k] = _shrink(v) if isinstance(v, (dict, list)) else v
    return out


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------


class Observability:
    """logfire + loguru 的单一收拢点。

    **所有方法在任何情况下都不抛异常。** 观测失败绝不允许影响业务请求。
    """

    def __init__(self) -> None:
        self._lf: Any = None
        self.enabled = False
        self.sdk_configured = False
        self.service = "jimeng-service"
        self.reason: str | None = "未初始化"
        self.egress: str = "none"
        #: 本次装配生效的脱敏开关（默认 False = 不脱敏，见模块 docstring）
        self.scrubbing = False
        self._sink_ids: list[int] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 装配

    def init(self, settings) -> dict:
        """按配置装配。返回状态 dict（`{enabled, service, reason, egress, scrubbing}`）。

        三种结果，**每一种都说清原因**：
          · 装了 SDK + 有 token ⇒ `egress="otlp"`；
          · 装了 SDK 但没 token ⇒ `send_to_logfire=False`（`egress="none"`）；
          · 没装 SDK ⇒ `enabled=False`，reason 写明"未安装"。
        """
        self.service = settings.otel_service_name
        self.scrubbing = bool(settings.otel_scrubbing)
        try:
            import logfire  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            self.enabled = False
            self.sdk_configured = False
            self.reason = f"未安装 logfire（{type(e).__name__}）"
            self._announce()
            return self.status()

        token = (settings.otel_token or "").strip()
        try:
            logfire.configure(
                token=token or None,
                send_to_logfire=("if-token-present" if not token else True),
                service_name=settings.otel_service_name,
                service_version=_service_version(),
                environment=(settings.otel_environment or None),
                # ⚠️ 默认 False（不脱敏）。改成 True 前先读模块 docstring：
                # SDK 自带 scrubber 按值子串命中 credential/token/auth，
                # 而即梦产物是 TOS 预签名 URL（必含 X-Tos-Credential=），
                # 打开它会把每一条结果 URL 变成 [Scrubbed due to 'Credential']。
                scrubbing=self.scrubbing,
                console=False,
                # 观测面不采样：本服务量小，采样只会让"3 次失败"随机消失
                sampling=None,
                additional_span_processors=None,
                # 参数不采集（也不采请求头）—— 见 asgi 侧 capture_headers=False
                inspect_arguments=False,
            )
        except Exception as e:  # noqa: BLE001
            self.enabled = False
            self.sdk_configured = False
            self.reason = f"logfire.configure 失败（{type(e).__name__}: {e}）"
            self._announce()
            return self.status()

        self._lf = logfire
        self.sdk_configured = True
        self.enabled = True
        if token:
            self.egress = "otlp"
            self.reason = None
        else:
            self.egress = "none"
            self.reason = "未配置 LOGFIRE_TOKEN —— 仅本地 span，不上报"

        self._announce()
        return self.status()

    def _announce(self) -> None:
        """降级必须是**可见**的 —— 打一行人话，别让人对着空面板猜。"""
        if self.enabled and self.reason:
            sys.stderr.write(f"[observability] 已启用但不上报：{self.reason}\n")
        elif not self.enabled:
            sys.stderr.write(f"[observability] 未启用：{self.reason}\n")
        else:
            sys.stderr.write(
                f"[observability] 已启用：service={self.service} "
                f"egress={self.egress} scrubbing={self.scrubbing}\n")

    def status(self) -> dict:
        return {"enabled": self.enabled, "service": self.service,
                "reason": self.reason, "egress": self.egress,
                "scrubbing": self.scrubbing}

    # ------------------------------------------------------------------ 日志

    def attach_loguru_sink(self) -> None:
        """把 logfire 的 loguru 桥接挂上。**幂等** —— 重复调用不会重复挂。

        为什么必须幂等：`setup_logging()` 里的 `logger.remove()` 是**全局**状态，
        会把 SDK sink 一并摘掉。摘掉后忘记重挂 = 之后全进程都没有上报，且**毫无报错**。
        """
        if not self.sdk_configured:
            return
        try:
            from loguru import logger  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            try:
                # 先精确摘掉上一次挂的（按 id，不用 remove() 全清）
                for sid in self._sink_ids:
                    with contextlib.suppress(Exception):
                        logger.remove(sid)
                self._sink_ids = []
                sid = logger.add(**self._lf.loguru_handler())
                self._sink_ids.append(sid)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[observability] 挂 loguru→logfire 桥接失败："
                                 f"{type(e).__name__}: {e}\n")

    # ------------------------------------------------------------------ span

    @contextlib.contextmanager
    def span(self, name: str, /, **attrs: Any) -> Iterator[Any]:
        """一条 span。**并记录实测耗时**为 `duration_ms` 属性。

        ⚠️ 为什么不只依赖 span 自身的时长：span 是我们自己包出来的区间，
        属性才是量出来的值。面板按 `duration_ms` 过滤时不会受包法影响。

        `name` 用位置参数（`msg_template`）——**维度走属性，正文走模板**。
        """
        start = time.perf_counter()
        prepared = prepare_attrs(attrs)
        if not self.enabled:
            yield None
            return
        try:
            with self._lf.span(name, **prepared):
                try:
                    yield self._lf
                finally:
                    ms = round((time.perf_counter() - start) * 1000, 2)
                    with contextlib.suppress(Exception):
                        self._lf.set_attribute("duration_ms", ms)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[observability] span({name}) 失败，已忽略："
                             f"{type(e).__name__}: {e}\n")
            yield None

    # ------------------------------------------------------------------ 记点

    def info(self, message: str, /, **attrs: Any) -> None:
        self._emit("info", message, attrs)

    def warning(self, message: str, /, **attrs: Any) -> None:
        self._emit("warning", message, attrs)

    def error(self, message: str, /, **attrs: Any) -> None:
        self._emit("error", message, attrs)

    def _emit(self, level: str, message: str, attrs: dict[str, Any]) -> None:
        """经 loguru 出口 —— **单一通道**，避免 logfire.info 与 log.info 双份上报。

        维度一律走 `bind`（顶层属性，可过滤可展开）；位置参数会落进
        `logfire.logging_args` 数组、**不能按字段过滤**（见技能 §8）。
        """
        try:
            from loguru import logger  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return
        prepared = prepare_attrs(attrs)
        try:
            getattr(logger.bind(**prepared), level)(message)
        except Exception:  # noqa: BLE001, S110
            pass

    def upstream(self, *, method: str, path: str, status: int | None,
                 duration_ms: float, body: Any = None, response: Any = None,
                 error: str | None = None, **extra: Any) -> None:
        """上报一次上游调用 —— **HTTP 层的单一收缝**（不要在别处再包一遍）。

        `body` / `response` 会经 `parsed_json` 转成**可展开的 dict 属性**
        （`upstream_request_json` / `upstream_response_json`），
        面板里能按 `upstream_response_json.ret` 这类路径过滤。

        🔴 **凭据不进这里**：`path` 只给 pathname（query 全部丢掉），
        请求头与 cookie 从不作为参数传入。见模块 docstring 的三条实现约束。
        """
        attrs: dict[str, Any] = {
            "upstream": "jimeng",
            "http_method": method,
            "http_path": path,          # 只给 path：query 可能带签名类参数，且对排障无价值
            "http_status": status,
            "duration_ms": round(duration_ms, 2),
        }
        if error:
            attrs["error"] = error
        req_json = parsed_json(body)
        if req_json is not None:
            attrs["upstream_request_json"] = req_json
        resp_json = parsed_json(response)
        if resp_json is not None:
            attrs["upstream_response_json"] = resp_json
        attrs.update(extra)
        self._emit("info", "upstream call", attrs)

    # ------------------------------------------------------------------ 收尾

    def flush(self, timeout_ms: int = 3000) -> bool:
        """刷出待发 span。**短命进程必须显式调用**，否则退出时最后一批直接丢。

        ⚠️ 返回 True 只说明"刷过一次"，**不代表端点真的受理**。
        """
        if not self.enabled:
            return False
        try:
            return bool(self._lf.force_flush(timeout_millis=timeout_ms))
        except Exception:  # noqa: BLE001
            return False


def _service_version() -> str:
    try:
        from . import __version__  # noqa: PLC0415
        return __version__
    except Exception:  # noqa: BLE001
        return "0.0.0"


#: 全服务共用的单例。**不要在各处 new Observability()** —— 那就是多份实现。
OBS = Observability()


# ---------------------------------------------------------------------------
# loguru 装配
# ---------------------------------------------------------------------------

#: 控制台格式：**带上 extra**，否则 bind 进去的维度在本地一条都看不见
#: （stderr sink 默认只格式化 `{message}`）。
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
    "<level>{level: <7}</level> <cyan>{name}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level> "
    "<dim>{extra}</dim>"
)

#: 压到 WARNING 的噪音源。**真正的杠杆只有"日志条数"**，不是 sink。
_NOISY_LOGGERS = (
    "httpx", "httpcore", "urllib3", "uvicorn.access", "asyncio",
    "opentelemetry", "logfire",
)


class _InterceptHandler(logging.Handler):
    """把 stdlib logging（uvicorn / httpx / 第三方）接进 loguru，避免两套日志并存。"""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 薄适配
        try:
            from loguru import logger  # noqa: PLC0415

            level: str | int
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            logger.opt(depth=6, exception=record.exc_info).log(
                level, record.getMessage())
        except Exception:  # noqa: BLE001, S110
            pass


def setup_logging(level: str = "INFO", *, obs: Observability | None = None) -> None:
    """装配 loguru。

    ⚠️ `logger.remove()` 是**全局**状态：它会连 SDK sink 一起摘掉，
    所以摘完必须按"SDK 是否已配置"**重挂**（`attach_loguru_sink` 幂等）。
    忘了重挂的症状是"之后全进程都没有上报，且毫无报错"。

    `backtrace=False, diagnose=False` 刻意保留：它们**不是脱敏**，
    而是"别把帧局部变量一股脑倒进日志"—— loguru 的 diagnose 会把帧内变量
    全量打印，日志体积与噪音都会失控。要看的字段应当**显式 bind**，
    而不是靠 diagnose 顺手捞。
    """
    try:
        from loguru import logger  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[logging] 未安装 loguru，退回 stdlib 日志：{e}\n")
        logging.basicConfig(level=level)
        return

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=_CONSOLE_FORMAT,
        backtrace=False,
        diagnose=False,
        # 刻意 **不开 enqueue**：它更慢（每条约 +34us，走 multiprocessing 队列要 pickle），
        # 而且会把桥接回溯不到调用栈帧 ⇒ `logfire.logging_args` 静默丢失。
        enqueue=False,
    )
    if obs is not None:
        obs.attach_loguru_sink()

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


__all__ = [
    "Observability", "OBS", "setup_logging",
    "PROBE_PATHS", "is_probe_path", "should_log_path", "excluded_urls",
    "prepare_attrs", "parsed_json", "LOG_PARSE_LIMIT",
]
