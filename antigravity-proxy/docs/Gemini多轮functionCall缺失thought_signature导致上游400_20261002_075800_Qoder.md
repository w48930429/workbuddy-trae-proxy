# Gemini 多轮 functionCall 缺 thought_signature 导致上游 HTTP 400

- **用途**：记录一次 `Google Upstream returned HTTP 400: Function call is missing a thought_signature` 的完整排查结论，避免下次从错误文本重新推导。
- **整理时间**：2026-10-02 07:58（系统时钟，UTC+8）
- **整理者**：Qoder
- **适用版本**：`HEAD = 642a9cf`（`feat(anthropic): support output_config.format ...`）**加上尚未提交的工作区补丁**（见「修复落点」）。`HEAD` 单独 checkout 时本问题必然复现。
- **实测环境**：Windows / `npm start` = `tsx src/index.ts` / 端口 43810 / 客户端为 omp（OpenAI `chat/completions` 通道）

## 一句话结论

`HEAD` 版本的 OpenAI 通道在把 assistant 历史里的 `tool_calls` 转成 Google `functionCall` 部件时**完全没有写 `thoughtSignature` 字段**，而 Gemini 3.x 对多轮回放的 functionCall 强制校验该字段，于是上游返回 HTTP 400。工作区补丁补上了「优先真实签名、缺失时回退 `skip_thought_signature_validator` 哨兵」，**并且必须重启服务才生效**。

## 现象（原文摘录）

```
Google Upstream returned HTTP 400: {
  "error": {
    "code": 400,
    "message": "Function call is missing a thought_signature in functionCall parts. This is
required for tools to work correctly ... Additional data, function call `default_api:bash` ,
position 1318.",
    "status": "INVALID_ARGUMENT"
  }
}
```

关键观察（后来都成了定位依据）：

1. 错误体是 **Google 原生 JSON 形状**（`code` / `message` / `status`），不是 OpenAI 的 `error.type` 形状 —— 说明它是上游原样透传回来的，不是本地校验失败的包装。
2. 报错里的函数名 `default_api:bash` **带冒号**，而客户端实际声明的工具名是 `bash`、`read`、`edit` 等（无冒号）。冒号是 Google 侧的 API 命名空间前缀，**不是**客户端名字泄漏，也不是「改名没生效」的证据。
3. `position 1318` 与失败请求的规模吻合：该请求有 **1320 条 messages**（1 system + 51 user + 598 assistant + 670 tool），去掉 system 后是 1319 条 contents，1318 即最后一条。*计数字段的确切单位未证实，只可用作量级参照。*

## 根因（代码级证据）

| 版本 | 位置 | 代码 | `thoughtSignature` |
|---|---|---|---|
| `HEAD`（00:59 实际在跑的） | `src/gateway/adapter.ts:95` | `parts.push({ functionCall: { id, name, args } });` | **0 处** |
| 工作区补丁（现在在跑的） | `src/gateway/adapter.ts:99` | `parts.push({ functionCall: { id, name, args }, thoughtSignature: resolveThoughtSignatureForReplay(id) });` | 2 处 |

核对命令与输出：

```
$ git show HEAD:src/gateway/adapter.ts | grep -c thoughtSignature
0
$ grep -c thoughtSignature src/gateway/adapter.ts
2
```

## 修复落点（双通道都要改，缺一不可）

共享实现：`src/gateway/thought-signature.ts`（新文件，**未纳入版本控制**，`git status` 显示 `??`）

- `rememberThoughtSignature(callId, signature)` —— 从上游响应里学到的真实签名按 `callId` 入 `Map`，容量上限 `MAX_CACHE_ENTRIES = 1000`，超限淘汰最早项。
- `resolveThoughtSignatureForReplay(callId)` —— 命中返回真实签名，否则返回哨兵 `skip_thought_signature_validator`。所以该函数**永远返回非空**。

两条通道的接线：

| 通道 | 写入点 | 学习点（从响应里收签名） | 门控 |
|---|---|---|---|
| OpenAI `chat/completions` | `adapter.ts:99` | `adapter.ts:258`（`extractTextFromGoogleChunk` 内） | 无条件写 |
| Anthropic `messages` | `anthropic.ts:156` | `anthropic.ts:79` | **`if (isGemini)` 才写**，`isGemini` 来自 `anthropic.ts:378` 的 `modelDef.upstreamModel.startsWith('gemini')` |

路由接线确认（排查时一度误判 Anthropic 通道没挂载）：`server.ts:122` 调用 `registerAnthropicRoutes(...)`，内部注册在 `anthropic.ts:768-773`；`server.ts:427-429` 注册 `chat/completions`。**没有** Gemini 原生路由（全仓搜 `generateContent` / `v1beta` 零命中），所以客户端只能走上面两条通道之一。

## 为什么"改了代码但错误还在"——时间线

| 时刻 | 事件 | 证据来源 |
|---|---|---|
| 10-02 00:59 | 400 发生，客户端把完整请求体落盘 | `~/.omp/logs/http-400-requests/1790873999983-*.json`（2 908 780 B） |
| 10-02 01:00 | 再次 400 | 同目录 `1790874040047-*.json`（2 909 495 B） |
| 10-02 **01:15:00** | `thought-signature.ts` 建立 | `Get-Item .LastWriteTime` |
| 10-02 01:18 / 01:19 | `anthropic.ts` / `adapter.ts` 改完 | 文件 mtime |
| 10-02 **01:20:42** | 服务进程启动（PID 2768，`node.exe --require tsx/preflight.cjs`） | `Get-Process StartTime` |
| 10-02 01:21–01:22 | 6 次 `POST /v1/chat/completions`，无 400 | `ag-proxy.log` |
| 10-02 01:10 之后 | **新增 400 落盘数 = 0** | `find -newermt "2026-10-02 01:10" \| wc -l` → 0 |
| 10-02 07:4x | 复现实测：带 assistant `tool_calls` + `tool` 回执的历史 → **HTTP 200 / 2.0 s**，`finish_reason=tool_calls`，usage 102 in / 27 out | 直接请求 `127.0.0.1:43810` |

`npm start` = **`tsx src/index.ts`**（`package.json` scripts），`tsx` 不带 watch、不热重载，所以 01:19 保存的代码必须等 01:20 的重启才生效；在那之前，磁盘上已经是新代码、内存里还是旧代码 —— 这就是"看着已经修了却还在报同一个错"的全部原因。

## 必须避开的三条假线索

1. **`dist/` 陈旧与本问题无关，但别把它当不存在。**
   `dist/gateway/adapter.js` 里 `thoughtSignature` 出现 **0 次**，`dist/gateway/thought-signature.js` **不存在**，整个 `dist` 是 09-29 23:16 的产物，比源码旧两天。可是 `npm start` 走 `tsx src/index.ts`，**根本不读 dist**，所以它陈旧不会让线上复现这个 bug。
   **真正的坑**：`package.json` 的 `main` = `dist/index.js`，`Dockerfile` / `docker-compose.yml` / `node .` 都走 dist。用这几条路启动，跑的就是没有签名的旧代码，本 400 会原样回来。要么先 `npm run build`（= `tsc`），要么别用这几条路。
2. **`default_api:bash` 的冒号不代表改名逻辑失效。** 两条通道都会把非法字符换成下划线（`adapter.ts:61-62` 的 `normalizeToolName`、`anthropic.ts:54-56` 的 `sanitizeToolName`），Google 报错里的冒号是它自己的命名空间。用它来判断"请求没经过本代理"会得出完全相反的结论。
3. **别用 `/v1/models` 的空结果判断服务挂了。** 该端点要求鉴权，无 key 时返回 `{"error":{"message":"Missing Authorization or x-api-key header"}}`；把它解析成 `data` 列表会得到"模型数 0"的假象。健康检查用 `GET /health`。

## 遗留风险（补丁没有覆盖的部分）

1. **签名缓存是进程内 `Map`，重启即清空。** 服务重启后，长历史里所有已发生过的 `tool_calls` 都查不到真实签名，只能退回哨兵。本次实测哨兵被上游接受，但这是**上游行为**，不是本项目的保证；一旦 Google 收紧对 `skip_thought_signature_validator` 的容忍，跨重启的长会话会整体失败，而不是零星失败。若要根治，需把 `callId → signature` 持久化。
2. **`MAX_CACHE_ENTRIES = 1000` 对超长会话偏小。** 淘汰策略是最旧项出队（`Map` 插入序），598 个 assistant 轮次的会话里，早期签名会被挤出后退回哨兵。
3. **Anthropic 通道有 `if (isGemini)` 门控**（`anthropic.ts:155`）。若某条链路的 `upstreamModel` 命名不以 `gemini` 开头、但实际打到 Gemini 3.x，签名就会被整个跳过 —— 本问题会在 `/v1/messages` 上复现。OpenAI 通道无此门控，两条通道行为不对称。
4. **单次请求 1320 条消息 / 2.9 MB 本身是更大的问题**：慢、贵，且每一轮都在重放全部工具历史。签名补丁只是让它能发出去，并没有让它变合理。优先做客户端侧的历史压缩/截断。

## 自查步骤（按顺序，零额度起）

1. `GET http://127.0.0.1:43810/health` —— 确认服务活着、以及**它是不是补丁后启动的**（比对进程 StartTime 与 `src/gateway/thought-signature.ts` 的 mtime；StartTime 更早就必须重启）。
2. **离线跑转换器，不联网不消耗额度**（本次最有效的一步）：写一个临时脚本 `import { convertOpenAIToCloudCode } from './src/gateway/adapter.js'`，构造一条含 `assistant.tool_calls` + `role: 'tool'` 回执的请求，打印返回 payload 的 `request.contents`，逐个检查 `functionCall` 部件旁边有没有 `thoughtSignature`、`name` 是否被改名。注意返回结构是 `{project, model, request}`，`contents` 在 **`payload.request.contents`**，不在顶层。
3. 小量真请求验证端到端（会消耗上游额度）。
4. 失败就取现场：客户端会把 400 的完整请求体落盘（omp 在 `~/.omp/logs/http-400-requests/*.json`，含 `url` / `body` / `errorResponse`）。**按文件时间筛**比重读错误文本可靠得多 —— 本次结论完全建立在"落盘时间早于补丁时间"这一条上。

## 尚未验证

- `position 1318` 的计数单位（contents 下标 / 部件下标 / 字符偏移）未证实。
- Docker 与 `node .` 路径未实测，只从 `main` 字段与 dist 内容推断。
- 上游对哨兵值的长期承诺未知，需以 Google 官方文档为准。
- 服务日志里没有记录上游错误体（`ag-proxy.log` 全程无该 400），排错只能靠客户端落盘 —— 若要改进，应在上游失败时记录 Google 返回的原始 body。

## 现场待办

`src/gateway/adapter.ts`、`src/gateway/anthropic.ts` 处于 `M`（已改未提交），`src/gateway/thought-signature.ts` 处于 `??`（**未纳入版本控制**）。这三项若被 `git clean` / 切分支 / 重新 clone，修复会直接消失，本 400 随即复现。
