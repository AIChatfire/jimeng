# 对外契约（冻结）

> 本文件是**唯一的对外契约真相**。改动 = 破坏调用方，必须同步 `tests/test_api.py`。
> 上游侧的字段与错误码在 `docs/UPSTREAM.md`；推导过程在 `.workbuddy/memory/`。

---

## 0. 三条端点

| 方法 | 路径 | 状态码 | 用途 |
|---|---|---|---|
| `POST` | `/async/v1/images/generations` | `202` | 受理，**只回一个 `task_id`** |
| `GET` | `/async/v1/images/generations/{task_id}` | `202`/`200` | 非终态回排队态；终态回结果 |
| `GET` | `/async/v1/models` | `200` | 能力清单（OpenAI 形态） |
| `DELETE` | `/async/v1/images/generations/{task_id}` | `200`/`400` | 删除**已终态**的任务 |
| `GET` | `/async/v1/images/generations` | `200` | 本 Key 的任务列表 |

运维端点（**不属于对外契约**）：`GET /healthz`（零依赖，容器探活用）、
`GET /readyz`（会 ping 一次库与凭据配置）、`GET /stats`。

鉴权：`Authorization: Bearer <key>`。`API_KEYS` 为空时**关闭鉴权**（仅限内网，
启动会打 WARNING）。任务与 Key 指纹绑定。

### 0.2 火山方舟（Ark）契约门面（2026-09-20 起）

对外形态逐字段对齐方舟《创建/查询视频生成任务》（实现收拢在 `app/ark.py`），
底层翻译到即梦 Seedance 链路 —— 方舟 SDK 客户端无需改代码即可切换：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v3/contents/generations/tasks` | 方舟创建形态：`{model, content[], ratio, duration, resolution, seed, …}` → **只回 `{"id": …}`** |
| `GET` | `/api/v3/contents/generations/tasks/{id}` | 方舟查询形态：`{id, model, status, error, content{video_url}, created_at, …}`；`status ∈ queued/running/succeeded/failed` |

翻译与降级规则（全部在 `degradations` 里可见，查询响应原样带回）：

* `model`：任何 `doubao-seedance-*` → 映射到 `jimeng-t2v`（降级留痕）；
* `content[]`：只接受 `text`；`image_url`/`video_url`/`audio_url`（r2v/i2v）
  **不支持，400** —— 即梦 t2v 无参考能力（未抓包）；
* `ratio "adaptive"` → 默认 16:9（降级留痕）；`duration`/`resolution` 过
  即梦计费档位白名单（当前 720p×4s）；
* `watermark` / `generate_audio` / `callback_url` / `return_last_frame` 等
  方舟常规参数 → **降级留痕不挡人**；
* 🔴 `usage`（completion_tokens/total_tokens）**不给** —— 即梦链路没有
  token 口径，伪造数字等于说谎；预估积分以扩展字段 `usage.forecast_credits`
  给出（仅成功任务）。

### 0.3 视频补帧（jimeng-vfi，仅 /async/v1/videos 提供）

```json
{ "source_task_id": "jimeng_…", "target_fps": 60 }   // model/prompt 可省略
```

* `source_task_id` 必填：指向本服务一个**已成功的 t2v 任务**（同 API Key）
  —— 补帧要引用源视频的 `vid`/`item_id`/`origin_history_id`，
  只有走本服务产物链才拿得到；
* `prompt` 省略 ⇒ 沿用源任务提示词（降级留痕）；`resolution`/`duration`
  必须**与源任务一致**（实抓形态是沿用，改档位没有抓包依据）；
* 提交包计费字段 `amount=0`（UI 口径免费，未对账）；
* Ark 方舟契约没有补帧概念，该能力不进门面。

| 方法 | 路径 | 状态码 | 用途 |
|---|---|---|---|
| `POST` | `/async/v1/videos/generations` | `202` | 受理，**只回一个 `task_id`** |
| `GET` | `/async/v1/videos/generations/{task_id}` | `202`/`200` | 非终态回排队态；终态回结果 |
| `DELETE` | `/async/v1/videos/generations/{task_id}` | `200`/`400` | 删除**已终态**的任务 |

受理体：

```json
{
  "model": "jimeng-t2v",          // 可省略（视频族当前只有它）
  "prompt": "一只猫在跳舞",        // 必填
  "resolution": "720p",           // 可省略，默认 720p（已实抓档位）
  "duration": 4,                  // 可省略，单位秒，默认 4（已实抓档位）
  "aspect_ratio": "16:9",         // 可省略，默认 16:9（唯一有实抓样本的比例）
  "seed": 123                     // 可选整数
}
```

成功响应：`{"data": [{"url": "…mp4"}], "created": …, "usage": {"videos": 1}}`
—— 量词是 **`usage.videos`**，不是 `images`。

视频端点的**硬边界**（与"不猜"纪律一致）：

* `(resolution, duration)` 必须命中**实抓档位白名单**
  （当前仅 `720p × 4s`），否则受理时 400 —— `benefit_type`/`amount`
  是计费字段，没有抓包依据的档位拒绝构造；
* 视频草稿**没有张数字段**（实抓确认无 `gen_option`）⇒ `n>1` 按 1 处理
  并在 `degradations` 留痕；
* `size` / `image` / `negative_prompt` 不属于视频端点，传了 400；
* ⚠️ `jimeng-t2v` 提交侧已按实抓适配，但**未端到端实跑**（建任务即计费）；
  产物解析为尽力而为（回包结构未实抓），解析不到按失败处理。

---

## 1. 受理

```http
POST /async/v1/images/generations
Authorization: Bearer <key>
Content-Type: application/json

{
  "model": "jimeng-t2i",
  "prompt": "飞上天",
  "image": []
}
```

**`image` 是数组，张数按能力分**（2026-09-20 起）：

| 能力 | 接受几张 | 说明 |
|---|---|---|
| `jimeng-t2i`（文生图） | 0 | 必须传 `[]`；传了会 400 |
| `jimeng-hd` / `jimeng-pro-hd` / `jimeng-outpaint` | **1** | 上游用单个 `origin_image` 承载；**多传会明确 400** |
| `jimeng-i2i`（图生图） | **1–4**（多张垫图） | 上游草稿的 `image_uri_list` / `image_list` 本来就是列表；**顺序即请求顺序** |

- 全局另有一道 4 张的合理性上限（挡住"一次塞几百个 URL"）。
- 🔴 **三种行为只有两种**：要么**支持**（全部上传、全部进草稿），要么**明确拒绝**。
  **绝不存在第三种（收下 N 张却只用第 1 张）** —— 那会让调用方以为用了 N 张。
- 张数超限一律 `400 invalid_parameter`（`param="image"`），信息里会写明上限与应有做法。

**`n`（出图张数）的默认值**：

- 🔴 **不传 `n` ⇒ 取该模型的最小合法值**（通常就是 **1**），
  **不采用上游自己的 `default_generate_count`**。
  实测上游各家默认不同（5.0 Pro 默认 **2**、5.0 Lite 默认 **4**）——
  照它的默认走，调用方会按"1 张"的预期收到 2~4 张的账单。
  **默认必须是最省的那个。**
- 不传 `n` 时**不会**留下"已吸附"的降级说明（没要求过，就没什么可解释的）。
- **显式传** `n` 时按模型的合法取值**吸附**（取不超过请求值的最大合法值；
  低于下界则抬到下界），并**在 `degradations` 里留痕** ——
  吸附会直接改变花费，不允许静默。
- 示例：`n=3` 而该模型选项是 `(1,2,4)` ⇒ 生效 `2`，且
  `degradations` 里出现"请求 n=3 已吸附为 2"。


**响应 `202`**：

```json
{ "task_id": "jimeng_b8d9f0b8247f4eeda60f84c908e192cb" }
```

`Location: /async/v1/images/generations/{task_id}`

### 请求字段

| 字段 | 必需 | 说明 |
|---|---|---|
| `model` | 否* | 见 §3。留空时按 `image` 是否为空推导 —— 但**只在无歧义时**给默认 |
| `prompt` | 看能力 | `t2i` / `i2i` 必需；后编辑族不需要 |
| `image` | 否 | **数组**。文生图传 `[]`；传字符串会被明确拒绝并给出正确写法 |
| `size` | 否 | `"2048x2048"`。默认 `2048x2048`（抓包实测唯一跑通的档位） |
| `n` | 否 | 出图张数。会**吸附**到该模型服务端声明的合法取值（见 §4） |
| `seed` | 否 | 整数 |
| `negative_prompt` | 否 | 仅文生图生效 |

\* `image` 为空时 `model` 可省；带 `image` 时**必须显式指定** —— 四个能力都能接，
而它们的单价差可达 10 倍，替你挑等于替你做决定。

### 关于「认得但做不到」的字段

出现 `watermark` / `response_format` / `quality` / `style` / `stream` / `user` /
`sequential_image_generation` / `max_images` 时**不报错**，而是进 `degradations`
（见 §5）。其它未知字段 → `400`。

这条区分的判据是：**这是"上游没有"还是"你写错了"？** 两者的修复动作完全不同。
把两者都塞进"不支持"，会让人去查上游能力表 —— 而真正的问题在请求体。

---

## 2. 查询

```http
GET /async/v1/images/generations/{task_id}
Authorization: Bearer <key>
```

### 2.1 非终态 → `202`

```json
{ "task_id": "jimeng_...", "status": "queued" }
```

`status ∈ {queued, in_progress}`。**202 的意思是"还没好，继续轮询"** ——
不要把排队态当结果。

### 2.2 成功 → `200`

```json
{
  "data": [ { "url": "https://...jpeg?X-Tos-Expires=86400&..." } ],
  "created": 1789490763,
  "usage": { "images": 1, "credits": 44 }
}
```

三条刻意的取舍：

1. **`data[]` 里只有 `url`。** 宽高/格式我们确实知道，但**不塞进来** ——
   与冻结契约逐字一致，多一个键就多一分"形状不同"的风险。那些真知识在 trace 里。
2. **结果 URL 原样透传，不做转存。** 参考实现的产物也是上游直链（预签名）。
   ⚠️ 即梦产物链接的有效期**未取证** —— 需要长期可用链接时得另做转存，本服务没做。
3. **`created` 是 epoch 秒**（任务完成时刻）。

### 2.3 失败 → `200`

```json
{
  "task_id": "jimeng_...",
  "status": "failure",
  "error": { "message": "...", "type": "...", "code": "..." }
}
```

**失败也回 200**：任务本身完成了（只是结果是失败），请求没出错。
回 4xx 会让调用方的重试逻辑误触发。

⚠️ 任务失败**不代表没花钱**：即梦的建任务是计费动作，`status=30 generate_failed`
照样扣积分 —— 错误文案里会明确写出来。

### 2.4 不存在 / 不属于本 Key → `404`

```json
{ "error": { "message": "任务 ... 不存在，或不属于当前 API Key。",
             "type": "invalid_request_error", "code": "task_not_found" } }
```

刻意**不区分**这两种情况（区分开等于告诉攻击者"这个 id 是存在的"），
且**本地拦死、不发上游请求**。

---

## 3. 能力与 `model` 取值

| `model` | 能力 | 输入图 | prompt | 实测单价 | 实测产物 |
|---|---|---|---|---|---|
| `jimeng-t2i` | 文生图 | 不需要 | **必需** | **0（实测免费）** | 2048×2048 |
| `jimeng-i2i` | 图生图（blend，**多垫图上限 4**） | 必需 | **必需** | **0（实测免费）** | 2048×2048 |
| `jimeng-hd` | 超清 | 必需 | 不需要 | **0（实测免费）** | 4096×4096 |
| `jimeng-pro-hd` | 智能超清 | 必需 | 不需要 | **未测** | 2160×2160 |
| `jimeng-outpaint` | 扩图 | 必需 | 不需要 | **未测** | **4 张** 4000×4000 |

> ⚠️ 单价口径（2026-09-20 校准）：这些值**只放实测值**。原先那批（44/40/9/91/28）
> 全部取自上游回执的 `forecast_generate_cost` —— 那是**预估**，实测**高估 4~9 倍**
> （见 `docs/UPSTREAM.md` §12）。**`0` = 实测不扣分**，不是占位；未实测的写「未测」。

**别名**（同样接受）：裸能力名 `t2i` / `i2i` / `hd` / `pro-hd` / `outpaint`；
中文 `即梦` / `文生图` / `图生图` / `超清` / `智能超清` / `扩图`；
英文 `jimeng` / `text2image` / `image2image` / `upscale`；
**别名大小写不敏感**。

**上游模型 key —— 已登记的 7 个**（可直接当 `model` 传，等价于 `jimeng-t2i` + 该模型）：

| `model` 传这个 key | 上游名字 | 张数选项 | 实测单价 |
|---|---|---|---|
| `high_aes_general_v50`（**默认**） | Seedream 5.0 **Lite** | 1..8 | **0（免费）** |
| `high_aes_general_v50p_large` | **Seedream 5.0 Pro** | **1..4** | **8/张** |
| `high_aes_general_v43` | Seedream 4.7 | 1..8 | 未测 |
| `high_aes_general_v42` | Seedream 4.6 | 1..8 | 未测 |
| `high_aes_general_v40l` | Seedream 4.5 | 1..8 | 未测 |
| `high_aes_general_v41` | Seedream 4.1 | 1..8 | 未测 |
| `high_aes_general_v40` | Seedream 4.0 | 1..8 | 未测 |

⚠️ **传上游 key 时的三条纪律**：

1. **精确匹配、必须全小写** —— 该分支**不做小写化**（与别名不同）：
   `HIGH_AES_GENERAL_V50P_LARGE` 会被判「未知 model」。
2. **只会落成 `jimeng-t2i`（文生图）** ⇒ **上游 key 不能用来选 i2i 的模型**（不能垫图）。
3. **`v50p_large`（5.0 Pro）是收费档**：实测 **8 积分/张**（2026-09-20，
   1:1 / 3:4 / 16:9 三种尺寸同价；账单按 `submit_id` 对上、余额差分吻合）。
   Lite 下 t2i/i2i/hd 实测实扣 0。

上游实际有 **9 个**模型：上表 7 个之外还有 **Seedream 3.0 / 3.1**
（key 带冒号：`high_aes_general_v30l:general_v3.0_18b` 等）—— **本服务未登记**，
传它们会被拒为未知模型。

**占位名**（`auto` / `dall-e-3` / `gpt-image-1` / `seedream-*` …）等价于"没写 `model`"，
走默认推导 —— 第三方 SDK 常硬编码这些值，它们不代表调用意图。

🔴 **别按名字选工具**：`pro-hd`（"智能超清"）只出 2160²，
而 `hd`（"超清"）出 4096² —— 名字里的"更高级"是错觉。

### 刻意缺席的能力

**`jimeng-detail-fix`（细节修复）不注册**：两次真实提交都返回
`status=30 generate_failed`，且**照样计费**。按「不制造假能力」摘除 ——
它不出现在 `/async/v1/models`，请求它会被拒为未知模型。
工具描述仍留在 `client.POST_EDIT_TOOLS` 供将来续查。

---

## 4. `n` 的吸附（会改变花费）

`n` 会被吸附到**服务端**为该模型声明的合法取值上（运行期零成本读取
`get_common_config`，不是代码里的经验值）：

- 实测 `high_aes_general_v50` = **1..8**（默认 4）；`…v50p_large` = 1..4；
- 吸附取**不超过请求值**的最大合法值；低于下界则抬到下界；
- **每次吸附都会写进 `degradations`** —— 张数直接乘积分，静默降级等于让人
  按 A 的预期为 B 付费；
- 后编辑族不支持指定张数（扩图固定出 4 张，由上游决定）：传了就进 `degradations`。

---

## 5. `degradations`（本服务的加性扩展）

任何"请求了 A、实际做了 B"都会出现在这里（**仅非空时出现该键**）：

```json
{ "degradations": ["模型 high_aes_general_v50 的张数选项为 [1..8]，请求 n=12 已吸附为 8",
                   "参数 watermark=True 本服务不支持…，已忽略；不要按它的语义预期结果。"] }
```

来源有三类：参数吸附、输入图归一化、服务端能力表读取失败退回冻结快照。

---

## 6. 错误信封

```json
{ "error": { "message": "...", "type": "...", "code": "...",
             "param": "可选", "retry_after": 可选, "detail": "可选" } }
```

| `code` | HTTP | 含义与**下一步** |
|---|---|---|
| `invalid_parameter` | 400 | 请求写错了。`param` 指出是哪个字段 |
| `content_policy_violation` | 400 | 上游送审/版权拦截 ⇒ 换 prompt 或换图 |
| `invalid_api_key` | 401 | 调用方的 Key 不对 |
| `task_not_found` | 404 | 不存在或不属于本 Key |
| `upstream_rate_limited` | 429 | 上游限流，**可退避重试**（带 `Retry-After`） |
| `upstream_quota_exhausted` | 429 | 积分/日额度耗尽，**重试无效** |
| `risk_control_challenge` | 429 | 命中风控，**重试会延长标记**，服务已进入冷却 |
| `upstream_error` | 502 | 上游 5xx / 非 JSON / WAF 页 |
| `upstream_not_configured` | 503 | 服务未配 `JIMENG_SESSIONID`（**部署问题**，不是你的错） |
| `upstream_timeout` | 504 | 上游超时 |

`Retry-After` **只在是真的才知道**的时候给 —— 编一个数字等于伪造事实。

---

## 7. 删除

| 任务状态 | `DELETE` 行为 |
|---|---|
| 非终态（`queued` / `in_progress`） | **`400`** —— 即梦**没有取消端点** |
| 终态 | `200 {"task_id": "...", "status": "DELETED"}`，删掉本地记录 |

🔴 未终态任务的删除**必须响亮失败**。本地置"已取消"就返回成功有三个后果：
① 上游任务继续跑、继续扣积分，而调用方以为停了；② 本地与上游状态永久不一致；
③ 没有任何出口能看出来。

---

## 8. 测试与运行

```bash
export TEST_DATABASE_URL='postgresql+psycopg2://jimeng:<密码>@127.0.0.1:5432/jimeng_test'
python -m pytest -q
```

⚠️ 缺 `TEST_DATABASE_URL` 时 store / API / 协调器类用例会**失败**（不是跳过）——
静默跳过会让人把"没跑"当成"跑过了"。签名向量 / 能力解析 / 可观测性等
纯离线用例不依赖数据库，随时可跑。
