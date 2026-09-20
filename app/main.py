#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI 装配：路由 + 统一错误信封 + lifespan（协调器）+ 埋点接线。

## 对外契约只有三条（冻结，见 `docs/INTERFACE.md`）

```
POST   /async/v1/images/generations         受理，只回一个 task_id
GET    /async/v1/images/generations/{id}    非终态回排队态；终态回 {data, created, usage}
GET    /async/v1/models                     模型清单（OpenAI 形态）
```

`GET /healthz` 是**运维端点**，不属于对外契约：它零依赖、不触上游、不消耗积分
（容器 HEALTHCHECK 每 30s 打它）。它**既不上报 span、也不留日志** ——
两条通道都从 `observability.PROBE_PATHS` 一张表派生。
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Iterator

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from . import models
from .config import Settings
from .coordinator import Coordinator
from .errors import AdapterError, AuthError, InvalidParameterError
from .observability import (
    OBS,
    excluded_urls,
    is_probe_path,
    setup_logging,
    should_log_path,
)
from .service import Service, view

# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class GenerationRequest(BaseModel):
    """`POST /async/v1/images/generations` 的请求体。

    刻意 `extra="allow"`：未知/已知但不支持的字段由 `Service.create` 统一裁决
    （它分得清"认得但做不到"（进 `degradations`）与"写错了"（400）），
    在 schema 层报错就拿不到这个区分。

    🔴 **字段类型刻意宽松**（`Any` / 可空），把严格性全部交给 `Service.create`。
    原因：schema 层的报错是 Pydantic 的 422，格式固定且**不可执行** ——
    调用方传 `"image": "https://…"`（很常见的写法）会拿到一条机器味儿的
    `Input should be a valid list`，而不是我们那条"请写 `"image": ["…"]`"的提示。
    同理，`n: null` 这类"显式给了空值"必须等价于"没给"（用默认），
    不能因为 schema 填了个 `None` 就被判成参数错误。
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = Field(default=None, description="能力/模型名，如 jimeng-t2i（留空则按有无输入图推导）")
    prompt: str | None = Field(default=None, description="提示词")
    image: Any = Field(default=None, description="输入图**数组**；文生图传 []")
    size: str | None = Field(default=None, description="如 2048x2048")
    n: int | None = Field(default=None, description="出图张数（会吸附到该模型声明的合法取值）")
    seed: int | None = None
    negative_prompt: str | None = None


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


def _service(request: Request) -> Service:
    return request.app.state.service


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization") or ""
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return raw.strip() or None


def require_key(request: Request) -> str:
    """校验调用方 Key，返回**凭证指纹**（不是明文）。

    · 未配置 `API_KEYS` ⇒ 鉴权关闭（dev），返回 `"anonymous"`；
    · 配置了 ⇒ 必须带 Bearer，且必须在白名单里。
    """
    settings: Settings = request.app.state.settings
    key = _bearer(request)
    if not settings.auth_enabled:
        return request.app.state.service.credential_of(None)
    if not key:
        raise AuthError("缺少 Authorization: Bearer <key>")
    if key not in settings.api_keys:
        raise AuthError("API Key 无效")
    return request.app.state.service.credential_of(key)


def require_key_optional(request: Request) -> str | None:
    """**可选的**调用方 Key —— 只给"按 id 即凭据"的读接口用（GET 单条任务）。

    三种情况分得很清（刻意不合并）：

    · **完全没带** `Authorization` ⇒ 返回 `None`，**放行**。
      理由：`task_id` 是不可猜的 128 位随机值，且**只在受理时返回给带 Key 的调用方**
      ⇒ id 本身就是凭据（调用方可以把结果链接直接给别人看）。
    · **带了但无效**（不在白名单）⇒ **照旧报 401**。不能因为"反正放行"就把错的 Key
      蒙过去 —— 那会让调用方的配置错误被静默吞掉，是最难查的一类问题。
    · **带了且有效、但不是该任务的属主** ⇒ 返回该指纹；上游按 id 取，
      **不做属主校验**（与"没带"同一待遇，语义统一好预测）。
    """
    settings: Settings = request.app.state.settings
    if _bearer(request) is None:
        # 鉴权关闭（dev）时也走这条路：与"没带"同样放行
        _ = settings
        return None
    return require_key(request)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        app.state.coordinator.start()
        try:
            yield
        finally:
            app.state.coordinator.stop()
            app.state.service.close()
            # 短命进程必须显式 flush，否则退出时最后一批 span 直接丢
            OBS.flush()
            logger.info("jimeng-service 已停止")

    app = FastAPI(
        title="jimeng-service",
        version="0.1.0",
        description="即梦（jimeng.jianying.com）图片生成的**异步**出口。",
        lifespan=lifespan,
    )

    service = Service(settings)
    app.state.settings = settings
    app.state.service = service
    app.state.coordinator = Coordinator(service, settings)

    if not service.store.ping():
        # 任务库是**事实源**：连不上就别装作能服务。启动期报错比运行期
        # "任务存不进去"要早得多，也便宜得多。刻意不做「连不上就退回内存」的降级。
        raise RuntimeError(
            f"任务库（PostgreSQL）连不上：{service.store.dsn}。"
            f"请检查 TASK_DB 与网络连通性。")

    _wire_observability(app, settings)
    _install_error_handlers(app)
    _install_request_logging(app)
    _install_routes(app)

    for w in settings.startup_warnings:
        logger.warning(w)
    logger.info("jimeng-service 装配完成 | " + json.dumps(service.status(),
                                                         ensure_ascii=False))
    return app


def _wire_observability(app: FastAPI, settings: Settings) -> None:
    """logfire + loguru 接线。**失败绝不影响服务启动。**"""
    try:
        OBS.init(settings)
        setup_logging(settings.log_level, obs=OBS)
    except Exception as e:  # noqa: BLE001
        print(f"[observability] 装配失败，已忽略：{type(e).__name__}: {e}")
        setup_logging(settings.log_level, obs=None)
        return

    if not OBS.sdk_configured:
        return
    try:
        import logfire  # noqa: PLC0415
        from .observability import PROBE_PATHS  # noqa: PLC0415

        # `excluded_urls` 由**路径表机械生成**（别手写：它是正则且上游用
        # `re.search` 子串匹配，写 "/" 会命中每一个 URL ⇒ 全站追踪静默关闭）
        url_regex = excluded_urls(PROBE_PATHS)
        logfire.instrument_fastapi(app, excluded_urls=url_regex,
                                   capture_headers=False)
        logger.debug(f"探活路径已从 span 中摘除：{url_regex}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"instrument_fastapi 失败，已忽略：{type(e).__name__}: {e}")


def _install_error_handlers(app: FastAPI) -> None:
    """所有 `AdapterError` → 统一错误信封。**HTTP 状态码来自错误类本身。**"""

    @app.exception_handler(AdapterError)
    async def _adapter_error(_r: Request, exc: AdapterError) -> JSONResponse:
        headers: dict[str, str] = {}
        if exc.retry_after is not None:
            # `Retry-After` 是事实：上游说多久就多久。**没说就不给这个头**
            # —— 编一个数字等于伪造它。
            headers["Retry-After"] = str(int(max(1, round(exc.retry_after))))
        return JSONResponse(status_code=exc.status_code, content=exc.to_error(),
                            headers=headers)

    @app.exception_handler(InvalidParameterError)
    async def _invalid(_r: Request, exc: InvalidParameterError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_error())


def _install_request_logging(app: FastAPI) -> None:
    """每个请求一行摘要。**探活路径不打**（与 span 侧同源判据）。"""

    @app.middleware("http")
    async def _log_request(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        path = request.url.path
        # 探活既不上报 span、也不留日志 —— 只摘 span 会留下一半噪音
        quiet = not should_log_path(path) or is_probe_path(path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            if not quiet:
                logger.bind(request_id=rid, http_path=path).exception(
                    f"{request.method} {path} -> 未处理异常")
            raise
        response.headers["X-Request-Id"] = rid
        if not quiet:
            ms = round((time.perf_counter() - started) * 1000, 1)
            logger.bind(request_id=rid, http_path=path,
                        http_status=response.status_code,
                        duration_ms=ms).info(
                f"{request.method} {path} -> {response.status_code} ({ms}ms)")
        return response


def _install_routes(app: FastAPI) -> None:
    # ------------------------------------------------------------- 受理
    @app.post("/async/v1/images/generations", status_code=202)
    async def create_generation(
        request: Request,
        body: GenerationRequest,
        credential: str = Depends(require_key),
    ) -> JSONResponse:
        """受理一次生成，**只回一个 task_id**。

        请求内**零上游往返**：建任务（计费动作）交给后台执行者，受节奏闸门约束。
        图片拉取同样在后台 —— 拉取失败会体现为任务 `failure`（附原因），
        而不是让受理请求随上游网络抖动。
        """
        svc: Service = request.app.state.service
        rec = svc.create(body.model_dump(), credential=credential)

        # 叫醒协调器：不然这条任务要等到下一个 tick 才被发现（默认最多白等 1s）。
        # 纯优化 —— 唤醒丢了也只是慢一个 tick，"该派发谁"始终由库里的状态决定。
        request.app.state.coordinator.wake()

        # 202 + 一个 id。不返回状态/时间戳之类的附加信息 —— 调用方要的是"拿着它去轮询"。
        return JSONResponse(
            status_code=202, content={"task_id": rec.task_id},
            headers={"Location": f"/async/v1/images/generations/{rec.task_id}"})

    # ------------------------------------------------------------- 查询
    @app.get("/async/v1/images/generations/{task_id}")
    async def get_generation(
        request: Request,
        task_id: str,
        credential: str | None = Depends(require_key_optional),
    ) -> JSONResponse:
        """查任务。**不需要 Authorization：`task_id` 本身就是凭据。**

        · 非终态 → **202** + `{task_id, status}`（调用方据此继续轮询）；
        · 成功 → **200** + `{data: [{url}], created, usage}`；
        · 失败 → **200** + `{task_id, status: "failure", error}`；
        · 不存在 → **404**（**本地拦，不发上游请求**）。

        鉴权（2026-09-20 起刻意放宽，判据见 `require_key_optional`）：
        **不带 Key 也能查**；带了**无效** Key 仍报 401；
        带了有效但不属于该任务的 Key **照样能查**（id 即凭据）。
        ⚠️ `DELETE` 与**列表**接口**仍然强制鉴权** —— 否则可以枚举/删除别人的任务。
        """
        svc: Service = request.app.state.service
        rec = svc.get_for_credential(task_id, credential)
        status_code, payload = view(rec)
        return JSONResponse(status_code=status_code, content=payload)

    # ------------------------------------------------------------- 列表 / 删除
    @app.get("/async/v1/images/generations")
    async def list_generations(
        request: Request,
        limit: int = 50,
        credential: str = Depends(require_key),
    ) -> dict:
        """本 Key 名下的任务列表。**只列自己的** —— 只按服务过滤会把别人的任务列给你。"""
        return request.app.state.service.list_for_credential(
            credential, limit=max(1, min(limit, 200)))

    @app.delete("/async/v1/images/generations/{task_id}")
    async def delete_generation(
        request: Request,
        task_id: str,
        credential: str = Depends(require_key),
    ) -> dict:
        """删除任务。**未终态的任务会响亮失败（400）** —— 即梦没有取消端点，
        本地删掉只会让"还在跑并继续计费"变成看不见的事。"""
        return request.app.state.service.delete_for_credential(task_id, credential)

    # ------------------------------------------------------------- 模型
    @app.get("/async/v1/models")
    async def list_models() -> dict:
        """本服务对外宣告的能力清单（OpenAI 形态）。

        只列**已端到端验证过**的能力；刻意缺席的（细节修复）不在这里 ——
        那就是"制造假能力"。
        """
        return {"object": "list", "data": models.catalog()}

    # ------------------------------------------------------------- 运维
    @app.get("/healthz")
    async def healthz() -> dict:
        """存活探针。**零依赖、不触上游、不消耗积分。**"""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        """就绪探针：**依赖项不通就报 503**，让编排层不要往这里导流量。

        查两件真正决定"能不能接活"的事：任务库可连、上游凭据已配。
        注意它比 `/healthz` 贵（会 ping 一次 DB），所以**不要**拿它当容器
        HEALTHCHECK —— 那个用 `/healthz`。
        """
        svc: Service = request.app.state.service
        if not svc.store.ping():
            return JSONResponse(status_code=503, content={
                "status": "not_ready", "reason": "任务库（PostgreSQL）不可连",
                "dsn": svc.store.dsn,
            })
        if not svc.settings.upstream_configured:
            return JSONResponse(status_code=503, content={
                "status": "not_ready",
                "reason": "未配置 JIMENG_SESSIONID，受理会返回 503",
            })
        return JSONResponse(status_code=200, content={"status": "ready"})

    @app.get("/stats")
    async def stats(request: Request) -> dict:
        """运行状态（闸门统计 / 任务计数 / 能力表缓存 / 观测状态 / 存储健康）。"""
        svc: Service = request.app.state.service
        return {**svc.status(),
                "coordinator": request.app.state.coordinator.stats(),
                "store": svc.store.stats()}


app_factory = create_app  # 便于测试与 uvicorn 直接引用


if __name__ == "__main__":  # pragma: no cover
    # 变量名就叫 `settings`：静态门禁是按 `*.settings.X` 收集配置读取点的，
    # 叫 `_s` 会让 host/port 被误判成"没人读的死旋钮"。
    settings = Settings.from_env()
    uvicorn.run("app.main:create_app", factory=True,
                host=settings.host, port=settings.port,
                log_level=settings.log_level.lower())
