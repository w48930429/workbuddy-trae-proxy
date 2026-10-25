// 缓存 + id 内嵌 Gemini 思考签名，使多轮对话能回传真实 thoughtSignature。
// Anthropic 通道（anthropic.ts）与 OpenAI 通道（gateway/adapter.ts、server.ts）共用。
//
// Google CCA 对 Gemini 3.x 的多轮 functionCall 有 thought_signature 校验：
// 回放的历史 assistant functionCall 部分必须带签名（真实签名或
// `skip_thought_signature_validator` 哨兵），否则上游返回 HTTP 400。
//
// 签名有两条回传路径：
//  1) 服务端内存缓存，按 call id 查找（原实现，保留作兜底）；
//  2) 把签名编码进返回给客户端的 tool call id：`<id>__ts<len>__<sig>`，
//     客户端原样回传 id 即带回签名 —— 跨进程重启 / 多实例 / 客户端改写 id
//     时更可靠（借鉴 magpie 的做法：签名搭 id 的便车）。
//     超长签名不塞进 id（避免撑爆客户端数据库索引），改存本地 store，id 里
//     只放 `<id>__tsk<len>__<key>` 短引用。
// 长度前缀 <len> 用于识别被客户端截断的残缺签名：残缺的一律当作无签名。

const MAX_CACHE_ENTRIES = 1000;
// 超过则改走 store 引用，避免 id 过长撑爆客户端存储
const MAX_INLINE_SIGNATURE = 512;

const thoughtSignatureCache = new Map<string, string>();
const signatureStore = new Map<string, string>();
let signatureStoreSeq = 0;

const SIG_MARK = '__ts';
const SIG_END = '__';

function randomStoreKey(): string {
	return `s${(signatureStoreSeq++).toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

/** 把签名编码进 call id；客户端原样回传 id 时签名即随之带回。 */
export function signedId(id: string | undefined, signature: string | undefined): string | undefined {
	if (!signature) return id;
	const base = id || `call_${Math.random().toString(36).slice(2, 14)}`;
	if (signature.length > MAX_INLINE_SIGNATURE) {
		// 超长签名：存本地，id 里只放短引用
		let key: string;
		do { key = randomStoreKey(); } while (signatureStore.has(key));
		signatureStore.set(key, signature);
		if (signatureStore.size > MAX_CACHE_ENTRIES) {
			const first = signatureStore.keys().next().value;
			if (first) signatureStore.delete(first);
		}
		return `${base}${SIG_MARK}k${key.length}${SIG_END}${key}`;
	}
	const enc = Buffer.from(signature, 'utf8').toString('base64url');
	return `${base}${SIG_MARK}${enc.length}${SIG_END}${enc}`;
}

/**
 * 从 call id 拆出真实 id 与签名。无签名、或签名被截断时，原样返回 id、签名空串。
 */
export function unsignedId(id: string | undefined): [string, string] {
	if (!id) return ['', ''];
	// 正向扫描：找第一个格式合法的标记（payload 自身含 __ts 也不会误命中）
	for (let at = 1; at < id.length; ) {
		const i = id.indexOf(SIG_MARK, at);
		if (i < 0) break;
		at = i + 1;
		let rest = id.slice(i + SIG_MARK.length);
		let kept = false;
		if (rest.startsWith('k')) { kept = true; rest = rest.slice(1); }
		let d = 0;
		while (d < rest.length && rest[d] >= '0' && rest[d] <= '9') d++;
		const tail = rest.slice(d);
		if (!tail.startsWith(SIG_END)) continue; // 不是标记：继续找下一个
		if (kept && d === 0) continue;
		const payload = tail.slice(SIG_END.length);
		if (!payload) return [id, '']; // 空 payload：当作无签名，原样返回 id
		// 第一个格式合法的标记即决定：残缺（长度不符）视为无签名
		if (d > 0 && rest.slice(0, d) !== String(payload.length)) return [id, ''];
		if (kept) {
			const sig = signatureStore.get(payload);
			return sig ? [id.slice(0, i), sig] : [id, ''];
		}
		try {
			return [id.slice(0, i), Buffer.from(payload, 'base64url').toString('utf8')];
		} catch {
			return [id, ''];
		}
	}
	return [id, ''];
}

/** 缓存真实签名（响应侧调用），按 call id 索引，作为回传兜底。 */
export function rememberThoughtSignature(callId: string | undefined, signature: string | undefined): void {
	if (!callId || !signature) return;
	thoughtSignatureCache.set(callId, signature);
	if (thoughtSignatureCache.size > MAX_CACHE_ENTRIES) {
		const first = thoughtSignatureCache.keys().next().value;
		if (first) thoughtSignatureCache.delete(first);
	}
}

/**
 * 回放时取签名：优先用 call id 内嵌的签名，其次查内存缓存，
 * 都拿不到时回退到 Google 文档化的绕过哨兵。
 */
export function resolveThoughtSignatureForReplay(callId: string | undefined): string {
	if (!callId) return 'skip_thought_signature_validator';
	const [own, sig] = unsignedId(callId);
	if (sig) return sig;
	return thoughtSignatureCache.get(own)
		|| thoughtSignatureCache.get(callId)
		|| 'skip_thought_signature_validator';
}