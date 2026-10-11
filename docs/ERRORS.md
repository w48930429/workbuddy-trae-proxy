# 常见错误速查（Troubleshooting / Errata）

> 目的：**看到某个报错时，先在这里反查真实原因，避免把 A 误判成 B。**
> 本仓库原先只有代码内注释，没有汇总的错误目录；本文档收录已经踩过并定过性的坑，
> 每条都写清「症状 → 真实原因 → 容易误判成什么 → 怎么验证 → 改到哪」。
>
> 相关日志位置：
> - WorkBuddy 代理（当前一体启动脚本）：`wt-proxy-<端口>.out.log` / `wt-proxy-<端口>.err.log`（默认端口 28087）
> - WorkBuddy 守护记录：`wt-proxy-<端口>.stop`（停机标志）、`wt-proxy-<端口>.pid`（守护 worker 进程号）
> - 遗留独立守护脚本：`proxy-guardian.log`（退出码/重启记录）+ `wb-proxy.err.log`
> - 反重力代理：`antigravity-proxy/ag-proxy.log`（访问日志）、`antigravity-proxy/ag-proxy.err.log`（警告/错误）
> - 用量流水：`usage/usage.jsonl`（一行一请求，含 `upstream` 字段区分出口）

## 索引

| 症状（你看到的） | 真实原因 | 别误判成 |
| --- | --- | --- |
| 反重力请求 503 说「Gemini 5小时额度均已耗尽」，但面板显示还有额度 | 请求上下文超限（全池候选都失败） | 额度真的没了 |
| 响应里 `model` 写着 gemini，输出风格却像 Claude | 静默跨家族改道（已关闭） | gemini 模型行为变了 |
| Trae 报 `4023 model is unknown` | 走错端点 / 模型名不对 | 账号没权限、额度没了 |
| 看板 Trae「本次消耗」出现五位数 | 计费公式漏除以 1000 | Trae 真的这么贵 |
| Trae 当次消耗 / 思考 / 缓存恒为 0 | Trae usage 事件不带 `credit`，且字段是平铺的 | 代理没记账 |
| 配了 trae_first，流量却全走 WorkBuddy | 早期只对非流式生效 + 模型未映射 | 开关没保存 |
| 28087 会自己退出 / 关掉窗口就掉线 | 控制台关闭事件（`CTRL_CLOSE_EVENT`）触发进程自杀 | 程序崩溃 |
| 用 `wt-proxy-stop.bat` 之外的方式停不掉 | 守护脚本会 3 秒内把代理拉起来 | 端口被占用 |
| Trae 手动「刷新积分」被拒 | 10 分钟频控（防上游 429） | 接口坏了 |
| Trae 多账号里第二个账号总说「今日已签到」 | 多账号共用同一设备指纹 | 账号没签到成功 |
| 反重力 claude 模型必报 400 `tools.0.custom.input_schema: JSON schema is invalid` | 工具 schema 里的 `anyOf/oneOf/allOf`，CloudCode 只接受 proto 子集 | Claude 模型坏了 / 账号被封 |
| 反重力 claude 报 503 `Failed to fulfill request after 3 attempts…` | 上游 `MODEL_CAPACITY_EXHAUSTED`（容量，不是额度） | 额度用尽 / 本地调度坏了 |

---

## 1. 反重力 503「当前账号池所有账号的 Gemini 5小时额度均已耗尽」

**症状**：`/v1/chat/completions` 返回 503，body 形如
`Antigravity upstream unavailable: 当前账号池所有账号的 Gemini 5小时额度均已耗尽 (最近回满倒计时: 0小时47分)…`，
而管理面板同一账号的 5h 剩余仍有 80%+。

**真实原因**：这次请求的**上下文超过上游单次输入上限**。执行器会把 400「prompt is too long」
当成「换号也许有用」去重试（不同账号/tier 输入上限不同），但目标家族（gemini）里只剩一个可用账号，
换无可换；旧代码此时会把失败**归因成"家族额度耗尽"**，而 `server.ts` 对非 `UpstreamRequestError`
一律映射成 503 —— 于是出现"面板有余量、请求却 503"的自相矛盾。

**别误判成**：账号额度真的没了 / 账号被风控。

**怎么验证**：
- 看 `antigravity-proxy/ag-proxy.err.log` 是否有
  `账号 xxx 返回上下文超限（…），尝试换下一个账号` 与 `All gemini quotas exhausted, auto-switching to claude model: …` 成对出现。
- 用 `/api/admin/accounts` 核对 `quota.gemini5hFraction`：若明显 >0.01，那就不是额度问题。
- 压测边界：实测 763,639 tokens 的请求可由 gemini 正常受理；再往上（~1.1M tokens）才会被上游拒绝。

**处置（已修）**：`antigravity-proxy/src/pool/breaker.ts`
- 记录 `lastOverflowError`；全池候选都因上下文超限失败时抛 `ContextOverflowError`，
  对外返回 **400 + "Request context exceeds the upstream limit. The client must compact its conversation and retry."**，
  不再伪装成额度 503。
- 客户端此时应该做的是**压缩上下文**，而不是切模型/充值。

## 2. 响应 `model` 是 gemini，实际由 Claude 作答（跨家族静默改道）

**症状**：请求 `gemini-*`，返回 200，`model` 字段也回显 `gemini-…`，但输出风格明显不同；
日志里能看到 `All gemini quotas exhausted, auto-switching to claude model: claude-sonnet-4-6`。
本仓库曾实测：663,163 tokens 的 gemini 请求日志记录"gemini 上下文超限"，却返回了 200 —— 就是被 claude 兜底答的。

**真实原因**：`breaker.ts` 里原有的「跨家族自动切换」：目标家族无可选账号而另一家族有额度时，
改写请求体模型（`gemini → claude-sonnet-4-6`，`claude → gemini-3.8-flash-high`）代答，
响应仍回显客户端请求的模型名，**客户端无从察觉**；代价是静默吃掉另一家族额度，且两家族的分词/上限/
工具调用/推理字段都不同。

**别误判成**：gemini 的行为变了 / 计费算错。

**处置（已关闭）**：`breaker.ts` 顶部新增 `const CROSS_FAMILY_FALLBACK_ENABLED = false;`
（含关闭原因注释，原分支代码保留，改回 `true` 即恢复）。
关闭后的可验证行为：停用唯一的 gemini 可用账号后再请求 gemini，会**立即**返回 503（不再转投 claude），
而同一时刻 claude 请求仍正常 200 —— 说明确实没有偷偷改道。

## 2.4 反重力 claude 模型：工具 schema 400 与上游容量 503

**症状 A（必现）**：客户端（实测 omp）切到 `antigravity-proxy/claude-opus-4-6-thinking` 等 claude 模型，
请求 400：

```
400 Google Upstream returned HTTP 400: … tools.0.custom.input_schema: JSON schema is invalid.
It must match JSON Schema draft 2020-12 …
```

**真实原因**：客户端工具声明的参数 schema 里含有 **`anyOf` / `oneOf` / `allOf`**。
CloudCode（claude 模型经 Google 中转）只接受受限的 proto 式 schema，组合关键字一律非法。
omp 的内置工具就命中这一点：`grep.glob: anyOf[string, array<string>]`（`exclude` 同）。
因为客户端几乎总会带上工具，**只要带这种工具，claude 请求就 100% 失败**。

**别误判成**：Claude 模型不可用 / 账号被封 / 额度没了。

**怎么验证**（最小复现）：
- 送 `{type:object, properties:{glob:{anyOf:[{type:string},{type:array,items:{type:string}}]}}}` → 400「JSON schema is invalid」；
- 同一请求把 `anyOf` 删掉 → 200；
- 其它关键字（`enum` / `additionalProperties` / `number` / `boolean`）都不触发。

**处置（已修）**：schema 清洗收敛为**唯一实现** `antigravity-proxy/src/gateway/schema.ts`
（`anyOf/oneOf` 取首个非 null 分支、`allOf` 浅合并、`type` 数组降级、`const→enum`、
数字型 `exclusiveMinimum/Maximum→minimum/maximum`，最后按 proto 白名单过滤）。
`/v1/chat/completions`（`adapter.ts`）与 `/v1/messages`（`anthropic.ts`）都改用它。
**此前两条入口各有一份实现，anthropic 侧处理了 `anyOf`、chat 侧没处理** —— 这就是同一个工具
在 Claude Code 可用、在 OpenAI 兼容入口（omp）报 400 的原因。不要再各自实现一份。

**症状 B（间歇）**：claude 模型 503：

```
503 Antigravity upstream unavailable: Failed to fulfill request after 3 attempts with rotating accounts.
```

**真实原因**：上游返回 `503 MODEL_CAPACITY_EXHAUSTED`：

```
"No capacity available for model claude-opus-4-6-thinking on the server" / reason: MODEL_CAPACITY_EXHAUSTED
```

即**Google 侧该模型当前没有容量**（与账号额度无关：实测活跃账号 `claude5hFraction` 仍有 0.98~1.0）。
三个账号都被 503 挡下后，旧代码抛出上面那句笼统文案，容易被误判成本地调度坏了。

**别误判成**：额度用尽 / 代理调度故障 / 需要重新登录。

**处置（已修）**：`breaker.ts` 记录 `lastCapacityError`，三个账号都失败时把上游原话回传
（客户端现在看到的是 `No capacity available for model …`）；跨家族静默改道也已关闭，
不会再偷偷换成 gemini 作答。

**检索线索**：日志里搜 `Google Upstream 503` 能看到完整上游报文。

## 3. Trae 报 `4023 We're sorry, the model is unknown`

**真实原因**：打错了端点或模型名。
- `POST /api/ide/v1/chat`（旧端点）**只认 5 个老模型**（`seed_m8`、`Doubao_1_5_thinking_pro`、
  `deepseek-R1`、`deepseek-V3`、`deepseek-V3-0324`），GLM/Kimi/Qwen 系一律 4023；
  真正可用的是 SOLO 网关 `POST /api/agent/v3/llm_utils_chat`。
- 该端点要求 `model_name` 带 `__dev` 后缀、`config_name` 同名、并按模型分发 `function`
  （`solo_agent` / `solo_work_lite`），缺字段报 4001。
- `model_list` 接口返回的那 5 个模型**不是** IDE 的真实目录；真实目录在 IDE 本地缓存
  `state.vscdb` 的 `chat_v3` 视图里（已固化进 `trae/trae_proxy.py` 的 `TRAE_SUPPORTED`）。

**别误判成**：账号没权限 / Trae 没有这个模型 / 端点被封。

**容易踩的坑（2026-10-11 实测）**：
- `4023` = **模型未知**（名字/端点不对），`4001` = 必填字段缺失（如 `prompt_max_tokens`）。
- `4008 Your requests have exceeded the quota.` = **配额不足**。注意：账号配额耗尽时，
  上游对**任意**模型名都回 4008（连故意编造的 `glm-9.9-bogus` 也一样），
  **因此 4008 不能用来判断某个模型是否存在**。要判断模型是否被接受，必须在账号有配额时复测。

**`glm-5.3-flashx` 的现状**（易被误判为"死条目"）：
- 确实存在于 Trae 官方 `batch_get_detail_param` 清单（`rate=0.31`，与 `glm-5.3-flash` 0.06、
  `glm-5.3` 0.78 并列），并被收录进 `trae/trae_proxy.py`：`TRAE_SUPPORTED`、`TRAE_RATES`、
  以及目录规则 `elif "flashx" in mid`（ctx 1,000,000 / out 32,000 / 支持视觉）。
  `/trae/models` 里显示为 `SOLO Work` 通道。
- 它**不在** WorkBuddy 的 `/v1/models`（`wb_catalog.py` 与桌面端缓存都无 `flashx`），
  但用 `model=glm-5.3-flashx` 直接请求代理时，Trae 腿先被配额拦下（429/4008），
  随后回落 WorkBuddy 上游并返回 200（倍率 0，未扣分）。
- **结论**：不要因为 `/v1/models` 或 `model_list` 里没有就判定它不存在；
  Trae 腿的可用性待账号配额恢复后复测（当前两账号对**所有**模型都返回 4008）。

**处置**：`trae/trae_proxy.py` 头部注释与 `TRAE_SUPPORTED`/`SOLO_AGENT_ONLY` 常量；
`trae/trae_route.py` 仅保留旧端点签名。

## 4. 看板 Trae「本次消耗」出现五位数（曾放大 1000 倍）

**真实原因**：Trae 官方倍率（如 `glm-5.3-flash: 0.06`）的单位是**每 1000 tokens 消耗 0.06 积分**；
早期实现写成 `total_tokens * rate`，漏除以 1000。正确公式：
`credit = (total_tokens / 1000) * rate`。

**别误判成**：Trae 单价极高 / 上游重复计费。

**处置**：
- `trae/trae_proxy.py` 的 `_usage_from` / `_usage_estimate` 已改正，并在常量处写明口径。
- 历史数据已批量修正 1000 倍（原文件备份为 `usage/usage.jsonl.bak_fix_credit`）。
- 注意：Trae 的 credit 是**按倍率估算**（Trae 不在对话响应里回传当次扣费）；
  账号真实剩余积分以 `/trae/credits`（官方 `ide_user_ent_usage`）为准。看板已把两种货币拆成
  `credit_wb`（上游真实计费）与 `credit_trae`（估算），不要再相加成一个数。

## 5. Trae 请求的「本次消耗 / 思考 / 缓存」恒为 0

**真实原因**：Trae 的 `token_usage` 事件**不含 `credit` 字段**，且 `reasoning_tokens`、
`cache_read_input_tokens` 是**平铺在顶层**；而 WorkBuddy 上游是 OpenAI 风格嵌套
（`completion_tokens_details.reasoning_tokens`、`prompt_cache_hit_tokens`）。
只按嵌套格式取就会恒为 0。

**处置**：`wb_proxy.py` 的 `_extract_usage` 已兼容两种形态（docstring 里写明）。

## 6. 配了 `trae_first` 却没走 Trae

**历史原因（两个叠加）**：
1. 路由分支曾写成 `_trae_first_enabled() and not want_stream`，**流式请求直接跳过 Trae**，
   而客户端绝大多数是 `stream: true`；
2. 模型名未映射时（Trae 不认）会安静回落 WB，日志只有一行 `trae_first: model not mapped`。

**现状**：流式/非流式都会先走 Trae；模型走查 `config.trae_model_map` 与内置支持表，
未映射的直走 WB；Trae 端 4023 等失败也会回落 WB。变更历史在 `wb_proxy.py` 的
`_dispatch_chat_post` 注释与 `trae/trae_proxy.py` 头部。

**别误判成**：Trae 没扣分是因为账号没配置。

## 7. 28087 会自己退出 / 关掉控制台窗口就掉线

**真实原因**：`wb_proxy.py` 注册了 Windows 控制台事件处理（`install_console_close_handler`），
收到 `CTRL_CLOSE_EVENT` / `CTRL_LOGOFF_EVENT` / `CTRL_SHUTDOWN_EVENT` 会主动 `os._exit(0)`
（避免端口被遗留占用）。所以**关闭那个 cmd 窗口 = 停服**，而且不一定留下任何错误日志。

**别误判成**：程序崩溃 / 内存溢出。

**处置与配套**：
- 新增 `[TERMINATION]`（收到哪个信号）与 `[FATAL_CRASH]`（未捕获异常回溯）日志标签，退出原因可查；
- **当前推荐的守护方式**：`wt-proxy-start.bat start [port]` 会用后台 worker（`wt-proxy-start.bat --worker <port>`）
  托管代理，代理退出即自动拉起；进程号写在 `wt-proxy-<port>.pid`，输出在 `wt-proxy-<port>.out.log` / `.err.log`。
- **遗留的独立守护脚本**：`start-guardian.bat`（简单 `:loop`，3 秒重启，退出码写 `proxy-guardian.log`）。
  两者都存在，别同时跑两套守护 —— 排查"谁关的/谁拉起的"时先确认是哪一套在管。

## 8. 停不掉代理 / 停了又自己起来

**真实原因**：守护机制会循环拉起，单纯 `taskkill` 掉 python 进程没用。

**正确做法**：`wt-proxy-stop.bat`（等价于 `wt-proxy-start.bat stop [port]`）。它会：
1. 写停机标志文件 `wt-proxy-<port>.stop`（先掐断守护循环，否则 3 秒后又拉起来）；
2. 按 `wt-proxy-<port>.pid` 杀掉守护 worker 进程树；
3. 杀掉端口监听进程；
4. 顺带停掉 `antigravity-proxy`（读 `antigravity-proxy/ag-proxy.pid`，并释放 43810）。

脚本内注释在各 bat 头部（bat 必须是 ASCII 注释，中文会破坏 cmd 解析）。

## 9. Trae 手动「刷新积分」被拒 / 积分多久刷新一次

**真实原因**：Trae 资产接口有频控。面板「刷新积分」带 `?force=1`，后端若距上次成功刷新
**不足 10 分钟**会直接拒绝（返回 429 + 还剩多久），避免把上游打到 429 甚至风控。

**现状**：看板每 15 分钟自动拉一次；普通展示读本地缓存，不打扰上游。

## 10. Trae 多账号：第二个账号总说「当前设备今日已经签到」

**真实原因**：多账号若共用同一个设备指纹（来自本机 icube 凭证），Trae 会按**设备**判定当日已签。

**处置**：`trae/trae_accounts.py` 的 `derive_device(uid)` 按 uid 稳定派生**每账号独立设备指纹**
（对齐 TraeWorkAssistant 的 `derive_device` 算法）；账号 `enabled` 状态与调度联动，
停用的账号在 `list_accounts` 里直接不参与。

## 11. omp（Oh My Pi）侧的两个"看起来像坏了"的配置

- **Enter 不发送、反而排队**：`~/.omp/agent/keybindings.yml` 里做了键位互换
  （`Enter = followUp`（排队等当前任务跑完）、`tty.input.submit = Ctrl+Q / Ctrl+Enter`）。
  文件内注释已写明：Windows Terminal 传不出独立的 `Ctrl+Enter`，因此备选键是 `Ctrl+Q`。
- **模型 `contextWindow` 只有 65000**：`~/.omp/agent/models.yml` 里
  `antigravity-proxy/gemini-3.8-flash` 刻意填了保守值（实测 gemini 单次输入上限 >763,639 tokens，
  约 1M 量级；代理自身声明 650000）。**这不是笔误**，是为了避免客户端攒出超限上下文。
  改大之前先读本文档第 1 条。

## 12. Qoder 账号「停用 / 删除」怎么看是否真生效

- `enabled` 落盘在 `qoder/qoder_data/accounts/<id>.json`，面板 `停用` 会同步写盘并热更新账号池；
- 停用后 `qoder/qoder_pool.py` 的 `Slot.available()` 直接返回不可用，**不会再被选中**，无需重启；
- 删除会连同密钥文件一并清除，不可恢复。

## 13. 本地补丁约定

本仓库对上游的非等价改动统一用 `PATCHED-BY-OPS` / `PATCH2-BY-OPS` 注释标记（`wb_proxy.py` 多处），
搜索该标记即可看到所有"与上游不同、且是本地有意为之"的位置。
新增此类改动请沿用同一标记，避免后来者误当 bug 修掉。
