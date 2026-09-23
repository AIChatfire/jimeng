#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：调和器为什么**不派发**排队任务。

零成本 —— 默认用 `jimeng-t2i`（Seedream 5.0 Lite，实测免费），
逐 tick 打印 `coordinator.stats()` / `gate.stats()`，把早退点暴露出来。

用法：
    python scripts/diag_dispatch.py                 # 3 个 tick
    python scripts/diag_dispatch.py --ticks 5 --model jimeng-t2i
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if k.strip() and v:
            out[k.strip()] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=3)
    ap.add_argument("--model", default="jimeng-t2i")
    ap.add_argument("--prompt", default="一只在窗台上晒太阳的橘猫")
    args = ap.parse_args()

    env = _env(REPO / ".env")
    sid = (env.get("JIMENG_SESSIONID") or "").strip()
    if not sid:
        raise SystemExit("仓库 .env 里没有 JIMENG_SESSIONID")

    os.environ.setdefault("TASK_DB",
                          "postgresql+psycopg2://jimeng:jimeng@127.0.0.1:5433/jimeng")
    os.environ["COORDINATOR_ENABLED"] = "0"
    os.environ["API_KEYS"] = "sk-diag-local"
    os.environ["JIMENG_SESSIONID"] = sid
    os.environ.setdefault("LOG_LEVEL", "INFO")

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    svc = app.state.service
    co = app.state.coordinator
    c = TestClient(app)
    auth = {"Authorization": "Bearer sk-diag-local"}

    with c:
        r = c.post("/async/v1/images/generations",
                   json={"model": args.model, "prompt": args.prompt, "image": []},
                   headers=auth)
        print("受理:", r.status_code, r.json())
        tid = r.json()["task_id"]

        print("\nsettings 关键项:",
              json.dumps({"upstream_configured": svc.settings.upstream_configured,
                          "jm_concurrency": svc.settings.jm_concurrency,
                          "coordinator_enabled": svc.settings.coordinator_enabled,
                          "coordinator_lease": svc.settings.coordinator_lease},
                         ensure_ascii=False))
        print("库里各状态计数:",
              {s: svc.store.count_by_status(s)
               for s in ("queued", "in_progress", "success", "failure")})
        print("gate:", json.dumps(svc.gate.stats(), ensure_ascii=False))
        print("cooldown 生效值:", svc.settings.jm_cooldown)

        for i in range(args.ticks):
            co.tick()
            time.sleep(1.0)
            body = c.get(f"/async/v1/images/generations/{tid}", headers=auth).json()
            print(f"\n── tick {i + 1} " + "─" * 30)
            print("coordinator:", json.dumps(co.stats(), ensure_ascii=False))
            print("gate:", json.dumps(svc.gate.stats(), ensure_ascii=False))
            print("task:", body.get("status"), "| degradations:", body.get("degradations"),
                  "| error:", (body.get("error") or {}).get("message"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
