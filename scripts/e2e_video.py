#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频链路**真实**端到端探针 —— 🔴 会扣积分，必须 --allow-real-submit。

阶段（顺序有依赖）：
  t2v  : 文生视频 720p×4s（实抓档位；回执预扣 4）
  vfi  : 补帧（免费档，源 = t2v 产物，走本服务产物链的 vid/item_id/history）
  omni : 全能参考（图+视频素材；"图生视频"形态）

每阶段后打积分流水（`user_credit_history`，只读）按 submit_id 对账。
用法：
  python scripts/e2e_video.py --phases t2v,vfi,omni --allow-real-submit
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

from app.upstream.jimeng import (  # noqa: E402
    ImageXUploader,
    JimengClient,
    VodUploader,
)

PATH_CREDITS = "/commerce/v1/benefits/user_credit_history"
POLL_INTERVAL = 10.0
POLL_TIMEOUT = 600.0


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def wait_task(client: JimengClient, sid: str) -> tuple:
    """轮询到终态。返回 TaskState。"""
    started = time.monotonic()
    last = None
    while time.monotonic() - started < POLL_TIMEOUT:
        st = client.fetch_many([sid])[sid]
        if st.status != last:
            print(f"    [{int(time.monotonic()-started)}s] status={st.status} "
                  f"{st.status_name}")
            last = st.status
        if st.finished:
            return st
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"任务 {sid} 超过 {POLL_TIMEOUT:.0f}s 未到终态")


def print_credits(client: JimengClient, tag: str, tracked: list[str]) -> None:
    d = client._post(PATH_CREDITS, {"count": 20, "cursor": "0",  # noqa: SLF001
                                    "history_type": 2})
    data = d.get("data") or {}
    print(f"  [对账:{tag}] 余额 total_credit={data.get('total_credit')}")
    for r in (data.get("records") or []):
        if r.get("submit_id") in tracked:
            print(f"    流水: submit_id={r.get('submit_id')} "
                  f"amount={r.get('amount')} title={r.get('title')} "
                  f"time={r.get('create_time')}")


def tiny_png() -> bytes:
    """64x64 纯色 PNG（stdlib 手写，够 ImageX 上传即可）。"""
    import struct
    import zlib
    w = h = 64
    raw = b"".join(b"\x00" + b"\x30\x60\x90" * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phases", default="t2v,vfi,omni")
    ap.add_argument("--allow-real-submit", action="store_true")
    args = ap.parse_args()
    if not args.allow_real_submit:
        print("🔴 会扣积分。确认请加 --allow-real-submit（不加只打印将发生什么）")
        return 2
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    env = load_env(REPO / ".env")
    client = JimengClient(
        sessionid=env["JIMENG_SESSIONID"],
        cookie=env.get("JIMENG_COOKIE", ""),
        workspace_id=int(env["JIMENG_WORKSPACE_ID"])
        if env.get("JIMENG_WORKSPACE_ID", "").isdigit() else None)
    ix = ImageXUploader(client)
    vod = VodUploader(client, imagex=ix)
    tracked: list[str] = []
    result: dict = {}
    try:
        print_credits(client, "开始前", tracked)
        prompt = "一只橘猫在窗台上伸懒腰，阳光洒进屋里，镜头缓缓推近"

        # ------------------------------------------------------------ t2v
        if "t2v" in phases:
            print("\n=== 阶段 t2v（720p×4s，回执预扣 4） ===")
            sid = client.submit_video(prompt, resolution="720p",
                                      duration_ms=4000, aspect_ratio="16:9")
            tracked.append(sid)
            print("  submit_id:", sid)
            st = wait_task(client, sid)
            print(f"  终态: status={st.status} {st.status_name} "
                  f"forecast={st.cost} ok={st.ok}")
            for im in st.images:
                print("  产物:", im.url[:120], "| vid:", im.vid,
                      "| item_id:", im.item_id)
            if not st.ok or not st.images:
                print("  🔴 t2v 失败，后续依赖它的阶段中止")
                return 1
            result["t2v"] = {
                "sid": sid, "vid": st.images[0].vid,
                "item_id": st.images[0].item_id,
                "history": st.history_record_id,
                "draft": client.last_draft, "url": st.images[0].url,
                "prompt": prompt}
            print_credits(client, "t2v后", tracked)

        # ------------------------------------------------------------ vfi
        if "vfi" in phases:
            src = result.get("t2v")
            if not src:
                print("🔴 vfi 需要 t2v 先成功")
                return 1
            print("\n=== 阶段 vfi（补帧 60fps，回执预扣 0 = 免费档） ===")
            sid = client.submit_video_vfi(
                src["draft"], prompt=src["prompt"], vid=src["vid"],
                origin_history_id=src["history"], item_id=src["item_id"],
                resolution="720p", duration_ms=4000, target_fps=60,
                source_submit_id=src["sid"], source_item_id=src["item_id"])
            tracked.append(sid)
            print("  submit_id:", sid)
            st = wait_task(client, sid)
            print(f"  终态: status={st.status} {st.status_name} "
                  f"forecast={st.cost} ok={st.ok}")
            for im in st.images:
                print("  产物:", im.url[:120], "| note:", (im.note or "")[:60])
            result["vfi"] = {"sid": sid, "ok": st.ok,
                             "url": st.images[0].url if st.images else None}
            print_credits(client, "vfi后", tracked)

        # ----------------------------------------------------------- omni
        if "omni" in phases:
            src = result.get("t2v")
            if not src:
                print("🔴 omni 需要 t2v 先成功（用其产物做参考视频）")
                return 1
            print("\n=== 阶段 omni（图+视频 全能参考，『图生视频』形态） ===")
            png = ix.upload(tiny_png())
            print("  图片素材 image_uri:", png)
            print("  下载 t2v 产物做参考视频…")
            data = httpx.get(src["url"], timeout=120,
                             follow_redirects=True).content
            print(f"  参考视频 {len(data)} 字节，上传 VOD…")
            v = vod.upload(data)
            print("  视频素材 vid:", v["vid"])
            materials = [
                {"kind": "image", "uri": png, "width": 64, "height": 64,
                 "name": "poster"},
                {"kind": "video", "uri": v["vid"],
                 "width": (v.get("commit") or {}).get("Results", [{}])[0]
                 .get("VideoMeta", {}).get("Width") or 0,
                 "height": (v.get("commit") or {}).get("Results", [{}])[0]
                 .get("VideoMeta", {}).get("Height") or 0},
            ]
            sid = client.submit_video_omni(
                "参考视频的运镜与构图，参考图片作为首帧贴片风格，"
                "画面主体换成一只柯基犬在海边奔跑",
                materials=materials, resolution="720p", duration_ms=5000,
                aspect_ratio="16:9", seed=7)
            tracked.append(sid)
            print("  submit_id:", sid)
            st = wait_task(client, sid)
            print(f"  终态: status={st.status} {st.status_name} "
                  f"forecast={st.cost} ok={st.ok}")
            for im in st.images:
                print("  产物:", im.url[:120])
            result["omni"] = {"sid": sid, "ok": st.ok,
                              "url": st.images[0].url if st.images else None}
            print_credits(client, "omni后", tracked)

        print("\n=== 汇总 ===")
        print(json.dumps({k: {kk: vv for kk, vv in v.items()
                              if kk != "draft"}
                          for k, v in result.items()},
                         ensure_ascii=False, indent=1, default=str))
        return 0
    finally:
        vod.close()
        ix.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
