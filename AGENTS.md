# Upstream Reference Repositories & Agent Protocol Specifications

本项目在复刻与实现华为云 **CodeArts Agent** 协议及多账号网关能力时，参考并纯 Python 原生复刻了以下 GitHub 远端开源项目的协议设计与实现规范：

---

## 1. 核心参考上游仓库清单

### ① [HITZY2002/codearts2api](https://github.com/HITZY2002/codearts2api)
- **原始语言**：Go
- **复刻内容**：
  - 华为云 OAuth 2.0 PKCE 完整鉴权流程（`code_verifier` + `code_challenge` S256）。
  - DPoP（Demonstrating Proof-of-Possession at the Application Layer）椭圆曲线（NIST P-256 / ES256）密钥对生成与 JWS 证明头签名规范。
  - STS Token 交换接口：`POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens`。
  - `refresh_token` 滚动刷新机制与安全续期周期（`safelyRenewTokenInterval`）调度算法。
- **纯 Python 对应实现**：`codearts/codearts_oauth.py`、`codearts/codearts_session.py`。

---

### ② [Johnsheng1/codearts2api](https://github.com/Johnsheng1/codearts2api)
- **原始语言**：Python
- **复刻内容**：
  - 华为云永久访问密钥（AK / SK）直连模式的签名参数结构。
  - 华为云 API 网关（APIG）`SDK-HMAC-SHA256` 签名器的边界测试基准。
- **纯 Python 对应实现**：`codearts/codearts_signer.py`、`codearts/codearts_client.py`。

---

### ③ [2951461586/CodeArts2API](https://github.com/2951461586/CodeArts2API)
- **原始语言**：Go
- **复刻内容**：
  - 桌面客户端配置读取：Windows DPAPI 解密 `Local State` 获取 `os_crypt` AES-GCM Master Key。
  - SQLite 数据库读取：从 `%APPDATA%/codearts-agent/User/globalStorage/state.vscdb` 中提取 `ItemTable` 内的 `HuaweiCloudSession` 密文。
  - 华为云服务端并发控制机制：解析 `x-ot-session-id`（单账号最大并发 3）与防并发超限规避规则。
- **纯 Python 对应实现**：`codearts/codearts_session.py`、`codearts/codearts_client.py`。

---

### ④ [RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API)
- **原始语言**：Python
- **复刻内容**：
  - 多上游网关融合架构：OpenAI 兼容协议与私有 AI 插件协议的双向转译模式。
  - DPoP ES256 JWT Payload 字段要求（`htm`, `htu`, `iat`, `jti`）及标准 Base64URL 编解码。
- **纯 Python 对应实现**：`codearts/codearts_route.py`、`codearts/codearts_oauth.py`。

---

### ⑤ 华为云官方 CodeArts Agent 核心扩展（内部基准标准）
- **来源路径**：`D:\Program Files\CodeArts Agent\resources\app\extensions\huaweicloud.authentication\dist\plugin.js`（版本 5.4.2）
- **规范基准**：
  - `portalHost`: `https://codearts.huaweicloud.com/portal`
  - `iamStsHost`: `https://sts.cn-north-4.myhuaweicloud.com`
  - `AUTH_REDIRECT_URL`: `/oauth/callback`
  - `CLIENT_ID`: `codearts-agent`
  - 授权请求地址格式：
    ```
    https://codearts.huaweicloud.com/portal/authorize?theme=2&locale=zh-cn&uri_scheme=codearts-agent&client_id=codearts-agent&port={project_port}&code_challenge={code_challenge}&code_challenge_method=S256
    ```
  - 回调重定向格式：
    ```
    http://127.0.0.1:{project_port}/oauth/callback?code={authorization_code}
    ```

---

## 2. 架构约定与工程约束

1. **纯 Python 实现**：零跨语言依赖、零编译二进制，全套密码学算法由 Python 3.12 标准库与 `cryptography`（ES256 ECDSA）驱动。
2. **端口复用**：OAuth 回调完全复用本项目主网关监听端口（`wb_proxy.py`），绝不开启额外的临时端口。
3. **凭据优先级链**：
   - 第一级：本项目独立 Web OAuth 授权账号（`accounts/codearts_account.json`）
   - 第二级：永久 AK / SK 直填配置（`config.json` -> `codearts.ak` / `sk`）
   - 第三级：本机桌面客户端只读提取（DPAPI / `state.vscdb`）
