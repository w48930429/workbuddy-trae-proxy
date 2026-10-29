"""CodeArts (Snap) 上游接入 —— 纯 Python 复刻。

与本仓库的 ``qoder/`` 同一套做法：Go/Rust 项目只当**协议事实来源**
（2951461586/CodeArts2API 的 ``docs/UPSTREAM.md`` 与 ``internal/codearts/``），
实现本身是纯 Python，不 vendor 源码、不调用外部二进制、不起子进程。

模块分工：

``codearts_signer``
    ``SDK-HMAC-SHA256`` 签名器。含两处与官方文档不同、漏了就必然
    ``APIG.0301`` 的怪癖：CanonicalURI 强制补尾斜杠、CanonicalHeaders
    自带尾 ``\\n``（导致 SignedHeaders 前多一个空行）。

``codearts_session``
    只读地读取本机 CodeArts Agent 的登录态（DPAPI + OSCrypt/AES-GCM）。
    **不续期**：refresh_token 单次轮转且 DPoP 绑定，被网关用掉会锁死 IDE 登录。

``codearts_client``
    上游协议：身份头、稳定 session id、``error_code`` 状态归一化、
    ``glm-5.3`` 仅流式的客户端适配。

``codearts_route``
    把上游包成 OpenAI 兼容端点。模型名一律带 ``codearts-`` 前缀 —— 上游
    也有 ``deepseek-v4.1-flash``，而那是本网关额度不足时的降级目标，
    不加前缀会把请求打到错误的上游。
"""
