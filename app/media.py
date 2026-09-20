#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体处理：类型嗅探 / 输入解析（外链、data URI、裸 base64）/ 上传前归一化。

三条硬规矩（都来自上游实测踩坑）：

  1. **按 magic bytes 嗅探，不信扩展名也不信 Content-Type。**
     真实案例：`.jpg` 结尾、Content-Type 写成非标准的 `image/jpg`，实际是 PNG，
     上游按 Content-Type 校验直接拒。
  2. **外链先转成本地字节再交给上游。**
     跨站抓取外链有体积/超时限制（实测 6MB 抓取失败，压到 0.7MB 后同一张图成功）。
  3. **透明通道不能被压成 JPEG。** RGBA/LA/P 一律保留为 PNG，否则 alpha 被抹黑。
"""
from __future__ import annotations

import base64
import binascii
import io
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .errors import InvalidParameterError, UpstreamUnavailableError

#: 多张垫图时**同时下载**几张。与 `Capability.max_images`（blend 上限 4）同量级。
#: 下载是纯网络等待，并发收益接近线性；再高没有收益 —— 上限本来只有 4。
MAX_DOWNLOAD_PARALLELISM = 4

#: 上游产物 URL 的形态（实测）：
#:   https://p26-dreamina-sign.byteimg.com/tos-cn-i-tb4s082cfz/<32位hex>~tplv-...png?x-signature=...
#: 而草稿要的 `image_uri` 就是 `<space>/<key>` —— 上传流程产出的也是**同一形态**：
#:   `tos-cn-i-tb4s082cfz/e103c9b4f36543cbbdce65a42f8acaf3`（见 submit 回执的 resources.key）
#: ⇒ 所以"把上一次的产物当下一次输入"时，**整段下载+归一化+上传都能跳过**。
#:
#: 两条守卫都必要：
#:   · **必须限定 host**：只看路径的话，任意站点上一个恰好长这样的路径都会被认成
#:     我们的资产 —— 于是拿着一个不存在的 key 去建任务；
#:   · **key 必须是 hex**：挡住 `/tos-cn-i-x/../../etc` 这类路径穿越式的怪值。
_TOS_HOST_SUFFIX = ".byteimg.com"
_TOS_ASSET_RE = re.compile(
    r"^/?(?P<uri>tos-cn-i-[a-z0-9]+/(?P<key>[0-9a-fA-F]{16,}))(?:~|\?|$)")


def reuse_image_uri(ref: str) -> str | None:
    """若输入**已经是上游存储里的资产**，返回它的 `image_uri`；否则 None。

    ## 为什么值钱

    「拿上一次的产物当这次输入」是最常见的用法。而在此之前，我们每次都把它
    **从上游下载回来、归一化、再上传回上游** —— 实测这三段合起来是
    ~0.5s 下载 + ~1.3s 上传（3 张、2.9MB 级），而且完全是在搬同一份字节。
    直接复用 uri 就是 **0 成本**。

    ## 守卫与代价

    - 只认 `<hex>.byteimg.com` 下的 `tos-cn-i-<space>/<16+ 位 hex>` 路径，其余一律
      走原路（下载+上传）。**不猜、不宽松匹配** —— 认错了会拿一个不存在的 key 去建任务。
    - 复用时**拿不到原图字节**，所以"magic bytes / 体积上限"这两道校验做不了；
      对象不存在的话会在**建任务**那一步暴露（上游拒绝或任务失败）。
    """
    text = (ref or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        return None
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    if not host.endswith(_TOS_HOST_SUFFIX):
        return None
    m = _TOS_ASSET_RE.match(parsed.path)
    return m.group("uri") if m else None

# ---------------------------------------------------------------------------
# 类型嗅探
# ---------------------------------------------------------------------------

_IMAGE_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"BM", "image/bmp", ".bmp"),
    (b"II*\x00", "image/tiff", ".tif"),
    (b"MM\x00*", "image/tiff", ".tif"),
    (b"\x00\x00\x01\x00", "image/x-icon", ".ico"),
    (b"8BPS", "image/vnd.adobe.photoshop", ".psd"),
)

_FTYP_BRANDS = {
    b"heic": ("image/heic", ".heic"),
    b"heix": ("image/heic", ".heic"),
    b"hevc": ("image/heic", ".heic"),
    b"hevx": ("image/heic", ".heic"),
    b"mif1": ("image/heif", ".heif"),
    b"msf1": ("image/heif", ".heif"),
    b"avif": ("image/avif", ".avif"),
}

DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]+)?(?P<b64>;base64)?,(?P<payload>.*)$", re.S)
_B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


def sniff(data: bytes) -> tuple[str, str]:
    """返回 (mime, ext)；无法识别时给 ('application/octet-stream', '.bin')。"""
    if not data:
        return "application/octet-stream", ".bin"
    for magic, mime, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime, ext
    # WebP: RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    # HEIC / HEIF / AVIF: ....ftyp<brand>
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(data[8:12], ("image/heif", ".heif"))
    if data[:2] == b"\xff\xd8":
        return "image/jpeg", ".jpg"
    return "application/octet-stream", ".bin"


def looks_like_url(raw: str) -> bool:
    return raw.strip().lower().startswith(("http://", "https://"))


def decode_b64(payload: str) -> bytes:
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as e:
        raise InvalidParameterError(
            f"image 字段的 base64 无法解码: {e}", param="image") from e


# ---------------------------------------------------------------------------
# 输入载体
# ---------------------------------------------------------------------------


@dataclass
class Blob:
    data: bytes
    mime: str
    ext: str
    origin: str                       # "url" / "data-uri" / "base64" / "jimeng-uri"
    src: str = ""                     # 原始引用（URL 或截断的 base64 前缀）
    original_bytes: int = 0
    normalized: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.data)

    def describe(self) -> dict:
        """对外/对 trace 的描述体 —— 不含字节，只含事实。"""
        return {
            "origin": self.origin,
            "src": self.src[:180],
            "mime": self.mime,
            "bytes": self.size,
            "original_bytes": self.original_bytes or self.size,
            "normalized": self.normalized,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 下载与解析
# ---------------------------------------------------------------------------


def download(url: str, settings: Settings) -> bytes:
    limit = settings.max_download_bytes
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0),
                          follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise UpstreamUnavailableError(
                        f"拉取输入图片失败 HTTP {resp.status_code}",
                        param="image", url=url)
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise InvalidParameterError(
                            f"输入图片超过上限 {limit} 字节", param="image", url=url)
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError as e:
        raise UpstreamUnavailableError(
            f"拉取输入图片失败: {e}", param="image", url=url) from e


def load_one(raw: str, settings: Settings) -> Blob:
    """把单个 image 载体（URL / data URI / 裸 base64）变成 Blob。"""
    text = (raw or "").strip()
    if not text:
        raise InvalidParameterError("image 载体为空", param="image")

    m = DATA_URI_RE.match(text)
    if m:
        payload = m.group("payload")
        data = decode_b64(payload) if m.group("b64") else payload.encode("utf-8")
        mime, ext = sniff(data)
        if m.group("mime") and mime == "application/octet-stream":
            mime = m.group("mime")
        return Blob(data=data, mime=mime, ext=ext, origin="data-uri",
                    src="data:" + text[5:45] + "…", original_bytes=len(data))

    if looks_like_url(text):
        data = download(text, settings)
        mime, ext = sniff(data)
        return Blob(data=data, mime=mime, ext=ext, origin="url", src=text,
                    original_bytes=len(data))

    if _B64_RE.match(text) and len(text) > 64:
        data = decode_b64(re.sub(r"\s+", "", text))
        mime, ext = sniff(data)
        return Blob(data=data, mime=mime, ext=ext, origin="base64",
                    src="<base64:%d chars>" % len(text), original_bytes=len(data))

    raise InvalidParameterError(
        "image 需为 http(s) URL、data URI 或 base64 字符串",
        param="image", got=text[:80])


def _load_checked(ref: str, settings: Settings) -> Blob:
    """`load_one` + 两道校验（可放进线程池，所以单独抽出来）。"""
    blob = load_one(ref, settings)
    if blob.mime == "application/octet-stream":
        raise InvalidParameterError(
            "输入不是可识别的图片格式（按 magic bytes 判定）",
            param="image", src=blob.src[:120])
    if blob.size > settings.max_input_bytes:
        raise InvalidParameterError(
            f"输入图片超过上限 {settings.max_input_bytes} 字节", param="image")
    return blob


def load_images(refs: list[str], settings: Settings) -> list[Blob]:
    """把多个输入图载体变成 Blob 列表。**顺序与 `refs` 一致。**

    多张时**并发下载**：`download()` 每次新建自己的 `httpx.Client`（`with` 块内），
    没有共享状态，所以并发是安全的；而传多个 URL 时这是**纯网络等待**，
    串行等于把各自的往返时间加起来。

    ⚠️ 顺序为何重要：垫图的先后对生成语义有影响（见 `build_blend_draft`）。
    `Executor.map` **保序** —— 结果按 `refs` 顺序产出，
    连报错也是"按输入顺序遇到的第一个"（确定性，不随线程调度漂移）。
    """
    if not refs:
        return []
    if len(refs) == 1:
        return [_load_checked(refs[0], settings)]
    n = min(len(refs), MAX_DOWNLOAD_PARALLELISM)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda r: _load_checked(r, settings), refs))


# ---------------------------------------------------------------------------
# 归一化（上传上游之前）
# ---------------------------------------------------------------------------

_ALPHA_MODES = ("RGBA", "LA", "PA", "P")


def normalize(blob: Blob, settings: Settings) -> Blob:
    """按最长边与体积压缩；**失败一律原样返回并留 note，不阻断主流程**。

    ⚠️ 归一化的前提是"上游对输入图的尺寸/体积有限制"。即梦侧的限制数值**未取证**
    （`get_common_config` 下发的是 `input_image_limit`，量纲待坐实），
    所以这里只做**保守压缩**，不做"压到某个神秘阈值"的猜测。
    """
    if not settings.normalize_uploads:
        return blob
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception:  # pragma: no cover - Pillow 缺失时优雅降级  # noqa: BLE001
        blob.notes.append("未安装 Pillow，跳过归一化")
        return blob

    try:
        with Image.open(io.BytesIO(blob.data)) as im:
            im.load()
            has_alpha = im.mode in _ALPHA_MODES
            img = im.copy()
    except Exception as e:  # noqa: BLE001
        blob.notes.append(f"归一化跳过：图像解码失败（{type(e).__name__}）")
        return blob

    changed = False
    max_side = settings.normalize_max_side
    if max(img.size) > max_side:
        scale = max_side / float(max(img.size))
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS)
        changed = True

    target = settings.normalize_max_bytes
    buf = _encode(img, has_alpha, quality=92)
    if len(buf) > target:
        for quality in (85, 78, 70, 62, 55, 48, 40):
            buf = _encode(img, has_alpha, quality=quality)
            changed = True
            if len(buf) <= target:
                break

    if not changed and len(buf) >= len(blob.data):
        return blob

    mime, ext = sniff(buf)
    if mime == "application/octet-stream":
        return blob

    return Blob(
        data=buf,
        mime=mime,
        ext=ext,
        origin=blob.origin,
        src=blob.src,
        original_bytes=blob.original_bytes or len(blob.data),
        normalized=True,
        notes=blob.notes + [f"已归一化 {len(blob.data)}B -> {len(buf)}B"],
    )


def _encode(img, has_alpha: bool, quality: int) -> bytes:
    buf = io.BytesIO()
    if has_alpha:
        # 透明通道不能被压成 JPEG，否则 alpha 被抹黑
        img.convert("RGBA").save(buf, format="PNG", optimize=True)
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


__all__ = [
    "Blob", "sniff", "looks_like_url", "decode_b64", "download",
    "load_one", "load_images", "normalize", "DATA_URI_RE",
]
