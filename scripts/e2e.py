#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端探针 —— **零成本优先，花钱那段要显式开闸**。

分五个阶段，前四个**一分钱不花**：

| 阶段 | 花积分 | 干什么 |
|---|---|---|
| `models`   | ❌ | `GET /async/v1/models` 契约自检 |
| `accept`   | ❌ | `POST` 受理，断言**只回一个 task_id**，再查一次状态（协调器不推进 ⇒ 零上游往返） |
| `upload`   | ❌ | **真实**跑一遍火山 ImageX 四段式上传本地图 → `image_uri`，并用 `get_image_by_uri` 免费验真 |
| `dry`      | ❌ | 用 `submit(dry_run=True)` 走完草稿构造与张数吸附，**不发任何请求**，打印将要提交的内容 |
| `generate` | 🔴 | 真建任务并轮询到终态。**必须显式 `--allow-real-submit`**，且会先打印单价 |

## 为什么把 `generate` 单独隔出来

建任务是**计费动作**，而即梦**失败了也照样扣积分**（`status=30 generate_failed` 也收费）。
所以这里沿用上游参考仓的双闸门做法：不显式开闸时，`generate` 阶段只打印
"将要发生什么"，一个字节都不发。

## 用法

```bash
# 零成本四阶段（推荐先跑这个）
python scripts/e2e.py --cookie-file /path/to/cookie_jimeng.txt

# 真实出图（会扣积分，先看清打印的单价）
python scripts/e2e.py --cookie-file … --phases generate \
    --model jimeng-hd --image /path/to/local.png --allow-real-submit
```

⚠️ 跑真实出图前先确认：`--model` 的单价、以及**这张图是你愿意花掉的**。
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: 实测单价（积分/次）。`outpaint` 一次出 4 张、按 4 张计费。
COST = {
    "jimeng-t2i": 44, "jimeng-i2i": 40, "jimeng-hd": 9,
    "jimeng-pro-hd": 91, "jimeng-outpaint": 28,
}
#: 默认只跑不花钱的四个阶段。
SAFE_PHASES = ("models", "accept", "upload", "dry")
ALL_PHASES = SAFE_PHASES + ("generate",)

OK = "  ✅"
NO = "  ❌"


def _make_test_image(size: int = 1024) -> bytes:
    """造一张本地测试图（有 Pillow 就造渐变，没有就退化成 1×1 PNG）。"""
    try:
        import io

        from PIL import Image

        buf = io.BytesIO()
        img = Image.new("RGB", (size, size))
        px = img.load()
        for y in range(size):
            for x in range(size):
                px[x, y] = ((x * 255) // size, (y * 255) // size, 96)
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6360000002000100cd0f4c9c0000000"
            "049454e44ae426082")


def _read_cookie(path: str) -> str:
    raw = Path(path).expanduser().read_text(encoding="utf-8").strip()
    # 只取 sessionid —— 即梦唯一的硬前提；很多 cookie 文件是整串，这里顺手解析
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == "sessionid" and v.strip():
            return v.strip()
    if raw and "=" not in raw:
        return raw          # 文件里就只存了 sessionid 值
    raise SystemExit(f"在 {path} 里找不到 sessionid")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookie-file", help="含 sessionid 的 cookie 文件（不打印、不入库）")
    ap.add_argument("--sessionid", help="直接给 sessionid（默认读 env JIMENG_SESSIONID）")
    ap.add_argument("--phases", default=",".join(SAFE_PHASES),
                    help=f"逗号分隔，可选 {','.join(ALL_PHASES)}（默认 {','.join(SAFE_PHASES)}）")
    ap.add_argument("--model", default="jimeng-t2i", help="generate 阶段用哪个能力")
    ap.add_argument("--prompt", default="一只在窗台上晒太阳的橘猫，柔和光线")
    ap.add_argument("--image", help="输入图路径（i2i / hd / pro-hd / outpaint 需要）")
    ap.add_argument("--size", default="2048x2048")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=300.0, help="generate 阶段轮询上限（秒）")
    ap.add_argument("--allow-real-submit", action="store_true",
                    help="🔴 允许真实建任务（扣积分）")
    args = ap.parse_args()

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    bad = [p for p in phases if p not in ALL_PHASES]
    if bad:
        raise SystemExit(f"未知阶段 {bad}；可选 {ALL_PHASES}")

    sid = args.sessionid or (os.environ.get("JIMENG_SESSIONID") or "").strip()
    if not sid and args.cookie_file:
        sid = _read_cookie(args.cookie_file)
    if not sid:
        raise SystemExit("缺少登录态：给 --cookie-file / --sessionid，或设 JIMENG_SESSIONID")

    # 单进程直连：不走 gunicorn，也不启后台协调器线程 ——
    # 推进与否完全由本探针控制（`COORDINATOR_ENABLED=0` + 手动 tick）。
    os.environ.setdefault("TASK_DB", "postgresql+psycopg2://jimeng:jimeng@127.0.0.1:5433/jimeng")
    os.environ["COORDINATOR_ENABLED"] = "0"
    os.environ["API_KEYS"] = "sk-e2e-local"
    os.environ["JIMENG_SESSIONID"] = sid
    os.environ.setdefault("LOG_LEVEL", "WARNING")

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    svc = app.state.service
    client_ = TestClient(app)
    auth = {"Authorization": "Bearer sk-e2e-local"}
    failures: list[str] = []

    def step(name: str, fn) -> None:
        print(f"\n── {name} " + "─" * max(0, 46 - len(name)))
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append(name)
            print(f"{NO} {name} 失败：{type(e).__name__}: {e}")

    with client_:
        # ---------------------------------------------------------- models
        if "models" in phases:
            def _models():
                r = client_.get("/async/v1/models")
                assert r.status_code == 200, r.text
                ids = [m["id"] for m in r.json()["data"]]
                print(f"{OK} 能力清单 {ids}")
                for m in ids:
                    print(f"      {m:16s} {COST.get(m, '?')} 积分/次")
            step("models", _models)

        task_id = None

        # ---------------------------------------------------------- accept
        if "accept" in phases:
            def _accept():
                nonlocal task_id
                body = {"model": args.model, "prompt": args.prompt, "image": []}
                r = client_.post("/async/v1/images/generations", json=body, headers=auth)
                assert r.status_code == 202, f"{r.status_code} {r.text}"
                got = r.json()
                assert set(got) == {"task_id"}, f"受理响应只该有 task_id，实得 {set(got)}"
                task_id = got["task_id"]
                print(f"{OK} 受理 → {task_id}（只回一个 id）")
                st = client_.get(f"/async/v1/images/generations/{task_id}",
                                 headers=auth).json()
                assert st["status"] == "queued", st
                print(f"{OK} 状态 {st['status']}（协调器没跑 ⇒ 零上游往返）")
            step("accept", _accept)

        # ---------------------------------------------------------- upload
        if "upload" in phases:
            def _upload():
                data = (Path(args.image).read_bytes() if args.image
                        else _make_test_image())
                print(f"      本地图 {len(data)} 字节（上传**不扣积分**）")
                t0 = time.perf_counter()
                uri = svc.uploader.upload(data)
                ms = (time.perf_counter() - t0) * 1000
                print(f"{OK} 上传成功 {uri}（{ms:.0f}ms，cached={svc.uploader.last_cached}）")
                info = svc.client.image_by_uri(uri)
                assert uri in info, f"get_image_by_uri 没认到它：{list(info)}"
                print(f"{OK} 免费验真通过（get_image_by_uri 认到了它）")
                # 同一份内容再来一次 ⇒ 应命中内容哈希缓存、零请求
                t1 = time.perf_counter()
                uri2 = svc.uploader.upload(data)
                ms2 = (time.perf_counter() - t1) * 1000
                assert uri2 == uri, "同一份字节应命中缓存并返回同一个 uri"
                print(f"{OK} 二次上传命中缓存 {ms2:.0f}ms（第二次几乎零成本）")
            step("upload", _upload)

        # ---------------------------------------------------------- dry
        if "dry" in phases:
            def _dry():
                from app.upstream.jimeng import DEFAULT_MODEL
                sid_ = svc.client.submit(args.prompt, model=DEFAULT_MODEL,
                                         size=args.size, count=args.n, dry_run=True)
                print(f"{OK} 草稿构造通过（dry_run，**未发任何请求**），submit_id={sid_}")
                print(f"      将要提交：model={DEFAULT_MODEL} size={args.size} n={args.n}")
                print(f"      告警：{svc.client.last_warnings or '无'}")
            step("dry", _dry)

        # ---------------------------------------------------------- generate
        if "generate" in phases:
            cost = COST.get(args.model, "未知")
            print("\n── generate " + "─" * 36)
            if not args.allow_real_submit:
                print(f"{NO} 已跳过：未传 --allow-real-submit（这是刻意的双闸门）")
                print(f"      将会做：建任务 + 轮询到终态，model={args.model}，"
                      f"预估 **{cost} 积分**")
                print("      要真跑就加：--allow-real-submit")
            else:
                if args.model not in COST:
                    raise SystemExit(f"未知 model {args.model}，无法预估花费，拒绝执行")
                print(f"  🔴 即将真实建任务：{args.model}，预估 **{cost} 积分**"
                      f"（上游失败也照样计费）")

                def _generate():
                    nonlocal task_id
                    # 接口收的是 URL / data URI / base64 —— **不收本地路径**，
                    # 所以本地图要在这里转成 data URI（服务侧再去下载/解码并上传）。
                    imgs: list[str] = []
                    if args.image:
                        raw = Path(args.image).expanduser().read_bytes()
                        imgs = ["data:image/png;base64," + base64.b64encode(raw).decode()]
                        print(f"      输入图 {len(raw)} 字节（以 data URI 传入）")
                    if not task_id:
                        r = client_.post("/async/v1/images/generations",
                                         json={"model": args.model, "prompt": args.prompt,
                                               "image": imgs}, headers=auth)
                        assert r.status_code == 202, f"{r.status_code} {r.text}"
                        task_id = r.json()["task_id"]
                    print(f"      task_id={task_id}")
                    deadline = time.time() + args.timeout
                    last = None
                    while time.time() < deadline:
                        app.state.coordinator.tick()      # 手动推进（未启后台线程）
                        body = client_.get(
                            f"/async/v1/images/generations/{task_id}",
                            headers=auth).json()
                        s = body.get("status", "success")
                        if s != last:
                            print(f"      … {s}")
                            last = s
                        if s in ("success", "failure", "canceled"):
                            if s == "success":
                                urls = [d["url"] for d in body["data"]]
                                print(f"{OK} 出图 {len(urls)} 张，usage={body.get('usage')}")
                                for u in urls:
                                    print(f"      {u[:110]}…")
                            else:
                                print(f"{NO} 任务失败：{body.get('error')}")
                                failures.append("generate")
                            return
                        time.sleep(2.0)
                    failures.append("generate")
                    print(f"{NO} 轮询超时（{args.timeout}s）")
                step("generate", _generate)

    print("\n" + "=" * 56)
    if failures:
        print(f"结果：{len(failures)} 个阶段失败 → {failures}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
