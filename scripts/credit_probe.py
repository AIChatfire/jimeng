#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：打印即梦积分余额与近期消耗记录（**不扣费、不出图**）。

用法：
    python scripts/credit_probe.py                 # 余额 + 最近 20 条
    python scripts/credit_probe.py --count 50
    python scripts/credit_probe.py --find <submit_id 前缀>

口径（见 docs/UPSTREAM.md §11.5）：`user_credit_history` 的 `records` /
`total_credit` 嵌在响应的 `data` 下；**结算有延迟** —— 刚提交的任务可能还没出账，
所以"没看到记录"**不能**当成免费。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PATH_CREDIT_HISTORY = "/commerce/v1/benefits/user_credit_history"


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
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--find", help="只看 submit_id 以它开头的记录（不区分大小写）")
    args = ap.parse_args()

    env = _env(REPO / ".env")
    sid = (env.get("JIMENG_SESSIONID") or "").strip()
    cookie = (env.get("JIMENG_COOKIE") or "").strip()
    if not sid and not cookie:
        print("缺少凭据：.env 里既没有 JIMENG_SESSIONID 也没有 JIMENG_COOKIE",
              file=sys.stderr)
        return 2

    from app.upstream.jimeng import JimengClient

    client = JimengClient(sessionid=sid, cookie=cookie,
                          workspace_id=int(env["JIMENG_WORKSPACE_ID"])
                          if (env.get("JIMENG_WORKSPACE_ID") or "").isdigit() else None)
    try:
        resp = client._post(PATH_CREDIT_HISTORY,          # noqa: SLF001
                            {"count": args.count, "cursor": "0", "history_type": 2})
    finally:
        client.close()

    data = resp.get("data") or {}
    print(f"余额 total_credit = {data.get('total_credit')}")
    records = data.get("records") or []
    print(f"记录 {len(records)} 条（新→旧）：")
    for r in records:
        sid_ = str(r.get("submit_id") or "")
        if args.find and not sid_.lower().startswith(args.find.lower()):
            continue
        print(f"  {r.get('create_time')} | amount={r.get('amount')} | "
              f"{str(r.get('title') or '')[:34]:<34} | submit_id={sid_[:24]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
