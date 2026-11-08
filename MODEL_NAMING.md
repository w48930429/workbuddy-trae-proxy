# Oh My Pi (omp) 模型展示命名规范 (MODEL_NAMING)

本文档规范并统一 Oh My Pi (omp) 配置文件（`~/.omp/agent/models.yml`）中各反代渠道模型的展示名称（`name`），解决多上游网关反代同名国模（DeepSeek、GLM、Kimi、Step、MiniMax 等）造成的来源混淆、终端状态栏宽度溢出，以及混元多计费通道识别问题。

---

## 1. 核心架构与安全边界

- **底层 `id` 绝对冻结**：`id` 是网关路由和上游接口的真实调用标识（例如 `deepseek-v4.1-flash`、`hy4-preview-f`），**严禁更改**。
- **展示 `name` 纯前端渲染**：`name` 仅用于 omp 终端状态栏、模型选择下拉框与历史会话标识，修改后**完全不影响 API 转发、并发限制与 Tokenizer 解析**。

---

## 2. 命名公式与结构规范

统一采用 **方案 B-1：自然精炼缩写 + 渠道与区域尾缀** 规范：

$$\text{[模型品牌精炼名]}\ \text{[版本号]}\ \text{[档位/规格]}\ \text{[计费属性(可选)]}\ \mathbf{(\text{[渠道]}-\text{[区域]opt})}$$

### ① 渠道与区域尾部标识字典

后缀分两族：**反代网关用短码**（同品牌多通道，必须区分区域），**官方直连用品牌名**（单一上游，无需短码）。

| 标识尾缀 | 对应提供方 / 上游通道 | 适用范围说明 |
| :--- | :--- | :--- |
| **`(wb-cn)`** | WorkBuddy 国内站反代 (`local-wb-cn`) | 国内账号池，支持混元 4 免扣点与低倍率国模 |
| **`(wb-intl)`** | WorkBuddy 国际站反代 (`local-wb-intl`) | 国际账号池，含限免三大主力及 GPT/Gemini 商业模型 |
| **`(qd-cn)`** | Qoder 国内站反代 (`local-qoder-cn`) | 国内版账号池，含 QoDuck 限免及国内专属 Step 5 |
| **`(qd)`** | Qoder 国际站反代 (`local-qoder`) | 国际版账号池（注：`QoDuck` 官方海外旗舰保持单名不加后缀） |
| **`(Zhipu)`** | 智谱官方直连 (`zhipu`) | 品牌名尾缀，官方单一上游 |
| **`(iFlytek)`** | 讯飞星辰 MaaS 官方直连 (`iflytek`) | 品牌名尾缀，华北-1 region 按量计费 |
| **`(MiMo)`** | 小米 MiMo 通道 (`local-mimo`) | 专有通道 |

---

## 3. 常见模型精炼缩写与特殊区分规则

### ① 腾讯混元（Hunyuan）系列区分
混元 4 代在底层存在免费测试与商用付费双通道，统一以语义后缀区分：
- **`hy4-preview-f` / `hy4free`** $\rightarrow$ **`Hy4 Free (wb-cn)`**（0.00x 官方限免深度思考通道，零扣点）
- **`hy4std` / `hy4-preview`** $\rightarrow$ **`Hy4 Std (wb-cn)`** / **`Hy4 Std (wb-intl)`**（0.29x 官方正式商用通道，抗拥堵）
- **`hy3`** $\rightarrow$ **`Hy3 (wb-cn)`** / **`Hy3 (wb-intl)`**（混元 3 代通用对话）

### ② 主流国模品牌缩写对照
| 原始上游全称 | 精炼缩写标准 | 典型示例 |
| :--- | :--- | :--- |
| `DeepSeek-V4.1-Flash` | **`DS 4.1 Flash`** | `DS 4.1 Flash (wb-cn)` / `DS 4.1 Flash (qd-cn)` |
| `DeepSeek-V4-Pro` | **`DS 4 Pro`** | `DS 4 Pro (wb-cn)` / `DS 4 Pro (qd-cn)` |
| `GLM-5.3-Flash` | **`GLM 5.3 Flash`** | `GLM 5.3 Flash (wb-cn)` / `GLM 5.3 Flash (qd-cn)` |
| `GLM-5.3` (旗舰) | **`GLM 5.3`** | `GLM 5.3 (wb-cn)` / `GLM 5.3 (qd-cn)` |
| `Qwen3.8-Flash` | **`Qwen 3.8 Flash`** | `Qwen 3.8 Flash (qd-cn)` / `Qwen 3.8 Flash (qd)` |
| `Qwen3.8-Max` | **`Qwen 3.8 Max`** | `Qwen 3.8 Max (qd-cn)` / `Qwen 3.8 Max (qd)` |
| `Kimi-K3.1-Pro` | **`Kimi 3.1 Pro`** | `Kimi 3.1 Pro (qd-cn)` |
| `Kimi-K2.8-Preview` | **`Kimi 2.8`** | `Kimi 2.8 (qd-cn)` |
| `Step-5-Preview` | **`Step 5`** | `Step 5 (wb-cn)` / `Step 5 (qd-cn)` / `Step 5 (wb-intl)` |
| `MiniMax-M3.1-Flash` | **`MiniMax 3.1 Flash`** | `MiniMax 3.1 Flash (wb-cn)` / `MiniMax 3.1 Flash (qd-cn)` |
| `QoDuck` (海外版) | **`QoDuck`** | 海外版保持极简主品牌 |
| `QoDuck` (国内站) | **`QoDuck (qd-cn)`** | 显式标记国内站通道 |

---

## 4. 各 Provider 现行模型对照清单

### ① `local-wb-cn` (WorkBuddy 国内站)
| 上游 ID | 呈现名称 (`name`) | 说明 |
| :--- | :--- | :--- |
| `step-5-preview` | **`Step 5 (wb-cn)`** | 阶跃星辰主力推理 |
| `glm-5.3-flash` | **`GLM 5.3 Flash (wb-cn)`** | 智谱极速版 |
| `glm-5.3` | **`GLM 5.3 (wb-cn)`** | 智谱旗舰 |
| `hy3` | **`Hy3 (wb-cn)`** | 混元 3 通用 |
| `hy4free` | **`Hy4 Free (wb-cn)`** | 混元 4 免扣点推理通道 |
| `hy4-preview-f` | **`Hy4 Free (wb-cn)`** | 混元 4 preview 免扣点 |
| `hy4std` | **`Hy4 Std (wb-cn)`** | 混元 4 标准付费通道 |
| `minimax-m3` | **`MiniMax M3 (wb-cn)`** | MiniMax M3 对话 |
| `kimi-k3-1` | **`Kimi 3 (wb-cn)`** | 月之暗面 K3 |
| `deepseek-v4.1-flash` | **`DS 4.1 Flash (wb-cn)`** | DeepSeek 闪电推理 |
| `deepseek-v4-pro` | **`DS 4 Pro (wb-cn)`** | DeepSeek 旗舰推理 |
| `space-bunny` | **`Space Bunny (wb-cn)`** | 官方专属优化模型 |
| `minimax-m3.1-flash` | **`MiniMax 3.1 Flash (wb-cn)`** | MiniMax 3.1 极速 |
| `mimo-v2.6-pro` | **`MiMo 2.6 Pro (wb-cn)`** | 小米 MiMo 旗舰 |

### ② `local-qoder-cn` (Qoder 国内站)
| 上游 ID | 呈现名称 (`name`) | 说明 |
| :--- | :--- | :--- |
| `QoDuck` | **`QoDuck (qd-cn)`** | 官方 0.00x 深度微调限免模型 |
| `Qwen3.8-Flash` | **`Qwen 3.8 Flash (qd-cn)`** | 0.04x 超低倍率阿里通义 |
| `GLM-5.3-Flash` | **`GLM 5.3 Flash (qd-cn)`** | 0.1x 智谱极速版 |
| `DeepSeek-Flash` | **`DS 4.1 Flash (qd-cn)`** | 0.1x DeepSeek 4.1 闪电版 |
| `Qwen3.8-Max` | **`Qwen 3.8 Max (qd-cn)`** | 0.2x 阿里通义千问旗舰 |
| `DeepSeek-V4-Pro` | **`DS 4 Pro (qd-cn)`** | 0.5x DeepSeek 旗舰 |
| `GLM-5.3` | **`GLM 5.3 (qd-cn)`** | 0.8x 智谱旗舰 |
| `Kimi-K2.8-Preview` | **`Kimi 2.8 (qd-cn)`** | 0.8x 月之暗面通用 |
| `Step-5-Preview` | **`Step 5 (qd-cn)`** | 国内站独有阶跃星辰模型 |
| `Kimi-K3.1-Pro` | **`Kimi 3.1 Pro (qd-cn)`** | 1.0x 月之暗面旗舰 |
| `MiniMax-M3.1-Flash` | **`MiniMax 3.1 Flash (qd-cn)`** | 0.1x MiniMax 极速 |
| `Auto` | **`Auto (qd-cn)`** | 智能动态调度档 |

### ③ `local-wb-intl` (WorkBuddy 国际站)
- **限免三主力**：`Step 5 (wb-intl)`、`DS 4.1 Flash (wb-intl)`、`Hy3 (wb-intl)`
- **国模通道**：`Hy4 Std (wb-intl)`、`GLM 5.3 (wb-intl)`、`GLM 5.2 (wb-intl)`、`Kimi 3 (wb-intl)`、`Kimi 2.6 (wb-intl)`、`MiniMax 3.1 Flash (wb-intl)`
- **国际商用模型**：`GPT 6 Astra (wb-intl)`、`GPT 6.1 Sol (wb-intl)`、`GPT 6 Sol (wb-intl)`、`GPT 6 Luna (wb-intl)`、`GPT 5.6 Sol (wb-intl)`、`GPT 5.6 Terra (wb-intl)`、`GPT 5.6 Luna (wb-intl)`、`GPT 5.5 (wb-intl)`、`GPT 5.4 (wb-intl)`、`Gemini 3.5 Flash (wb-intl)`、`Gemini 3.8 Flash (wb-intl)`

### ④ `local-qoder` (Qoder 国际站)
- **海外旗舰独占命名**：`QoDuck`（干净主名）
- **通用模型**：`Qwen 3.8 Flash (qd)`、`GLM 5.3 Flash (qd)`、`DS Flash (qd)`、`MiniMax 3.1 Flash (qd)`、`Qwen 3.8 Max (qd)`、`Efficient (qd)`、`Ultimate (qd)`、`Auto (qd)`、`DS 4 Pro (qd)`、`GLM 5.3 (qd)`、`Kimi 2.8 (qd)`、`Kimi 3.1 Pro (qd)`

### ⑤ `iflytek` (讯飞星辰 MaaS 官方直连)
官方直连单一上游，后缀用品牌名而非渠道短码：

| 上游 ID | 呈现名称 (`name`) | 说明 |
| :--- | :--- | :--- |
| `spark-x2.6-4b` | **`Spark X2.6 4B (iFlytek)`** | 星火 X2.6 开源 4B，1M 上下文，三档 reasoning effort |

---

## 5. 新增模型维护准则

后续在 `models.yml` 中新增模型条目时，必须严格执行以下四步检查：
1. **上游对齐**：核对 `id`、`contextWindow`、`maxTokens`、`reasoning` 是否与反代上游实拉的 JSON 一致；
2. **精炼去噪**：去除上游临时标记（如 `-Preview`、`-1017`、多余连字符等），仅保留版本号与档位；
3. **附加后缀**：反代渠道加短码 `(wb-cn)`、`(qd-cn)`、`(wb-intl)`、`(qd)`；官方直连加品牌名 `(iFlytek)`、`(Zhipu)`。
4. **思考档位对齐**：上游 `reasoning_effort` 只认部分档位时（如讯飞仅 `none/low/high/xhigh`），必须写 `thinking.efforts` 白名单夹档，需要跨档映射时写 `thinking.effortMap`。**`compat.reasoningEffortMap` 对自定义 provider 不生效**，写了也不会改变上线档位——机制与抓包验证方法见 `omp-provider` skill。
