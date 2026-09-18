#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""即梦图片**上传** —— 火山引擎 ImageX 四段式，**纯 stdlib 签名，零浏览器**。

取证来源：2026-09-18 三条真实抓包
（`get_upload_token` / `ApplyImageUpload` / `CommitImageUpload`）。

## 四段式

```
① POST /mweb/v1/get_upload_token   {"scene":2}          ← 站点自己的口，只要 sessionid
      → {access_key_id, secret_access_key, session_token, space_name, upload_domain, ...}
② GET  https://<upload_domain>/?Action=ApplyImageUpload&Version=2018-08-01
        &ServiceId=<space_name>&FileSize=<n>              ← AWS4-HMAC-SHA256 签名（STS 凭据）
      → UploadAddress{ StoreInfos:[{StoreUri, Auth, UploadID}], UploadHost, SessionKey, ... }
③ POST https://<UploadHost>/upload/v1/<StoreUri>          ← 裸字节 + Authorization: <Auth>
④ POST https://<upload_domain>/?Action=CommitImageUpload&Version=2018-08-01
        &ServiceId=<space_name>  body {"SessionKey":"<b64(UploadAddress)>"}
      → 提交后该素材即可被草稿按 `image_uri` 引用
```

`image_uri` 最终形态 = `tos-cn-i-<space_name>/<hash>` —— 正是草稿里
`origin_image.image_uri` 需要的那个值。**这一步不产生任何生成、不扣积分。**

## 签名细节（照抄抓包，不照抄"通用 AWS4 教程"）

抓包里两条请求的 `SignedHeaders` **不一样**，这不是笔误，所以本实现按抓包分别构造：

| 请求 | SignedHeaders |
|---|---|
| `ApplyImageUpload`（GET，无 body） | `x-amz-date;x-amz-security-token`（**不含 host**） |
| `CommitImageUpload`（POST，有 body） | `content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token` |

算法 `AWS4-HMAC-SHA256`，region `cn-north-1`，service `imagex`。

## 三层保护（并发实测后补的，缺一层都会重复上传）

| 层 | 防的是什么 | 依据 |
|---|---|---|
| **STS 双检锁** | 并发 N 条各签一次 STS | 实测并发 8 会发出 8 次 `get_upload_token` |
| **内容哈希 → uri 缓存** | 同一份字节反复走四段式 | 🔴 `image_uri` **不是内容寻址**的：同一份字节上传两次得到两个**不同**的 uri |
| **in-flight 去重** | 并发同内容**全部 miss**、各走一遍 | 实测并发 8 产生 **8 个 uri**（账号里 8 份重复素材） |
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import zlib

import httpx

from .client import JimengClient, JimengError

PATH_TOKEN = "/mweb/v1/get_upload_token"
API_VERSION = "2018-08-01"
REGION = "cn-north-1"
SERVICE = "imagex"

#: `sha256(字节) → (image_uri, space, 时间戳)` 缓存窗口。
#: 🔴 依据（2026-09-19 实测）：`image_uri` **不是内容寻址**的 —— 同一份字节上传两次
#: 得到两个**不同**的 uri ⇒ 不缓存就既重复 4 次 HTTP、又在账号里堆重复素材。
#: ⚠️ 6h 是保守值（**上游是否回收未引用素材未知**）；缓存键就是内容哈希
#: ⇒ 最坏情况是浪费一次上传，**不会静默用错图**。
URI_CACHE_TTL = 6 * 3600.0
URI_CACHE_MAX = 128
#: STS 复用窗口。实测 token 约 1h 有效，取 1500s 留足余量。
STS_REUSE_SECONDS = 1500.0
#: 等待「同一份内容正在上传」的上限（秒）；超时显式报错，不无限等。
UPLOAD_INFLIGHT_WAIT = 120.0


class ImageXError(JimengError):
    """上传链路失败（与生成无关，不消耗积分）。"""


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _uri_encode(s: str, *, encode_slash: bool = True) -> str:
    return urllib.parse.quote(s, safe="-_.~" if encode_slash else "-_.~/")


def aws4_sign(*, method: str, host: str, path: str, query: dict[str, str],
              access_key: str, secret_key: str, session_token: str,
              payload: bytes, signed_headers: tuple[str, ...],
              extra_headers: dict[str, str] | None = None,
              now: _dt.datetime | None = None) -> dict[str, str]:
    """按抓包的形态生成 AWS4 签名头。返回需要附加到请求上的头。"""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")

    hdrs = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-security-token": session_token,
        "x-amz-content-sha256": _sha256_hex(payload),
    }
    hdrs.update(extra_headers or {})

    canonical_query = "&".join(
        f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items()))
    canonical_headers = "".join(f"{h}:{hdrs[h].strip()}\n" for h in signed_headers)
    signed = ";".join(signed_headers)
    canonical_request = "\n".join([
        method, path, canonical_query, canonical_headers, signed,
        hdrs["x-amz-content-sha256"],
    ])
    scope = f"{date}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope, _sha256_hex(canonical_request.encode()),
    ])
    k = _hmac(("AWS4" + secret_key).encode(), date)
    k = _hmac(k, REGION)
    k = _hmac(k, SERVICE)
    k = _hmac(k, "aws4_request")
    sig = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

    out = {
        "x-amz-date": amz_date,
        "x-amz-security-token": session_token,
        "authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={sig}"),
    }
    if "x-amz-content-sha256" in signed_headers:
        out["x-amz-content-sha256"] = hdrs["x-amz-content-sha256"]
    if "content-type" in signed_headers and extra_headers:
        out["content-type"] = extra_headers["content-type"]
    return out


class ImageXUploader:
    """把本地图片传成即梦可引用的 `image_uri`。

    `scene=2` 是抓包里的取值（推测是"图片"场景）；token 有 `expired_time`，
    过期自动重取。
    """

    def __init__(self, client: JimengClient, *, scene: int = 2,
                 timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.c = client
        self.scene = scene
        self.timeout = timeout
        self._token: dict | None = None
        self._token_at: float = 0.0
        #: `sha256(字节)` → `(image_uri, space, 时间戳)`
        self._uri_cache: dict[str, tuple[str, str, float]] = {}
        #: 上一次 `upload()` 是否命中缓存（探针/测试展示用）
        self.last_cached: bool = False
        #: 保护缓存的锁；以及「同一份内容正在上传」的 in-flight 登记表
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._sts_lock = threading.Lock()
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ ① 取 STS

    def token(self, *, force: bool = False) -> dict:
        """取（并复用）STS。**双检锁** —— 并发 N 条只签发一次。

        没有它时实测并发 8 会发出 8 次 `get_upload_token`。
        """
        if not force and self._token and time.time() - self._token_at < STS_REUSE_SECONDS:
            return self._token
        with self._sts_lock:
            if (not force and self._token
                    and time.time() - self._token_at < STS_REUSE_SECONDS):
                return self._token
            return self._token_refill()

    def _token_refill(self) -> dict:
        d = self.c._post(PATH_TOKEN, {"scene": self.scene})  # noqa: SLF001
        data = d.get("data") or {}
        need = ("access_key_id", "secret_access_key", "session_token", "space_name")
        missing = [k for k in need if not data.get(k)]
        if missing:
            raise ImageXError(
                f"get_upload_token 返回缺少字段 {missing}；实得键 {sorted(data)}",
                retryable=False)
        self._token, self._token_at = data, time.time()
        return data

    # ------------------------------------------------------ ② ApplyImageUpload

    def apply(self, file_size: int) -> dict:
        t = self.token()
        host = t.get("upload_domain") or "imagex.bytedanceapi.com"
        query = {"Action": "ApplyImageUpload", "Version": API_VERSION,
                 "ServiceId": t["space_name"], "FileSize": str(file_size)}
        signed = ("x-amz-date", "x-amz-security-token")   # 照抄抓包：不含 host
        h = aws4_sign(method="GET", host=host, path="/", query=query,
                      access_key=t["access_key_id"],
                      secret_key=t["secret_access_key"],
                      session_token=t["session_token"], payload=b"",
                      signed_headers=signed)
        url = f"https://{host}/?" + "&".join(f"{k}={v}" for k, v in query.items())
        try:
            r = self._client.get(url, headers=h)
        except httpx.HTTPError as e:
            raise ImageXError(f"ApplyImageUpload 请求失败：{e}", retryable=True) from e
        res = self._unwrap(r, "ApplyImageUpload")
        # 真实响应：Result.UploadAddress.{StoreInfos, UploadHost, SessionKey}
        addr = res.get("UploadAddress") or res.get("uploadAddress")
        if not isinstance(addr, dict):
            raise ImageXError(
                f"ApplyImageUpload 未返回 UploadAddress（实得键 {sorted(res)}）")
        return addr

    # ------------------------------------------------------------ ③ 上传字节

    def put(self, addr: dict, data: bytes, *, content_crc32: bool = True) -> None:
        si = (addr.get("StoreInfos") or addr.get("storeInfos") or [{}])[0]
        uri = si.get("StoreUri") or si.get("storeUri")
        auth = si.get("Auth") or si.get("auth")
        # 响应里是 UploadHost（**单数**，字符串）；老版本 SDK 用 UploadHosts（数组）——
        # 两种都认。抓包里我们踩过这个坑。
        hosts = addr.get("UploadHosts") or addr.get("uploadHosts") or []
        if not hosts and addr.get("UploadHost"):
            hosts = [addr["UploadHost"]]
        if not (uri and auth and hosts):
            raise ImageXError(
                f"UploadAddress 缺字段（uri={bool(uri)} auth={bool(auth)} "
                f"hosts={hosts}）：{sorted(addr)}")
        host = hosts[0]
        headers = {"authorization": auth, "content-type": "application/octet-stream"}
        if content_crc32:
            headers["content-crc32"] = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
        try:
            r = self._client.post(f"https://{host}/upload/v1/{uri}",
                                  headers=headers, content=data)
        except httpx.HTTPError as e:
            raise ImageXError(f"上传字节失败：{e}", retryable=True) from e
        if r.status_code >= 400:
            raise ImageXError(
                f"上传字节被拒：HTTP {r.status_code} {r.text[:200]!r}",
                retryable=r.status_code >= 500)

    # ------------------------------------------------------ ④ CommitImageUpload

    def commit(self, addr: dict) -> dict:
        t = self.token()
        host = t.get("upload_domain") or "imagex.bytedanceapi.com"
        # SessionKey = base64(UploadAddress) —— 抓包里就是这个（已解出验证）
        session_key = addr.get("SessionKey") or base64.b64encode(
            json.dumps(addr, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode()
        body = json.dumps({"SessionKey": session_key},
                          ensure_ascii=False, separators=(",", ":")).encode()
        query = {"Action": "CommitImageUpload", "Version": API_VERSION,
                 "ServiceId": t["space_name"]}
        signed = ("content-type", "host", "x-amz-content-sha256",
                  "x-amz-date", "x-amz-security-token")   # 照抄抓包
        h = aws4_sign(method="POST", host=host, path="/", query=query,
                      access_key=t["access_key_id"],
                      secret_key=t["secret_access_key"],
                      session_token=t["session_token"], payload=body,
                      signed_headers=signed,
                      extra_headers={"content-type": "application/json"})
        url = f"https://{host}/?" + "&".join(f"{k}={v}" for k, v in query.items())
        try:
            r = self._client.post(url, headers=h, content=body)
        except httpx.HTTPError as e:
            raise ImageXError(f"CommitImageUpload 请求失败：{e}", retryable=True) from e
        return self._unwrap(r, "CommitImageUpload")

    # ---------------------------------------------------------------- 一步到位

    def upload(self, data: bytes, *, content_crc32: bool = True,
               dry_run: bool = False) -> str:
        """把字节传上去，返回可直接放进草稿的 `image_uri`。

        **不产生生成、不扣积分。**
        `dry_run=True` 时只取 STS 并返回将发请求的摘要，不上传任何字节。
        **同一份内容命中缓存时直接返回既有 uri（零请求）**。
        """
        t = self.token()
        if dry_run:
            return (f"dry-run: space={t['space_name']} "
                    f"domain={t.get('upload_domain')} bytes={len(data)}")
        digest = hashlib.sha256(data).hexdigest()
        hit = self._uri_cache.get(digest)
        if hit and time.time() - hit[2] < URI_CACHE_TTL:
            self.last_cached = True
            return hit[0]

        claim = threading.Event()
        with self._lock:
            hit = self._uri_cache.get(digest)          # 双检
            if hit and time.time() - hit[2] < URI_CACHE_TTL:
                self.last_cached = True
                return hit[0]
            waiter = self._inflight.get(digest)
            leader = waiter is None
            if leader:
                self._inflight[digest] = claim
        if not leader:
            if not waiter.wait(timeout=UPLOAD_INFLIGHT_WAIT):
                raise ImageXError(
                    f"等待同一份内容的上传超时（{UPLOAD_INFLIGHT_WAIT:.0f}s）")
            hit = self._uri_cache.get(digest)
            if hit and time.time() - hit[2] < URI_CACHE_TTL:
                self.last_cached = True
                return hit[0]
            raise ImageXError("同一份内容的上传刚失败过（等待者不再重试）")
        try:
            self.last_cached = False
            return self._upload_uncached(t, data, digest, content_crc32)
        finally:
            with self._lock:
                self._inflight.pop(digest, None)
            claim.set()

    def _upload_uncached(self, t: dict, data: bytes, digest: str,
                         content_crc32: bool = True) -> str:
        """四段式本体。调用方已保证「同一份内容同时只有一个线程在执行它」。"""
        addr = self.apply(len(data))
        si = (addr.get("StoreInfos") or addr.get("storeInfos") or [{}])[0]
        store_uri = si.get("StoreUri") or si.get("storeUri")
        self.put(addr, data, content_crc32=content_crc32)
        out = self.commit(addr)
        uri = _find_uri(out) or store_uri
        if not uri:
            raise ImageXError(f"commit 成功但拿不到 Uri：{json.dumps(out)[:300]}")
        self._uri_cache[digest] = (uri, t["space_name"], time.time())
        if len(self._uri_cache) > URI_CACHE_MAX:
            for k in sorted(self._uri_cache, key=lambda k: self._uri_cache[k][2])[
                    :len(self._uri_cache) - URI_CACHE_MAX]:
                self._uri_cache.pop(k, None)
        return uri

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _unwrap(r: httpx.Response, what: str) -> dict:
        try:
            d = r.json()
        except Exception as e:
            raise ImageXError(
                f"{what} 返回非 JSON（HTTP {r.status_code}）：{r.text[:200]!r}",
                retryable=r.status_code >= 500) from e
        if r.status_code >= 400 or d.get("ResponseMetadata", {}).get("Error"):
            err = (d.get("ResponseMetadata") or {}).get("Error") or {}
            raise ImageXError(
                f"{what} 失败：HTTP {r.status_code} "
                f"code={err.get('Code')} msg={err.get('Message')}",
                retryable=False)
        return d.get("Result", d)


def _find_uri(obj) -> str | None:
    """从 commit 结果里挖出 `tos-cn-i-...` 形态的 uri。"""
    if isinstance(obj, dict):
        for k in ("Uri", "uri", "StoreUri", "storeUri"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("tos-cn-i-"):
                return v
        for v in obj.values():
            got = _find_uri(v)
            if got:
                return got
    if isinstance(obj, list):
        for v in obj:
            got = _find_uri(v)
            if got:
                return got
    return None


__all__ = [
    "ImageXUploader", "ImageXError", "aws4_sign",
    "URI_CACHE_TTL", "STS_REUSE_SECONDS",
]
