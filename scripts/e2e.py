#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端探针 —— **零成本优先，花钱那段要显式开闸**。

分五个阶段，前四个**一分钱不花**：

| 阶段 | 花积分 | 干什么 |
|---|---|---|
| `models`   | ❌ | `GET /v1/models` 契约自检（含 `upstream_models` 与声明单价） |
| `accept`   | ❌ | `POST` 受理，断言**只回一个 task_id**，再查一次状态（协调器不推进 ⇒ 零上游往返） |
| `upload`   | ❌ | **真实**跑一遍火山 ImageX 四段式上传本地图 → `image_uri`，并用 `get_image_by_uri` 免费验真 |
| `dry`      | ❌ | 用 `submit(dry_run=True)` 走完草稿构造与张数吸附，**不发任何请求**，打印将要提交的内容 |
| `generate` | 🔴 | 真建任务并轮询到终态。**必须显式 `--allow-real-submit`**，且会先打印单价 |

## 为什么把 `generate` 单独隔出来

建任务是**计费动作**，而即梦**失败了也照样扣积分**（`status=30 generate_failed` 也收费）。
所以这里沿用上游参考仓的双闸门做法：不显式开闸时，`generate` 阶段只打印
"将要发生什么"，一个字节都不发。

## 单价从哪来

🔴 **不再写死**。原先这里那张表（t2i 44 / i2i 40 / hd 9 / pro-hd 91 / outpaint 28）
抄的是上游回执的 `forecast_generate_cost`，实测**高估 4~9 倍**（见 `docs/UPSTREAM.md` §12），
已于 2026-09-23 删除。现在一律读服务自己的 `/v1/models`：
`credits_measured` 是**实测**值，`null` = **未实测**（**不等于免费**）。

## 用法

```bash
# 零成本四阶段（推荐先跑这个）
python scripts/e2e.py                       # 凭据自动读仓库 .env
python scripts/e2e.py --cookie-file /path/to/cookie_jimeng.txt

# 真实出图（会扣积分，先看清打印的单价）
python scripts/e2e.py --phases generate --model jimeng-hd \
    --image /path/to/local.png --allow-real-submit

# 真实跑新模型（面板名也能直接写）
python scripts/e2e.py --phases generate --model "Seedream 5.0 Flash" --allow-real-submit

# 真实跑视频（能力 id 决定走哪个端点，不用加开关）
python scripts/e2e.py --phases generate --model jimeng-t2v-fast \
    --duration 5 --aspect-ratio 16:9 --allow-real-submit
```

⚠️ 跑真实出图前先确认：打印出来的单价、以及**这次花费是你愿意付的**。
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

#: 端点按**媒体**分流（与 `app/main.py` 的装配一致）。
ENDPOINTS = {
    "image": "/async/v1/images/generations",
    "video": "/async/v1/videos/generations",
}
#: 默认只跑不花钱的四个阶段。
SAFE_PHASES = ("models", "accept", "upload", "dry")
ALL_PHASES = SAFE_PHASES + ("generate",)

OK = "  ✅"
NO = "  ❌"
WARN = "  ⚠️ "


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


def _read_env(path: Path) -> dict[str, str]:
    """极简 .env 解析（与 `dump_video_models.py` 同一套，够用即可）。

    🔴 只取值、**不打印**：会话凭据不进日志、不进响应。
    """
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


def _media_of(catalog: dict, model: str) -> str:
    """该 `model` 走哪个端点？**只按服务自己的目录判**（不另起一套推导）。

    目录里没有 ⇒ 它是上游模型 key / web 面板名 ⇒ 文生图族（`resolve` 的规则：
    上游模型只在 t2i 族有意义）。
    """
    for m in catalog.get("data") or []:
        if m.get("id") == model:
            return m.get("media") or "image"
    return "image"


def _declared_cost(catalog: dict, model: str) -> str:
    """服务声明的单价文案。`null` = **未实测**，绝不是"免费"。"""
    for m in catalog.get("data") or []:
        if m.get("id") == model:
            c = m.get("credits_measured")
            return "未实测" if c is None else f"{c} 积分/次（实测）"
    return "未实测（面板名/上游 key：单价是能力级的，见 jimeng-t2i）"


def _pick_sessionid(args: argparse.Namespace) -> str:
    sid = (args.sessionid or os.environ.get("JIMENG_SESSIONID") or "").strip()
    if not sid and args.cookie_file:
        sid = _read_cookie(args.cookie_file)
    if not sid:
        env = _read_env(REPO / ".env")
        sid = (env.get("JIMENG_SESSIONID") or "").strip()
        if not sid and env.get("JIMENG_COOKIE"):
            sid = _read_cookie_value(env["JIMENG_COOKIE"])
    if not sid:
        raise SystemExit(
            "缺少登录态：给 --cookie-file / --sessionid，或设 JIMENG_SESSIONID，"
            "或在仓库 .env 里配 JIMENG_SESSIONID")
    return sid


def _read_cookie_value(raw: str) -> str:
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == "sessionid" and v.strip():
            return v.strip()
    return raw.strip() if "=" not in raw else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookie-file", help="含 sessionid 的 cookie 文件（不打印、不入库）")
    ap.add_argument("--sessionid", help="直接给 sessionid（默认 env / 仓库 .env）")
    ap.add_argument("--phases", default=",".join(SAFE_PHASES),
                    help=f"逗号分隔，可选 {','.join(ALL_PHASES)}（默认 {','.join(SAFE_PHASES)}）")
    ap.add_argument("--model", default="jimeng-t2i",
                    help="能力 id / 裸能力名 / web 面板名 / 上游模型 key")
    ap.add_argument("--prompt", default="一只在窗台上晒太阳的橘猫，柔和光线")
    ap.add_argument("--image", help="输入图路径（i2i / hd / pro-hd / outpaint 需要）")
    ap.add_argument("--size", default="2048x2048")
    ap.add_argument("--n", type=int, default=1)
    # 视频族参数（图片族会忽略它们）
    ap.add_argument("--resolution", default="720p", help="视频：分辨率档位")
    ap.add_argument("--duration", type=int, default=5, help="视频：时长（秒）")
    ap.add_argument("--aspect-ratio", default="16:9", help="视频：画面比例")
    ap.add_argument("--timeout", type=float, default=300.0, help="generate 阶段轮询上限（秒）")
    ap.add_argument("--allow-real-submit", action="store_true",
                    help="🔴 允许真实建任务（扣积分）")
    args = ap.parse_args()

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    bad = [p for p in phases if p not in ALL_PHASES]
    if bad:
        raise SystemExit(f"未知阶段 {bad}；可选 {ALL_PHASES}")

    sid = _pick_sessionid(args)

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
    catalog: dict = {}

    def step(name: str, fn) -> None:
        print(f"\n── {name} " + "─" * max(0, 46 - len(name)))
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append(name)
            print(f"{NO} {name} 失败：{type(e).__name__}: {e}")

    with client_:
        # ---------------------------------------------------------- models
        def _models():
            nonlocal catalog
            r = client_.get("/v1/models", headers=auth)
            assert r.status_code == 200, r.text
            catalog = r.json()
            print(f"{OK} 能力清单 {len(catalog['data'])} 项")
            for m in catalog["data"]:
                c = m.get("credits_measured")
                money = "未实测" if c is None else f"{c} 积分/次"
                print(f"      {m['id']:18s} {m['media']:5s} {money}")
            t2i = next((m for m in catalog["data"] if m["id"] == "jimeng-t2i"), None)
            for u in (t2i or {}).get("upstream_models") or []:
                print(f"        ↳ {u['key']:24s} {u['web_name']}")
        if "models" in phases:
            step("models", _models)
        if not catalog:                      # 没跑 models 阶段也要有目录做判据
            catalog = client_.get("/v1/models", headers=auth).json()

        media = _media_of(catalog, args.model)
        ep = ENDPOINTS[media]
        task_id = None

        # ---------------------------------------------------------- accept
        def _body(imgs: list[str]) -> dict:
            if media == "video":
                return {"model": args.model, "prompt": args.prompt,
                        "resolution": args.resolution, "duration": args.duration,
                        "aspect_ratio": args.aspect_ratio}
            #: 🔴 `size` / `n` **必须带上**：漏掉的话调用方传了 `--n 2` 却只出 1 张，
            #: 看起来"一切正常"——而这正是本仓最不能接受的**静默失效**
            #: （2026-09-23 实测踩到：`--n 2` 落库仍是 n=1，白跑一轮才发现）。
            return {"model": args.model, "prompt": args.prompt, "image": imgs,
                    "size": args.size, "n": args.n}

        if "accept" in phases:
            def _accept():
                nonlocal task_id
                r = client_.post(ep, json=_body([]), headers=auth)
                assert r.status_code == 202, f"{r.status_code} {r.text}"
                got = r.json()
                assert set(got) == {"task_id"}, f"受理响应只该有 task_id，实得 {set(got)}"
                task_id = got["task_id"]
                print(f"{OK} 受理 → {task_id}（只回一个 id）")
                st = client_.get(f"{ep}/{task_id}", headers=auth).json()
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
                from app.models import resolve
                from app.upstream.jimeng import DEFAULT_MODEL

                # 走一遍**服务自己的解析**，把"面板名 → 上游模型"这一步也验到
                cap, upstream = resolve(args.model, has_image=bool(args.image),
                                        video=(media == "video"))
                print(f"{OK} 解析：{args.model!r} → {cap.api_id}"
                      + (f"（上游模型 {upstream}）" if upstream else ""))
                if cap.media == "video":
                    print("      视频族：草稿构造由提交侧负责，dry 只验解析与档位")
                    print(f"      档位：{args.resolution} × {args.duration}s × "
                          f"{args.aspect_ratio}")
                    return
                sid_ = svc.client.submit(args.prompt,
                                         model=upstream or DEFAULT_MODEL,
                                         size=args.size, count=args.n, dry_run=True)
                print(f"{OK} 草稿构造通过（dry_run，**未发任何请求**），submit_id={sid_}")
                print(f"      将要提交：model={upstream or DEFAULT_MODEL} "
                      f"size={args.size} n={args.n}")
                print(f"      告警：{svc.client.last_warnings or '无'}")
            step("dry", _dry)

        # ---------------------------------------------------------- generate
        if "generate" in phases:
            print("\n── generate " + "─" * 36)
            cost = _declared_cost(catalog, args.model)
            if not args.allow_real_submit:
                print(f"{NO} 已跳过：未传 --allow-real-submit（这是刻意的双闸门）")
                print(f"      将会做：建任务 + 轮询到终态，model={args.model}"
                      f"（{media} 族）")
                print(f"      服务声明的花费：**{cost}**")
                print("      要真跑就加：--allow-real-submit")
            else:
                print(f"  🔴 即将真实建任务：{args.model}（{media} 族）")
                print(f"     服务声明的花费：**{cost}**"
                      f"（上游失败也照样计费；未实测≠免费）")
                if media == "image":
                    print(f"     请求：size={args.size} n={args.n}")

                def _generate():
                    nonlocal task_id
                    # 接口收的是 URL / data URI / base64 —— **不收本地路径**，
                    # 所以本地图要在这里转成 data URI（服务侧再去下载/解码并上传）。
                    imgs: list[str] = []
                    if args.image and media == "image":
                        raw = Path(args.image).expanduser().read_bytes()
                        imgs = ["data:image/png;base64," + base64.b64encode(raw).decode()]
                        print(f"      输入图 {len(raw)} 字节（以 data URI 传入）")
                    if not task_id:
                        r = client_.post(ep, json=_body(imgs), headers=auth)
                        assert r.status_code == 202, f"{r.status_code} {r.text}"
                        task_id = r.json()["task_id"]
                    print(f"      task_id={task_id}")
                    deadline = time.time() + args.timeout
                    last = None
                    while time.time() < deadline:
                        app.state.coordinator.tick()      # 手动推进（未启后台线程）
                        body = client_.get(f"{ep}/{task_id}", headers=auth).json()
                        s = body.get("status", "success")
                        if s != last:
                            print(f"      … {s}")
                            last = s
                        if s in ("success", "failure", "canceled"):
                            if s == "success":
                                urls = [d["url"] for d in body["data"]]
                                kind = "出片" if media == "video" else "出图"
                                print(f"{OK} {kind} {len(urls)} 个，"
                                      f"usage={body.get('usage')}")
                                #: 🔴 **少给要报出来**：判据是"交付张数 < 请求的 n"，
                                #: 不是上游的 total/finished 计数（那两个跟的是垫图数）。
                                if media == "image" and len(urls) < args.n:
                                    print(f"{WARN} **少给**：请求 n={args.n}、"
                                          f"只交付 {len(urls)} 张"
                                          f"（按本仓口径这属于如实降级，不是成功）")
                                for d in body.get("degradations") or []:
                                    print(f"{WARN} 降级：{d}")
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
