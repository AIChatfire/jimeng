# 即梦上游契约（精简）

> 只记录**服务依赖的事实**及其**出处**。逐字段的取证过程在上游研究仓的
> `docs/upstream/jimeng-image-api.md`；这里是够用的那一份。
> 标 ❓ 的是**未取证**项 —— 不要把它们当结论用。

站点：`https://jimeng.jianying.com`（mweb）。产物名 `即梦 / Dreamina`，
服务端自报模型 `Seedream 5.0 Lite`。

---

## 1. 鉴权

🔴 **最小凭据 = cookie 里的一个键 `sessionid`。**

负对照（读写端点一致的判据）：

| 格 | cookie | `sign` | 读接口 | 写接口 |
|---|---|---|---|---|
| W1 / A | ✅ | ✅ | `ret=0` | `ret=1002`（业务错误，鉴权已过） |
| W3 | ✅ | ❌ | `ret=0` | `ret=1002`（**与 W1 同码**） |
| C / E / W2 | ❌ | — | `ret=1015 login error` | `ret=1015 login error` |

⇒ W1 == W3 ⇒ **`sign` 不参与鉴权**。其余 20 多个 cookie、`a_bogus`、`msToken`
全部不需要。**全链路纯 HTTP，零浏览器。**

⚠️ 码表里有 `1014 ErrSign` ⇒ 签名校验**实现上是有的**，可能按风控力度动态开启。
故本实现**照带但不依赖**（纯 MD5、零成本）。

## 2. 签名

```
sign = md5("9e2c|" + pathname[-7:] + "|" + pf + "|" + appvr + "|" + unix秒 + "|" + tdid + "|11ac")
```

`pf=7`、`appvr=8.4.0`、`sign-ver=1`。与 cookie / 请求体 / query **都无关**
⇒ 完全离线可复现。`device-time` 必须等于参与签名的那一秒。

11 条真实抓包向量在 `app/upstream/jimeng/sign.py::SELF_TEST_VECTORS`，
`verify_vectors()` 直接断言。

## 3. 端点

| 用途 | 路径 | 计费 |
|---|---|---|
| 建任务 | `POST /mweb/v1/aigc_draft/generate` | 🔴 **扣积分** |
| 取任务 | `POST /mweb/v1/get_history_by_ids`（body `{"submit_ids":[…]}`） | 免费 |
| 历史列表 | `POST /mweb/v1/get_history`（body **必须** `{"count":N}`，空 body → `2008`） | 免费 |
| 换签名 URL | `POST /mweb/v1/get_image_by_uri`（body **`{"uris":[…]}` 复数**，单数 → `1000`） | 免费 |
| 模型能力表 | `POST /mweb/v1/get_common_config`（body `{}`） | 免费 |
| 上传 STS | `POST /mweb/v1/get_upload_token`（body `{"scene":2}`） | 免费 |

列表 / 取消：**无取消端点**（见 §7）。

## 4. 建任务的两个硬形态

1. 🔴 **`draft_content` 是「JSON 字符串」而不是 JSON 对象**（双重编码）。
   写成对象 → `1002 common error`。
2. **`submit_id` 由客户端自生成**（uuid4）⇒ 不依赖回执格式，回执解析失败也能轮询。

`metrics_extra` 也是 JSON 字符串，且**三种能力形态不同**（文生图 / blend / 后编辑），
照抄抓包，不拼凑。

## 5. 任务状态

原文出处：`7960.13db92ac44.js` / `5794.fa66eab7e8.js`（两处一致）。

| 值 | 名 | 终态 | 本服务映射 |
|---|---|---|---|
| 0 | init | 否 | `queued` |
| 10 | pre_check_reject | 是 | `failure` |
| 20 | submitted | 否 | `in_progress` |
| 30 | generate_failed | 是 | `failure` |
| 40 | post_check_reject | 是 | `failure` |
| 42 | thinking | 否 | `in_progress` |
| 45 | partial_success | 是 | `success`（带产物提示） |
| 50 | success | 是 | `success` |
| 100 | deleted | 是 | `canceled` |

🔴 **成败只看 `task.status`**：`item.common_attr.status=144` **不是**生成状态；
`ret=0` 只说明"请求被受理"。`status=30` **照样计费**。

实测：4 张 2048×2048 约 19s；并发 2/4 实测无 `1010`（至少容忍 4 并发）。

## 6. 错误码（89 条全表已取证，分类决定重试语义）

分类入口 `client.classify_reject` / `client.raise_for_ret`，代码在
`app/upstream/jimeng/client.py`。

| 类 | 码 | 语义 |
|---|---|---|
| 限流（可退避） | `1` `1010` `1057` `2014` `2020` `10020` | 并发/频率上限 |
| 额度（**重试无效**） | `1006` `4001` `121101` | 积分不足 / 日额度用尽 |
| 风控（**重试会加剧**） | `1018` `1019` `1021` `2035` `2038` `2039` `2041` `2042` `2043` | 必须退避 |
| 内容审核 / 版权 | `1063` `1159` `2003` `2004` `2005` `2048` `2050` | 换 prompt / 换图 |
| 参数 / 业务体 | `1001` `1002` `1161` `1162` `3021` `4003` `4010` … | 只有 `1001/1002` 对"草稿形态"有判别力 |
| 凭据 | `1015` | cookie 里没有有效 `sessionid` |

🔴 **只有 `1001/1002` 说明"服务端读不懂这个草稿"。** 把 `1006`（积分/权益）
当成形态问题，曾产出一条假结论。

## 7. 取消：**没有**

上游只有"建任务 + 查询"两个接口 ⇒ 未终态任务的取消**做不到**。
本服务对未终态 `DELETE` **响亮失败**（400），绝不本地置"已取消" —— 那会让
"其实还在跑并继续计费"变成看不见的事。

## 8. 上传（本地图片 → `image_uri`）

火山引擎 ImageX 四段式，**纯 stdlib AWS4 签名，零浏览器零积分**：

```
① POST /mweb/v1/get_upload_token {"scene":2}    → STS + space_name + upload_domain
② GET  https://<upload_domain>/?Action=ApplyImageUpload&Version=2018-08-01
        &ServiceId=<space>&FileSize=<n>          → UploadAddress{StoreInfos, UploadHost, SessionKey}
③ POST https://<UploadHost>/upload/v1/<StoreUri>  (裸字节 + Authorization: <Auth>)
④ POST ?Action=CommitImageUpload  body {"SessionKey":"<b64(UploadAddress)>"}
```

产出 `tos-cn-i-<space>/<hash>` = 草稿要的 `image_uri`。

🔴 **两个坑**：两条请求的 `SignedHeaders` **不一样**
（GET `x-amz-date;x-amz-security-token` **不含 host**；
POST `content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token`）；
响应字段是 `UploadHost`（**单数**）而非 `UploadHosts`。

🔴 **`image_uri` 不是内容寻址**：同一份字节传两次得到两个**不同**的 uri
⇒ 不缓存既重复 4 次 HTTP、又在账号里堆重复素材。故本服务加了三层：
STS 双检锁 + 内容哈希→uri 缓存 + in-flight 去重（并发同内容 8 个 uri → 1 个）。

## 9. 后编辑工具族 = 同一端点追加组件

不是四个端点：往 `component_list` 追加一个带 `postedit_param` 的
`image_base_component`，`generate_type` 字符串选工具、参数放 `abilities.<同名 key>`。

| 工具 | `generate_type` | `postedit_param.generate_type` | 实测 |
|---|---|---|---|
| 超清 | `normal_hd` | 13 | ✅ **9 积分**，2048² → **4096²** |
| 智能超清 | `pro_hd` | 35 | ✅ 91 积分 → 2160²（又贵又小） |
| 扩图 | `painting` | 8 | ✅ 28 积分 → **4 张** 4000²（张数由上游定） |
| 细节修复 | `super_resolution` | 2 | ❌ 两次 `generate_failed` ⇒ **不注册** |

## 10. 图生图（blend）

不是另一个端点，而是同一个 `component_list` 里的另一种组件（`gen_type=12`、
`generate_type="blend"`）。结构逐字段照抄账号历史里的 30 条真实样本，关键点：

- 组件**没有 `metadata`**（与后编辑组件不同）；
- `abilities.blend.ability_list[0].name = "byte_edit"`；
- **需要 prompt**（描述要怎么改）——这是它与后编辑族的区别；
- 实测 ✅ 2048² / **40 积分**。

## 11. 服务端模型能力表（免费、可读、**别猜**）

`POST /mweb/v1/get_common_config`（body `{}`）按模型下发：
`feats` / `generate_count_options` / `default_generate_count` /
`resolution_map`（比例 → **精确像素**，1k/2k/4k）/ `input_image_limit`。

实测：`high_aes_general_v50`（默认）张数 **1..8**（默认 4）；
`high_aes_general_v50p_large` = **1..4**。

⇒ 运行期读取并缓存（`app/upstream/jimeng/capabilities.py`），
**代码里那份快照只作探测失败时的兜底，且会如实标注降级**。
教训：曾按"用户经验 1–4"写死上界 4，对默认模型直接是错的。

## 12. 模型单价（实测，**别按新旧推**）

`v43` = **35** < `v50` = 44 < `v40` = **51**；
最老的 `v30l:general_v3.0_18b` 直接 `ret=1006`（权益不足，不可用）。
⇒ "选旧模型省积分"**不成立**，要省必须逐个实测。
