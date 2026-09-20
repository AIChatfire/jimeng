#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**只读**拉取即梦服务端模型能力表，筛出**视频**模型并落盘。

纪律：`get_common_config` 是**只读、零成本**接口（不出图、不扣积分）。
本脚本不发任何生成请求 —— 它只回答「服务端到底宣告了哪些视频模型、
各有哪些分辨率/时长/张数档位」，给适配层提供"可读的服务端数据"（别猜）。

用法：
    python scripts/dump_video_models.py            # 打印视频模型摘要
    python scripts/dump_video_models.py --all      # 打印全部模型
    python scripts/dump_video_models.py --raw out.json  # 原始响应落盘

凭据从环境变量 / .env 取（JIMENG_COOKIE 或 JIMENG_SESSIONID）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.upstream.jimeng import JimengClient  # noqa: E402

VIDEO_HINTS = ("seedance", "video", "v2v", "t2v", "i2v", "omni")


def load_env(path: Path) -> dict[str, str]:
    """极简 .env 解析（够用即可：KEY=VALUE，支持引号包裹）。"""
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="打印全部模型，不只视频")
    ap.add_argument("--raw", metavar="PATH", help="原始响应 JSON 落盘")
    args = ap.parse_args()

    env = load_env(REPO / ".env")
    cookie = env.get("JIMENG_COOKIE", "")
    sessionid = env.get("JIMENG_SESSIONID", "")
    ws = env.get("JIMENG_WORKSPACE_ID")
    if not cookie and not sessionid:
        print("缺少凭据：.env 里既没有 JIMENG_COOKIE 也没有 JIMENG_SESSIONID", file=sys.stderr)
        return 2
    client = JimengClient(
        sessionid=sessionid, cookie=cookie,
        workspace_id=int(ws) if ws and ws.isdigit() else None)
    try:
        cfg = client.common_config()
    finally:
        client.close()

    if args.raw:
        Path(args.raw).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"原始响应已写入 {args.raw}")

    data = cfg.get("data") or {}
    models = data.get("model_list") or []
    print(f"模型总数：{len(models)}")
    shown = 0
    for it in models:
        if not isinstance(it, dict):
            continue
        key = (it.get("model_req_key") or it.get("model_key")
               or it.get("model") or it.get("key") or "")
        low = key.lower()
        if not args.all and not any(h in low for h in VIDEO_HINTS):
            continue
        shown += 1
        print("-" * 72)
        print(json.dumps(it, ensure_ascii=False, indent=2)[:4000])
    if not args.all and not shown:
        print("（没有命中视频关键词的模型 —— 换 --all 看全量，能力表可能按场景下发）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
