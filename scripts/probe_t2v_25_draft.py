#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端真跑探针：jimeng-t2v-2.5-draft（**计费动作，实扣约 45 积分**）。

进程内 TestClient（绕开 HTTP_PROXY；真库 = 5433 测试实例 + 独立 schema 隔离）。
跑完自动轮询到终态，打印产物与 forecast；对账用 scripts/credit_probe.py。
"""
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.config import Settings          # noqa: E402
from app.main import create_app          # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if k.strip() and v:
            out[k.strip()] = v
    return out


env = load_env(REPO / ".env")
os.environ.setdefault("JIMENG_SESSIONID", env.get("JIMENG_SESSIONID", ""))
os.environ.setdefault("JIMENG_COOKIE", env.get("JIMENG_COOKIE", ""))

DSN = ("postgresql+psycopg2://jimeng:jimeng@127.0.0.1:5433/jimeng_test"
       "?options=-csearch_path%3Dprobe_25_draft")

# 独立 schema 隔离：先建 schema（conftest 同款做法，避免 search_path 指向不存在）
from sqlalchemy import create_engine, text  # noqa: E402
_eng = create_engine(DSN, pool_pre_ping=True)
with _eng.begin() as conn:
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS probe_25_draft"))
_eng.dispose()

settings = Settings(
    task_db=DSN,
    jm_concurrency=3,
    jimeng_sessionid=os.environ["JIMENG_SESSIONID"],
    jimeng_cookie=os.environ.get("JIMENG_COOKIE", ""),
    jimeng_workspace_id=(int(env["JIMENG_WORKSPACE_ID"])
                         if env.get("JIMENG_WORKSPACE_ID", "").isdigit() else None),
)
app = create_app(settings)

with TestClient(app) as client:
    r = client.post("/async/v1/videos/generations", json={
        "model": "jimeng-t2v-2.5-draft",
        "prompt": "一只橘猫在窗台打盹，阳光洒在毛上，微风吹动胡须，午后光斑摇曳",
        "resolution": "480p",
        "duration": 5,
        "aspect_ratio": "16:9",
    })
    print("submit:", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:400])
    if r.status_code != 202:
        sys.exit(1)
    task_id = r.json()["task_id"]
    deadline = time.time() + 420
    while time.time() < deadline:
        time.sleep(8)
        q = client.get(f"/async/v1/videos/generations/{task_id}")
        body = q.json()
        st = body.get("status")
        print(" poll:", st, "| finished:", body.get("finished_image_count"))
        if st in ("success", "failure", "canceled"):
            print(json.dumps(body, ensure_ascii=False, indent=2)[:1500])
            break
    else:
        print("TIMEOUT waiting terminal state")
