"""本地起一个实例（跑 8201），用真实上游凭据，但连**自己的库**。

为什么不用容器测：容器那边 `JM_CONCURRENCY` 下压着一堆积压任务，
新用例只能排到队尾 —— 本地起一个实例即可**绕开队列**，
而且与容器**不共享数据库**（容器连 compose 的 db，本地连 127.0.0.1:5432），
所以两边的协调器不会互相抢租约。
"""
import os
import pathlib

# 先把 .env 灌进 os.environ（容器靠 env_file，本地没有）
for _line in pathlib.Path(".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _v = _line.split("=", 1)
    os.environ.setdefault(_k.strip(), _v.strip())

import uvicorn  # noqa: E402

from app.main import create_app  # noqa: E402

uvicorn.run(create_app(), host="127.0.0.1", port=8201, log_level="warning")
