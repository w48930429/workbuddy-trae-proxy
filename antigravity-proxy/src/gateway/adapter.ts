import {
  OpenAIChatCompletionRequest,
  GoogleCloudCodePayload,
  GoogleContent,
  OpenAIChatCompletionChunk
} from '../types.js';
import { resolveDynamicUpstreamModel, ThinkingEffort } from './models.js';
// 工具参数 schema 清洗：与 anthropic 入口共用同一实现（anyOf/oneOf/allOf 在 CloudCode 侧非法）
import { sanitizeGoogleSchema } from './schema.js';
// 缓存 + id 内嵌 Gemini 思考签名：共享实现见 gateway/thought-signature.ts
import {
  rememberThoughtSignature,
  resolveThoughtSignatureForReplay,
  signedId,
  unsignedId
} from './thought-signature.js';

/** 解析 data URL（data:image/png;base64,xxxx）中的 mime 与 base64 数据。 */
function parseDataUrl(url: string): { mimeType: string; data: string } | null {
  const m = /^data:([^;,]*)(;base64)?,(.*)$/s.exec(url || '');
  if (!m || !m[2] || !m[3]) return null;
  return { mimeType: m[1] || 'image/png', data: m[3] };
}

/** 从图片 URL 猜测 mime 类型（远程图无法预读时使用）。 */
function guessImageMime(url: string): string {
  const low = (url || '').toLowerCase();
  if (low.includes('.jpg') || low.includes('.jpeg')) return 'image/jpeg';
  if (low.includes('.webp')) return 'image/webp';
  if (low.includes('.gif')) return 'image/gif';
  return 'image/png';
}

/**
 * 将 OpenAI 格式的 chat completions 请求转换为 Google CloudCode / Gemini 内部 payload
 * 自动检测客户端（CC / Codex）随时传入的 thinking / reasoning_effort，动态控制底层思考强度
 */
export function convertOpenAIToCloudCode(req: OpenAIChatCompletionRequest): {
  payload: GoogleCloudCodePayload;
  upstreamModel: string;
  pool: 'cli' | 'sandbox';
} {
  // 1. 动态检测客户端随请求传入的思考强度控制
  let effort: ThinkingEffort = 'high';
  let budgetTokens: number | undefined = undefined;

  if (req.reasoning_effort) {
    effort = req.reasoning_effort;
  } else if (req.thinking) {
    if (req.thinking.type === 'disabled') {
      effort = 'none';
    } else if (typeof req.thinking.budget_tokens === 'number') {
      budgetTokens = req.thinking.budget_tokens;
      if (budgetTokens <= 1500) effort = 'low';
      else if (budgetTokens <= 5000) effort = 'medium';
      else effort = 'high';
    }
  } else if (req.thinking_budget !== undefined || req.thinkingBudget !== undefined) {
    budgetTokens = req.thinking_budget ?? req.thinkingBudget;
    if (budgetTokens! <= 1500) effort = 'low';
    else if (budgetTokens! <= 5000) effort = 'medium';
    else effort = 'high';
  }

  const modelDef = resolveDynamicUpstreamModel(req.model, effort);
  const contents: GoogleContent[] = [];
  let systemInstruction: { parts: { text: string }[] } | undefined = undefined;
  const toolCallIds = new Map<string, string>();
  // 每个函数名下尚未被回应的 call id 队列：tool 结果只给 name 不带 id 时按序遍历配对
  const openByName = new Map<string, string[]>();

  const normalizeToolName = (name: string) =>
    (name || 'tool').replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 128) || 'tool';

  const appendContentPart = (parts: any[], part: any) => {
    if (typeof part === 'string') {
      if (part) parts.push({ text: part });
    } else if (part?.type === 'text' && typeof part.text === 'string') {
      if (part.text) parts.push({ text: part.text });
    } else if (part?.type === 'image_url') {
      // 图片走 Gemini 原生多模态字段：base64 data URL -> inlineData；
      // 远程 URL -> fileData（跟随 Google 上游对 fileUri 的支持）。
      // 早期实现把图片降级为文本标记，等于丢弃了图像输入。
      const url = part.image_url?.url || '';
      const data = parseDataUrl(url);
      if (data) {
        parts.push({ inlineData: { mimeType: data.mimeType, data: data.data } });
      } else if (url) {
        parts.push({ fileData: { mimeType: guessImageMime(url), fileUri: url } });
      }
    }
  };

  for (const msg of req.messages) {
    if (msg.role === 'system') {
      const textContent = typeof msg.content === 'string'
        ? msg.content
        : Array.isArray(msg.content)
          ? msg.content.map(p => (typeof p === 'string' ? p : p.text || '')).join('\n')
          : String(msg.content || '');
      systemInstruction = {
        parts: [{ text: textContent }]
      };
    } else if (msg.role === 'assistant') {
      const parts: any[] = [];
      if (typeof msg.content === 'string') appendContentPart(parts, msg.content);
      else if (Array.isArray(msg.content)) msg.content.forEach(part => appendContentPart(parts, part));
      for (const call of msg.tool_calls || []) {
        // 客户端回传的 id 可能内嵌了上轮的 thoughtSignature，先拆出真实 id
        const [ownId] = unsignedId(call.id);
        const name = normalizeToolName(call.function?.name);
        const id = ownId || `call_${contents.length}_${parts.length}`;
        toolCallIds.set(id, name);
        const queue = openByName.get(name);
        if (queue) queue.push(id); else openByName.set(name, [id]);
        let args: any = {};
        try { args = JSON.parse(call.function?.arguments || '{}'); } catch { args = { raw_arguments: call.function?.arguments || '' }; }
        // Gemini 3.x 多轮 functionCall 必须带 thought_signature（真实签名或
        // skip_thought_signature_validator 哨兵），否则上游 HTTP 400
        parts.push({ functionCall: { id, name, args }, thoughtSignature: resolveThoughtSignatureForReplay(call.id) });
      }
      contents.push({
        role: 'model',
        parts: parts.length ? parts : [{ text: '' }]
      });
    } else if (msg.role === 'tool') {
      let [ownToolId] = unsignedId(msg.tool_call_id);
      const name = msg.name || (ownToolId ? toolCallIds.get(ownToolId) : undefined) || 'tool';
      // tool 结果没带 id 时，回退到该函数名下最早的未回应 call
      if (!ownToolId) {
        const queue = openByName.get(name);
        if (queue && queue.length) ownToolId = queue[0];
      }
      const queue = openByName.get(name);
      if (queue) {
        const idx = queue.indexOf(ownToolId);
        if (idx >= 0) queue.splice(idx, 1);
      }
      const output = typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content ?? '');
      contents.push({
        role: 'user',
        parts: [{ functionResponse: { id: ownToolId || msg.tool_call_id, name: normalizeToolName(name), response: { output } } }]
      });
    } else {
      const parts: any[] = [];
      if (typeof msg.content === 'string') appendContentPart(parts, msg.content);
      else if (Array.isArray(msg.content)) msg.content.forEach(part => appendContentPart(parts, part));
      contents.push({
        role: 'user',
        parts: parts.length ? parts : [{ text: '' }]
      });
    }
  }

  // 必须保证至少有一个 user content
  if (contents.length === 0) {
    contents.push({
      role: 'user',
      parts: [{ text: 'Hello' }]
    });
  }

  const isGemini = modelDef.upstreamModel.startsWith('gemini');
  const defaultMaxOutput = isGemini ? 65536 : 8192;
  const generationConfig: any = {
    temperature: req.temperature ?? 0.7,
    topP: req.top_p ?? 0.95,
    maxOutputTokens: req.max_tokens ? Math.min(req.max_tokens, defaultMaxOutput) : defaultMaxOutput
  };

  // 如果客户端指定了具体的思考 Token 预算，透传给上游
  if (budgetTokens !== undefined && budgetTokens > 0) {
    generationConfig.thinkingConfig = {
      thinkingBudget: budgetTokens
    };
  }

  const innerRequest: any = {
    contents,
    generationConfig
  };

  if (systemInstruction) {
    innerRequest.systemInstruction = systemInstruction;
  }

  if (Array.isArray(req.tools) && req.tools.length) {
    innerRequest.tools = [{
      functionDeclarations: req.tools
        .filter(tool => tool?.type === 'function' && tool.function?.name)
        .map(tool => ({
          name: normalizeToolName(tool.function.name),
          description: tool.function.description || '',
          parameters: sanitizeGoogleSchema(tool.function.parameters || { type: 'object', properties: {} })
        }))
    }];
  }
  if (req.tool_choice && req.tool_choice !== 'auto') {
    innerRequest.toolConfig = {
      functionCallingConfig: {
        mode: req.tool_choice === 'required' ? 'ANY' : 'AUTO',
        ...(req.tool_choice?.function?.name ? { allowedFunctionNames: [normalizeToolName(req.tool_choice.function.name)] } : {})
      }
    };
  }

  const payload: GoogleCloudCodePayload = {
    project: 'aicode-consumers',
    model: modelDef.upstreamModel,
    request: innerRequest
  };

  return {
    payload,
    upstreamModel: modelDef.upstreamModel,
    pool: modelDef.pool
  };
}

/**
 * 创建标准的 OpenAI Chat Completion Chunk
 */
export function createOpenAIChunk(
  id: string,
  model: string,
  contentDelta: string,
  finishReason: string | null = null,
  toolCalls?: Array<{ index: number; id?: string; name: string; arguments: string }>
): string {
  const chunk: OpenAIChatCompletionChunk = {
    id,
    object: 'chat.completion.chunk',
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [
      {
        index: 0,
        delta: contentDelta
          ? { content: contentDelta }
          : toolCalls?.length
            ? { tool_calls: toolCalls.map(call => ({
                index: call.index,
                ...(call.id ? { id: call.id } : {}),
                type: 'function' as const,
                function: { name: call.name, arguments: call.arguments }
              })) }
            : {},
        finish_reason: finishReason
      }
    ]
  };
  return `data: ${JSON.stringify(chunk)}\n\n`;
}

/**
 * 解析 Google 上游返回的单行 SSE 数据，提取文本片段
 */
export function extractTextFromGoogleChunk(rawJson: string): {
  text: string;
  finishReason?: string | null;
  functionCalls?: Array<{ id?: string; name: string; args: any }>;
  usageMetadata?: {
    promptTokenCount?: number;
    candidatesTokenCount?: number;
    totalTokenCount?: number;
    cachedContentTokenCount?: number;
    cacheReadInputTokenCount?: number;
    cacheCreationInputTokenCount?: number;
    cache_read_input_tokens?: number;
    cache_creation_input_tokens?: number;
  };
} {
  try {
    const parsed = JSON.parse(rawJson);
    const usageMetadata = parsed.response?.usageMetadata || parsed.usageMetadata;
    const candidate = parsed.response?.candidates?.[0] || parsed.candidates?.[0];
    if (!candidate) return { text: '', functionCalls: [], usageMetadata };

    const parts = candidate.content?.parts;
    let text = '';
    const functionCalls: Array<{ id?: string; name: string; args: any }> = [];
    if (Array.isArray(parts)) {
      for (const part of parts) {
        // 过滤思维链（thought）部分，只返回最终输出
        if (part.text && !part.thought) {
          text += part.text;
        }
        if (part.functionCall) {
          const sig = part.thoughtSignature || part.thought_signature;
          const rawId = part.functionCall.id;
          rememberThoughtSignature(rawId, sig);
          functionCalls.push({
            // 签名编码进 id 返回给客户端：客户端原样回传即带回签名，
            // 不依赖服务端缓存（重启 / 多实例 / 客户端改写 id 时仍可靠）
            id: signedId(rawId, sig),
            name: part.functionCall.name,
            args: part.functionCall.args || {}
          });
        }
      }
    }

    const finishReason = candidate.finishReason || null;
    return { text, functionCalls, finishReason: finishReason === 'STOP' ? 'stop' : finishReason, usageMetadata };
  } catch {
    return { text: '', functionCalls: [] };
  }
}