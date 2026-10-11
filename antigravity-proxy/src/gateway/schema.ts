/**
 * JSON Schema → Google CloudCode / Gemini proto-schema 清洗（OpenAI 与 Anthropic 两条入口共用）。
 *
 * 为什么需要它：CloudCode 侧（含 claude 模型经 Google 代理到 Anthropic 的链路）只接受
 * 一个受限的 proto 式 schema，不支持 JSON Schema 的组合关键字。把 `anyOf` 原样送上去会得到
 * 上游 400：
 *   tools.0.custom.input_schema: JSON schema is invalid. It must match JSON Schema draft 2020-12
 *
 * 实机复现（2026-10-11）：omp 的 `grep` 工具参数含 `glob: anyOf[string, array<string>]`，
 * 经本代理的 OpenAI 兼容入口请求 `claude-opus-4-6-thinking` 必然 400；
 * 只带 `anyOf` 的最小 schema 同样 400，把 `anyOf` 去掉后同一请求 200。
 *
 * 处理策略：
 *   1. anyOf / oneOf → 取第一个非 null 分支与原字段合并（含 null 分支时置 nullable）；
 *   2. allOf → 分支依次浅合并；
 *   3. type 数组（如 ['string','null']）→ 取非 null 项并置 nullable；
 *   4. 草案 7 的数字型 exclusiveMinimum/Maximum → minimum/maximum；
 *   5. const → enum（proto schema 没有 const）；
 *   6. 缺 type 时按 properties / items 补全；
 *   7. 最后按 proto 允许的字段白名单过滤，并递归清洗 properties / items / required。
 *
 * 两条入口必须共用本实现：`src/gateway/adapter.ts`（/v1/chat/completions）与
 * `src/gateway/anthropic.ts`（/v1/messages）。此前 adapter 侧是另一套"只删固定关键字"的
 * 实现、未处理 anyOf，正是 omp 切到 claude 模型报 400 的原因。
 */

type JsonObject = Record<string, unknown>;

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Google Schema Proto 允许出现的键（其余一律丢弃）。 */
const ALLOWED_SCHEMA_KEYS: Record<string, true> = {
  type: true,
  format: true,
  description: true,
  nullable: true,
  enum: true,
  properties: true,
  required: true,
  items: true,
  title: true,
  maxItems: true,
  minItems: true,
  maxLength: true,
  minLength: true,
  pattern: true,
  maximum: true,
  minimum: true,
  default: true,
};

/** 递归清洗任意 schema 片段（对象、数组或标量）。 */
function sanitizeValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitizeValue);
  if (!isJsonObject(value)) return value;
  return sanitizeObject(value);
}

function sanitizeObject(schema: JsonObject): JsonObject {
  let s: JsonObject = { ...schema };

  // 1. anyOf / oneOf → 取第一个非 null 分支合并
  const union = s.anyOf ?? s.oneOf;
  if (Array.isArray(union)) {
    const branches = union.filter(isJsonObject);
    if (branches.some((branch) => branch.type === 'null' || branch.type === 'NULL')) s.nullable = true;
    const nonNull = branches.find((branch) => branch.type !== 'null' && branch.type !== 'NULL');
    delete s.anyOf;
    delete s.oneOf;
    if (nonNull) s = { ...s, ...nonNull };
  }

  // 2. allOf → 分支依次浅合并
  if (Array.isArray(s.allOf)) {
    const branches = s.allOf.filter(isJsonObject);
    delete s.allOf;
    for (const branch of branches) {
      s = { ...s, ...branch };
    }
  }

  // 3. type 数组（['string','null']）→ 单个类型 + nullable
  if (Array.isArray(s.type)) {
    const types = s.type.filter((t): t is string => typeof t === 'string');
    if (types.includes('null')) s.nullable = true;
    s.type = types.find((t) => t !== 'null') ?? 'string';
  }

  // 4. 草案 7 的数字型 exclusiveMinimum / exclusiveMaximum → minimum / maximum
  if (typeof s.exclusiveMinimum === 'number') s.minimum = s.exclusiveMinimum;
  if (typeof s.exclusiveMaximum === 'number') s.maximum = s.exclusiveMaximum;

  // 5. const → enum
  if (s.const !== undefined) s.enum = [s.const];

  // 6. 补全缺失的 type
  if (s.type === undefined || s.type === null) {
    if (s.properties) s.type = 'object';
    else if (s.items) s.type = 'array';
    else s.type = 'object';
  }

  // 7. 白名单过滤 + 递归清洗
  const result: JsonObject = {};
  for (const [key, value] of Object.entries(s)) {
    if (!ALLOWED_SCHEMA_KEYS[key]) continue;

    if (key === 'properties' && isJsonObject(value)) {
      const cleanProps: JsonObject = {};
      for (const [propKey, propValue] of Object.entries(value)) {
        cleanProps[propKey] = sanitizeValue(propValue);
      }
      result.properties = cleanProps;
    } else if (key === 'items') {
      result.items = sanitizeValue(value);
    } else if (key === 'required' && Array.isArray(value)) {
      result.required = value.filter((entry): entry is string => typeof entry === 'string');
    } else {
      result[key] = value;
    }
  }

  if (result.type === 'object' && !result.properties) {
    result.properties = {};
  }

  return result;
}

/**
 * 清洗一个工具参数 schema。非对象输入（理论上不会出现）退化为不约束的 `{}`，
 * 随后被补全为 `{ type: 'object', properties: {} }`。
 */
export function sanitizeGoogleSchema(schema: unknown): JsonObject {
  if (!isJsonObject(schema)) return { type: 'object', properties: {} };
  return sanitizeObject(schema);
}
