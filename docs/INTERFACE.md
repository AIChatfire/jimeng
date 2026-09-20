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
| `jimeng-t2i` | 文生图 | 不需要 | **必需** | 44 | 2048×2048 |
| `jimeng-i2i` | 图生图（blend） | 必需 | **必需** | 40 | 2048×2048 |
| `jimeng-hd` | 超清 | 必需 | 不需要 | **9** | 4096×4096 |
| `jimeng-pro-hd` | 智能超清 | 必需 | 不需要 | 91 | 2160×2160 |
| `jimeng-outpaint` | 扩图 | 必需 | 不需要 | 28 | **4 张** 4000×4000 |

**别名**（同样接受）：裸能力名 `t2i` / `i2i` / `hd` / `pro-hd` / `outpaint`；
中文 `即梦` / `文生图` / `图生图` / `超清` / `智能超清` / `扩图`；
上游模型 key（`high_aes_general_v50` 等，等价于 `jimeng-t2i` + 该模型）。

**占位名**（`auto` / `dall-e-3` / `gpt-image-1` / `seedream-*` …）等价于"没写 `model`"，
走默认推导 —— 第三方 SDK 常硬编码这些值，它们不代表调用意图。

🔴 **别按名字选工具**：`pro-hd`（"智能超清"）91 积分只出 2160²，
而 `hd`（"超清"）9 积分出 4096²。

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
