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

### 枚举值逐项复核（2026-09-20，与网页端面板对齐）

**比例** —— `resolution_map[bucket].image_ratio_sizes` 是**数组**，
`ratio_type` 取 `1..8`，**顺序与面板逐项一致**：

| ratio_type | 比例 | 2K 像素 | 4K 像素 |
|---|---|---|---|
| 1 | 1:1 | 2048×2048 | 4096×4096 |
| 2 | 3:4 | 1728×2304 | 3520×4693 |
| 3 | 16:9 | 2560×1440 | 5404×3040 |
| 4 | 4:3 | 2304×1728 | 4693×3520 |
| 5 | 9:16 | 1440×2560 | 3040×5404 |
| 6 | 2:3 | 1664×2496 | 3328×4992 |
| 7 | 3:2 | 2496×1664 | 4992×3328 |
| 8 | 21:9 | 3024×1296 | 6197×2656 |

- 面板上排在最前的**"智能"不在这里** —— 它对应草稿里的
  `intelligent_ratio: true`（文生图草稿默认写 `false`）。
- ⚠️ **4K 的像素不是整数倍**（3:4 → 3520×4693），看来是按面积换算的 ——
  **别自己按比例乘**，要用就直接读这张表。

**分辨率** —— 桶名 `1.5k` / `2k` / `4k`，`resolution_name` 分别是
标清 1.5K / 高清 2K / 超清 4K。**不是每个模型都有三档**：
**5.0 Pro 有 `1.5k`**，5.0 Lite 及往下只有 `2k` / `4k`。
`default_resolution_type` 实测 `2k`。

**采样步数** —— `sample_steps`：5.0 系 `steps=16`（`min 10` / `max 41`）。

**生成数量** —— `generate_count_options` + `default_generate_count`，
⚠️ **默认值各家不同**：5.0 Pro 默认 **2**、5.0 Lite 默认 **4** ——
所以"不传张数"在两者上拿到的结果不一样。

**输入图上限** —— `input_image_limit` 原生是**数组、按 ability 分**：
`[{'max_image_num': 10, 'ability_name': 'byte_edit'}]`
（`byte_edit` 就是 blend / 图生图那个 ability；5.0 Lite 未声明该字段）。
🔴 我们这边 `capabilities.py` 用 `_as_int()` 读它 ⇒ **对数组恒得 `None`**
（且目前没有消费方，所以是"声明了却常为 None 的死旋钮"）。
⚠️ 也就是说 `Capability.max_images`（i2i 现在写死 4）**比上游声明的 10 更保守** ——
要放开得先按数组解析这个字段。

⇒ 运行期读取并缓存（`app/upstream/jimeng/capabilities.py`），
**代码里那份快照只作探测失败时的兜底，且会如实标注降级**。
教训：曾按"用户经验 1–4"写死上界 4，对默认模型直接是错的。

## 11.5 积分余额 / 消耗记录（**只读、不计费**，2026-09-20 实测）

```
POST /commerce/v1/benefits/user_credit_history
body: {"count": 20, "cursor": "0", "history_type": 2}     # 2 = 消耗
```

**鉴权：两种形式等价**（实测同一次调用，三种变体都返回 `200` / `ret=0` / 同一余额）：

| 变体 | 结果 |
|---|---|
| `Cookie: sessionid=<…>`（本仓客户端现有做法） | ✅ `ret=0` |
| `Authorization: Bearer <sessionid>` | ✅ `ret=0` |
| 两者都带 | ✅ `ret=0` |

⇒ 走 Cookie 即可，**不必为它改鉴权代码**。（签名头那套
`sign` / `x-secsdk-web-signature` / `device-time` / `uifid` 由
`app/upstream/jimeng/sign.py` 现成算好，`_post` 自动带。）

**响应**：`data.total_credit`（**账户可用积分**）/ `data.records[]` /
`data.new_cursor` / `data.has_more`。
`records[]`：`amount` / `create_time`（**秒**）/ `title` / `submit_id` / `status`。
`title` 例："图片生成"、"智能超清2.0-2k"。
**`submit_id` 与我们任务的 `upstream_submit_id` 对得上 ⇒ 可按任务对账。**

### 🔴 实测：回执里的 `forecast_generate_cost` **严重高估**

| 任务 | 回执 `forecast_*`（我们报成 `usage.credits`） | **实际扣（本接口）** |
|---|---|---|
| i2i 双垫图 | **55** | **12** |
| hd（"智能超清2.0-2k"） | **9** | **1** |

⇒ **别把 `forecast_generate_cost` 当实际扣费报给调用方**（高估 4~9 倍）。
`Capability.credits_measured` 目前全部取自 forecast，**都偏高**，应改用本接口校准。

⚠️ 两个陷阱：
- **消耗记录会延迟结算**：跑完当场查可能还没有那条记录 ——
  **不能因为"没看到记录"就断定免费**；要等结算，或用 `total_credit` **跑前/跑后差分**。
- 积分**会过期清零**（记录里出现过 "积分到期清零"）。

## 12. 模型单价（实测，**别按新旧推**）

`v43` = **35** < `v50` = 44 < `v40` = **51**；
最老的 `v30l:general_v3.0_18b` 直接 `ret=1006`（权益不足，不可用）。
⇒ "选旧模型省积分"**不成立**，要省必须逐个实测。
