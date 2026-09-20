#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传/下载并发的真实测量（**零成本**：上传不扣积分，下载只花流量）。

为什么要有它：并发是**只有真跑才能验**的那类改动。
假上游（`tests/conftest.py` 的 `FakeUploader`）能验"保序"与"避开了串行逻辑"，
但**验不出真实加速比** —— 那取决于上游往返、连接复用、以及这一步占整体多少。

用法（在容器里跑，用容器内的凭据与环境）：
    docker compose exec -T jimeng-service python - < scripts/bench_concurrency.py

## 实测结论（2026-09-20，本机 Docker Desktop）

    下载 3 张真实 CDN 链接     串行 3.59s(冷) / 1.13s(热) → 并发 0.5s  2.2x ~ 6.7x
    上传 3 张 ~2.9MB PNG       串行 3.30s → 并发 1.26s              2.62x
    STS 取 token               0.2~0.3s，**双检锁** ⇒ 3 张并发时也只取一次

### ⚠️ 读这些数字要注意两件事

1. **加速比是"串行基线"的函数，而基线会漂。** 下载那项两次跑分别得到 6.7x 与 2.2x ——
   差别全在串行基线（首次是**冷 CDN**、第二次有缓存）。**并发的绝对值反而很稳**
   （下载恒在 ~0.5s，上传 ~1.3s）：所以看**绝对值**，别把某一次的倍数当结论。
2. 测的样本状态（热/冷、体积、是否预热 token）必须跟着数字一起报，
   否则同一个脚本换个时间跑会得出"并发没用"或"并发 7 倍"两种错误结论。

两个"看起来该更快、其实不会"的坑，别改回去：

1. **样本太小会让上传并发毫无意义。** 用 8KB 图测只得 1.5x —— 因为每次上传
   固定要走 apply + commit 两段往返，固定开销盖过了并发收益。
   要拿有意义的数字，样本得接近真实体积（数百 KB ~ 数 MB）。

2. **必须先把 STS token 预热掉再比。** 否则并发那一轮的开头会被
   双检锁串起来（第一个线程去取 token，其余等着），量到的是
   "token 往返 + 并发上传"的混合，不是上传本身的并发度。
   （生产里这一步已经由 `Coordinator._prewarm()` 在启动时做掉。）
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from app.config import Settings
from app.media import download, load_images
from app.upstream.jimeng.client import JimengClient
from app.upstream.jimeng.upload import ImageXUploader

#: 用哪次任务的产物当下载样本（签名 URL 会过期，换一个自己刚跑出来的即可）。
SAMPLE_TASK = "jimeng_3420668fcaa44ba9b07d0bf0c2c20b38"
N = 3
LOCAL_API = "http://127.0.0.1:8200"


def timed(fn):
    t0 = time.monotonic()
    out = fn()
    return time.monotonic() - t0, out


def make_png(tag: int, *, size: int = 1000) -> bytes:
    """造一张约 3MB 的 PNG（`compress_level=0` 让体积接近真实照片量级）。"""
    im = Image.new("RGB", (size, size),
                   (tag % 250, (tag * 7) % 250, (tag * 13) % 250))
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=0)
    return buf.getvalue()


def main() -> int:
    s = Settings.from_env()
    client = JimengClient(sessionid=s.jimeng_sessionid, cookie=s.jimeng_cookie,
                          base=s.jimeng_base_url,
                          workspace_id=s.jimeng_workspace_id,
                          poll_interval=s.jimeng_poll_interval,
                          capture_upstream=False)

    # ---------------------------------------------------------------- 下载
    req = urllib.request.Request(
        f"{LOCAL_API}/async/v1/images/generations/{SAMPLE_TASK}",
        headers={"Authorization": f"Bearer {s.api_keys[0]}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        urls = [it["url"] for it in json.loads(r.read())["data"][:N]]
    print(f"下载样本：{len(urls)} 个真实 CDN 链接")

    serial_dl, _ = timed(lambda: [download(u, s) for u in urls])
    par_dl, blobs = timed(lambda: load_images(urls, s))
    # 顺序是**语义要求**（垫图先后影响生成），并发最容易在这里出错
    assert [b.src for b in blobs] == urls, "下载结果顺序错了！"
    print(f"【下载】{len(urls)} 张  串行 {serial_dl:5.2f}s → 并发 {par_dl:5.2f}s"
          f"  {serial_dl / max(par_dl, 1e-6):.2f}x   顺序正确 ✓")

    # ---------------------------------------------------------------- 上传
    # 两轮用**内容不同**的图：上传器自带内容哈希缓存，同一份字节第二轮会零请求命中
    imgs_a = [make_png(t) for t in (11, 22, 33)]
    imgs_b = [make_png(t) for t in (44, 55, 66)]
    print(f"\n上传样本：{N} 张 ~{len(imgs_a[0]) // 1024}KB PNG"
          f"（3 串行 / 3 并发，内容互不相同）")

    up_warm = ImageXUploader(client)
    t_token, _ = timed(lambda: up_warm.token())
    print(f"【STS token】{t_token:.2f}s（双检锁：3 张并发也只取一次）")

    # 两轮都**先预热 token**，把这一段从对比里排除，单看上传本身的并发度
    up_a = ImageXUploader(client)
    up_a.token()
    serial_up, _ = timed(lambda: [up_a.upload(x) for x in imgs_a])

    up_b = ImageXUploader(client)
    up_b.token()
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=N) as pool:
        uris = list(pool.map(up_b.upload, imgs_b))
    par_up = time.monotonic() - t0
    print(f"【上传】{N} 张 {len(imgs_a[0]) // 1024}KB  串行 {serial_up:5.2f}s"
          f" → 并发 {par_up:5.2f}s  {serial_up / max(par_up, 1e-6):.2f}x"
          f"   不同 uri {len(set(uris))} 个 ✓")

    total_serial, total_par = serial_dl + serial_up, par_dl + par_up
    print(f"\n合计：串行 {total_serial:.2f}s → 并发 {total_par:.2f}s"
          f"（省 {total_serial - total_par:.2f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
