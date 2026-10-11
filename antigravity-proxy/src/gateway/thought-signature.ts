// 缓存 Gemini 思考签名，以便在多轮对话中回传真实 thoughtSignature。
// Anthropic 通道（anthropic.ts）与 OpenAI 通道（gateway/adapter.ts、server.ts）共用。
//
// Google CCA 对 Gemini 3.x 的多轮 functionCall 有 thought_signature 校验：
// 回放的历史 assistant functionCall 部分必须带签名（真实签名或
// `skip_thought_signature_validator` 哨兵），否则上游返回 HTTP 400。

const MAX_CACHE_ENTRIES = 1000;

const thoughtSignatureCache = new Map<string, string>();

export function rememberThoughtSignature(callId: string | undefined, signature: string | undefined): void {
	if (!callId || !signature) return;
	thoughtSignatureCache.set(callId, signature);
	if (thoughtSignatureCache.size > MAX_CACHE_ENTRIES) {
		const first = thoughtSignatureCache.keys().next().value;
		if (first) thoughtSignatureCache.delete(first);
	}
}

/** 优先真实签名；缺失时回退到 Google 文档化的绕过哨兵。 */
export function resolveThoughtSignatureForReplay(callId: string | undefined): string {
	return (callId && thoughtSignatureCache.get(callId)) || 'skip_thought_signature_validator';
}
