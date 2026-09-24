#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：打印即梦积分余额与近期消耗记录（**不扣费、不出图**）。

用法：
    python scripts/credit_probe.py                 # 余额 + 最近 20 条
    python scripts/credit_probe.py --count 50      # 自动分页（每页 20）
    python scripts/credit_probe.py --find <submit_id 前缀>   # 分页找，直到命中或翻完

口径（见 docs/UPSTREAM.md §11.5）：`user_credit_history` 的 `records` /
`total_credit` 嵌在响应的 `data` 下；**结算有延迟** —— 刚提交的任务可能还没出账，
所以"没看到记录"**不能**当成免费。

🔴 分页（2026-09-23 实测）：单页 `count` 上限 **20**（>20 ⇒ `ret=1000 invalid
param`，表现为"查询失败"而非少给数据）；翻页用响应里的 `new_cursor`
（形如 `"<ts>:<id>"`），逐页记录无重叠，`has_more` 判终止。
本脚本自动按 20/页翻到够数（或 `--find` 命中 / 触到 `--max-pages`）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PATH_CREDIT_HISTORY = "/commerce/v1/benefits/user_credit_history"

#: 服务端单页上限 —— 超过会被拒（ret=1000），别再试。
PAGE_MAX = 20


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
    ap.add_argument("--find", help="只看 submit_id 以它开头的记录（不区分大小写）；自动分页找")
    ap.add_argument("--max-pages", type=int, default=10, help="分页上限（防失控翻页）")
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
    balance = None
    records: list[dict] = []
    cursor = "0"
    pages = 0
    try:
        for _ in range(max(1, args.max_pages)):
            want = PAGE_MAX if args.find else min(PAGE_MAX, args.count - len(records))
            if not args.find and want <= 0:
                break
            resp = client._post(PATH_CREDIT_HISTORY,          # noqa: SLF001
                                {"count": max(1, want), "cursor": cursor,
                                 "history_type": 2})
            pages += 1
            data = resp.get("data") or {}
            if balance is None:
                balance = data.get("total_credit")
            page = data.get("records") or []
            records.extend(page)
            if args.find and any(
                    str(r.get("submit_id") or "").lower().startswith(args.find.lower())
                    for r in page):
                break
            if not args.find and len(records) >= args.count:
                break
            nxt = str(data.get("new_cursor") or "")
            if not data.get("has_more") or not page or not nxt or nxt == cursor:
                break
            cursor = nxt
    finally:
        client.close()

    print(f"余额 total_credit = {balance}（共翻 {pages} 页，累计 {len(records)} 条）")
    shown = 0
    for r in records:
        sid_ = str(r.get("submit_id") or "")
        if args.find and not sid_.lower().startswith(args.find.lower()):
            continue
        shown += 1
        print(f"  {r.get('create_time')} | amount={r.get('amount')} | "
              f"{str(r.get('title') or '')[:34]:<34} | submit_id={sid_[:24]}")
    if args.find and not shown:
        print(f"  未找到 submit_id 以 {args.find!r} 开头的记录"
              f"（已翻 {pages} 页）—— 注意结算有延迟，别急着下'免费'结论")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
