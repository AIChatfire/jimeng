"""gunicorn 配置 —— 单进程 uvicorn worker。

**WORKERS 默认 1 是架构约束，不是保守参数。** 三个理由：
  1. 上下游节奏闸门（最小间隔 / 每分钟上限 / 风控冷却）是进程内状态，
     进程数 N 会把限速按 N 倍放大 —— 那正是上游风控最敏感的维度；
  2. 任务协调器靠 SQLite 租约选主：多 worker 虽然安全，但非持锁进程只会空转，
     白白多一份连接与日志噪音；
  3. 建任务是**计费**动作，重复提交等于重复扣积分。

要提吞吐的正确顺序是：先把 JM_CONCURRENCY 从 1 提高到实测上限（≥4），
再考虑把任务库换成共享后端。见 README「扩容前必读」。
"""
from __future__ import annotations

import multiprocessing
import os

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '8200')}"

workers = int(os.environ.get("WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"

# 上游生成是异步的，单次请求本身很轻（受理即返回），但保险起见留足余量
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 90
keepalive = 5

# 刻意不开 preload：应用启动期会建 SQLite 连接并拉起协调器，
# 在 fork 之前建连接会让子进程共享不该共享的句柄。
preload_app = False

max_requests = 0
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "INFO").lower()

_ = multiprocessing  # 保留导入：便于将来按 CPU 数推导 workers 时直接可用
