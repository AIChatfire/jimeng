#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""即梦（jimeng.jianying.com）请求签名 —— **纯算法，零浏览器**。

来源（静态取证）：站点 bundle `static/js/byted-image.f1b8bcce99.js` 的模块 `561634`。
原文：

    let m = "8.4.0";
    function sign({url, pf, appvr, tdid}) {
      let {pathname: o} = new URL(url);
      let i = Math.floor(Date.now() / 1e3);
      return {
        sign: md5(`9e2c|${o.slice(-7)}|${pf}|${appvr}|${i}|${tdid}|11ac`).toLowerCase(),
        "device-time": i,
        tdid: r,
      };
    }

即 **`sign` 是「路径末 7 字符 + 常量」的 MD5**，不依赖 cookie、不依赖请求体、
与 query 无关 —— 所以它可以完全离线重放。

两点容易踩的坑，都已固化：
  1. **`o.slice(-7)` 取的是 pathname 的末 7 个字符**，不是能力名、也不是末 7 个路径段。
     `/mweb/v1/aigc_draft/generate` → `enerate`（注意没有 `g`）、
     `/mweb/v1/get_history_by_ids` → `_by_ids`。写错一个字符就是 100% 不通。
  2. **`device-time` 必须等于参与签名的那一秒**，签名与这个头拆开算都会失败。

🔴 **但 `sign` 不参与鉴权**（实测负对照，读写端点一致）：不带它的请求照常 `ret=0` /
`ret=1002`，与带的完全相同 ⇒ 唯一的硬前提是 cookie 里的 `sessionid`。
上游码表里确有 `1014 ErrSign` ⇒ 校验实现存在、可能按风控力度动态开启，
故本实现**照带但不依赖**（纯 MD5、零成本）。这一点很重要：它意味着
**sign 算法哪天失效，本服务不会整体挂掉**，只是少了一层"贴合浏览器形态"。

`SELF_TEST_VECTORS` 是 11 条**真实抓包**的 (path, ts) → sign 映射，
`verify_vectors()` 直接断言 —— 上游改版改掉 `9e2c`/`11ac` 或改掉切片长度时，
这里会立刻红，而不是等到线上 403 才发现。
"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlsplit

# ---------------------------------------------------------------- 站点常量
APPID = "513695"
APPVR = "8.4.0"          # 请求头 appvr；bundle 里是硬编码的 m = "8.4.0"
PF = "7"                 # 请求头 pf；bundle 默认取 s8.getVar("pf") ?? "7"
SIGN_VER = "1"
DA_VERSION = "3.3.28"    # query 参数（前端版本号，与 appvr 是两套，别混）
WEB_VERSION = "7.5.0"
APP_SDK_VERSION = "48.0.0"

_SIGN_PREFIX = "9e2c"
_SIGN_SUFFIX = "11ac"
_TAIL_LEN = 7


def sign_payload(pathname: str, *, ts: int, pf: str = PF, appvr: str = APPVR,
                 tdid: str = "") -> str:
    """拼出参与 MD5 的原文（单独暴露，方便排障时打日志/比对）。"""
    return f"{_SIGN_PREFIX}|{pathname[-_TAIL_LEN:]}|{pf}|{appvr}|{ts}|{tdid}|{_SIGN_SUFFIX}"


def sign_headers(url_or_path: str, *, ts: int | None = None,
                 pf: str = PF, appvr: str = APPVR, tdid: str = "") -> dict[str, str]:
    """算出该请求应带的签名头（6 个一起给）。

    `url_or_path` 可传完整 URL（含 query）或裸 pathname —— 两种情况都只取 pathname，
    因为签名**不看 query**。

    返回的 `device-time` 与 `sign` 出自同一个 `ts`：**这是同一个原子的两半**，
    调用方不要再自行覆盖 `device-time`。
    """
    pathname = urlsplit(url_or_path).path or "/"
    ts = int(time.time()) if ts is None else int(ts)
    sig = hashlib.md5(
        sign_payload(pathname, ts=ts, pf=pf, appvr=appvr, tdid=tdid).encode()
    ).hexdigest().lower()
    return {
        "sign": sig,
        "sign-ver": SIGN_VER,
        "device-time": str(ts),
        "pf": pf,
        "appvr": appvr,
        "tdid": tdid,
    }


#: 真实抓包向量：(pathname, device-time, sign)。任何一条不过 ⇒ 算法已失效。
#:
#: 11 条来自 **6 组独立抓包**（2026-09-18，覆盖文生图 / 智能超清 / 超清 / 扩图 /
#: 细节修复各一次 generate + 一次 get_history）。向量多不是为了凑数：
#: 它们把算法**钉死在跨端点、跨时间**上都成立，而不是"碰巧对上一条"。
SELF_TEST_VECTORS: tuple[tuple[str, int, str], ...] = (
    ("/mweb/v1/aigc_draft/generate", 1789742348, "103979f37bb61af3a5a6d0a53f9e5992"),
    ("/mweb/v1/get_history_by_ids", 1789742352, "a6def80120c292743fa98f189898e112"),
    ("/mweb/v1/aigc_draft/generate", 1789744838, "c735ac6365447728451e5ff3cb15244d"),
    ("/mweb/v1/get_history_by_ids", 1789744874, "571a5ba7496c5303a26bcc0d950ad017"),
    ("/mweb/v1/aigc_draft/generate", 1789744911, "fd161293051c3416ac41091a0409303d"),
    ("/mweb/v1/get_history_by_ids", 1789744921, "a77b7ae8b993cba5173afb5a69eccc08"),
    ("/mweb/v1/aigc_draft/generate", 1789745001, "ffb0544074b0d2b79a5f95c495f79df7"),
    ("/mweb/v1/get_history_by_ids", 1789745045, "a334a3334276c6f5c5445f85dacc7032"),
    ("/mweb/v1/aigc_draft/generate", 1789745096, "6671e841e9d622f1d4540b8dc5af63ee"),
    ("/mweb/v1/get_history_by_ids", 1789745122, "a435619131b30121f0815dda1115fd3e"),
    ("/mweb/v1/get_upload_token", 1789746355, "166eea62cb5dfc9c50fbcd5382d194fe"),
)


def verify_vectors() -> list[str]:
    """返回不匹配的向量描述列表（空列表 = 全部通过）。"""
    bad: list[str] = []
    for path, ts, expect in SELF_TEST_VECTORS:
        got = sign_headers(path, ts=ts)["sign"]
        if got != expect:
            bad.append(f"{path}@{ts}: 期望 {expect}，实得 {got}")
    return bad


__all__ = [
    "APPID", "APPVR", "PF", "SIGN_VER", "DA_VERSION", "WEB_VERSION",
    "APP_SDK_VERSION", "sign_payload", "sign_headers",
    "SELF_TEST_VECTORS", "verify_vectors",
]


if __name__ == "__main__":  # pragma: no cover - 自检入口
    failed = verify_vectors()
    if failed:
        raise SystemExit("签名自检失败：\n  " + "\n  ".join(failed))
    print(f"签名自检通过（{len(SELF_TEST_VECTORS)} 条真实抓包向量）")
