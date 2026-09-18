"""gunicorn 配置 —— 单进程 uvicorn worker。

**WORKERS 默认 1 是架构约束，不是保守参数。** 三个理由：
  1. 上下游节奏闸门（最小间隔 / 每分钟上限 / 风控冷却）是进程内状态，
     进程数 N 会把限速按 N 倍放大 —— 那正是上游风控最敏感的维度；
  2. 任务协调器靠 PostgreSQL 里的租约选主（任务库已统一到 PG，不再是 SQLite）：
     多 worker 虽然安全，但非持锁进程只会空转，白白多一份连接与日志噪音；
  3. 建任务是**计费**动作，重复提交等于重复扣积分。

要提吞吐的正确顺序是：先把 JM_CONCURRENCY 从 1 提高到实测上限（≥4），
再考虑把任务库换成共享后端。见 README「扩容前必读」。
"""
from __future__ import annotations

import multiprocessing
import os

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '8200')}"

workers = int(os.environ.get("WORKERS", "1"))
# ⚠️ 用独立包 `uvicorn-worker`，**不要**写 `uvicorn.workers.UvicornWorker`：
# 后者已被 uvicorn 官方弃用（导入即报
# `The 'uvicorn.workers' module is deprecated. Please use 'uvicorn-worker' package`），
# 终将被移除。而本仓的依赖是 `uvicorn>=0.30`（不锁上界）⇒ 某次镜像重建
# 就会在启动时炸掉，且**本地跑得好好的**（本地 venv 里那个版本还在）。
worker_class = "uvicorn_worker.UvicornWorker"

# 上游生成是异步的，单次请求本身很轻（受理即返回），但保险起见留足余量
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 90
keepalive = 5

# 刻意不开 preload。两个理由，第二个是这套栈（gunicorn + OTel/Logfire）**必须**遵守的：
#   ① 应用启动期会建数据库连接并拉起协调器，fork 之前建连接会让子进程共享不该共享的句柄；
#   ② 🔴 **OTel 的 BatchSpanProcessor 会起一个后台导出线程**。若在 master 里先初始化
#      再 fork，子进程拿到的是**被复制出来的线程状态**（线程本身不会跨 fork 存活），
#      结果是导出器行为未定义 —— 典型表现是 span 静默丢失或上报错乱。
#      所以可观测性必须在**每个 worker 里各自 init**（本仓走 `create_app()` →
#      `OBS.init()`，天然满足；前提就是 preload 保持关闭）。
preload_app = False

max_requests = 0
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "INFO").lower()

_ = multiprocessing  # 保留导入：便于将来按 CPU 数推导 workers 时直接可用
