#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""细节修复（super_resolution）路 A 探针：**最小差异重试**。

与 2026-09-19 两次 generate_failed 形态的唯一残差：
  ❌ 去掉 `origin_image`（不再带 tos uri）
  ✅ 只带 `item_id` + `origin_history_id`（引用账号已有作品 —— 2026-09-20
     真实 UI 抓包里唯一可见的输入承载方式，见 docs/UPSTREAM.md §9.1）

目标作品用抓包里同一条（UI 已用它成功做过细节修复，是已知好目标）：
    item_id          = 7687536700452588862
    origin_history_id = 44853933660428

用法：
    python scripts/detail_fix_probe.py          # dry_run：只打印草稿，零消耗
    python scripts/detail_fix_probe.py --go     # 真提交 1 次（失败也计费！）

⚠️ 这是**计费动作**：历史上两次失败提交都照样扣分。--go 前必须用户明确授权。
提交后轮询到终态，并用 user_credit_history 按 submit_id 对账实扣。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from app.config import Settings
from app.upstream.jimeng.client import JimengClient, build_post_edit_draft


def _load_dotenv() -> None:
    """把仓库根的 .env 灌进 os.environ（已有环境变量优先，绝不打印值）。"""
    p = Path(__file__).resolve().parent.parent / ".env"
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))

ITEM_ID = 7687536700452588862
ORIGIN_HISTORY_ID = 44853933660428
POLL_INTERVAL = 5.0
POLL_TIMEOUT = 300.0
CREDIT_HISTORY_PATH = "/commerce/v1/benefits/user_credit_history"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true",
                    help="真提交（计费！）。缺省只 dry_run 打印草稿")
    args = ap.parse_args()

    _load_dotenv()
    s = Settings.from_env()
    client = JimengClient(sessionid=s.jimeng_sessionid, cookie=s.jimeng_cookie,
                          base=s.jimeng_base_url,
                          workspace_id=s.jimeng_workspace_id,
                          poll_interval=s.jimeng_poll_interval,
                          capture_upstream=False)

    draft = build_post_edit_draft(
        tool="detail", item_id=ITEM_ID, origin_history_id=ORIGIN_HISTORY_ID,
        count=1)
    print("== 草稿形态（路 A：无 origin_image，仅 item_id 引用）==")
    print(json.dumps(json.loads(draft), ensure_ascii=False, indent=2))
    comp = json.loads(draft)["component_list"][0]
    pedit = comp["abilities"]["super_resolution"]["postedit_param"]
    assert "origin_image" not in pedit, "不该带 origin_image"
    assert pedit["item_id"] == ITEM_ID
    assert pedit["origin_history_id"] == ORIGIN_HISTORY_ID
    assert "core_param" in comp["abilities"]["super_resolution"]
    if not args.go:
        print("\n[dry_run] 形态自检通过：无 origin_image、item_id/history 已带、"
              "core_param 已带。加 --go 真提交。")
        return 0

    sid = client.edit(
        tool="detail",
        item_id=ITEM_ID,
        origin_history_id=ORIGIN_HISTORY_ID,
        count=1,
    )

    print(f"\n== 已提交 submit_id={sid}，轮询到终态（≤{POLL_TIMEOUT:.0f}s）==")
    t0 = time.monotonic()
    while time.monotonic() - t0 < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        st = client.fetch(sid)
        print(f"  status={st.status} {st.status_name} finished={st.finished} "
              f"failed={st.failed} images={len(st.images)}")
        if st.finished:
            break
    else:
        print("  超时未到终态（任务可能仍在上游跑）")

    if st.finished:
        if st.failed:
            print(f"\n❌ generate_failed（与历史两次同款）：{st.failed_reason}")
        else:
            print("\n✅ 成功！产物：")
            for im in st.images:
                print(f"  {im.url}  {im.width}x{im.height}")

    # ---- 账单对账（只读；⚠️ records 嵌在 data 下，且结算有延迟）----
    print("\n== user_credit_history 近 10 条 ==")
    try:
        # `client._post` 是私有方法（SLF001）：对账脚本直调是刻意的 ——
        # 它是只读、签名自动带，且公开 API 不含账单接口（同 docs/UPSTREAM.md §12 口径）。
        raw = client._post(  # noqa: SLF001
            CREDIT_HISTORY_PATH, {"count": 10, "cursor": "0", "history_type": 2})
        d = raw.get("data") or {}
        print(f"  total_credit={d.get('total_credit')}")
        for r in d.get("records") or []:
            print(f"  submit={r.get('submit_id')} amount={r.get('amount')} "
                  f"title={r.get('title')} t={r.get('create_time')}")
        if sid in json.dumps(d):
            print(f"  ↑ 本次提交 {sid} 出现在账单里（对上号）")
        else:
            print(f"  本次 {sid} 暂未出现在账单（可能延迟结算，别急着下结论）")
    except Exception as e:  # noqa: BLE001
        print(f"  账单查询失败（不影响主结论）：{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
