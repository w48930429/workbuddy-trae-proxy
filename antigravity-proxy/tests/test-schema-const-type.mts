import { sanitizeGoogleSchema } from '../src/gateway/schema.js';

let passed = 0;
let failed = 0;

function assert(condition: boolean, message: string) {
  if (condition) {
    passed++;
    console.log('  ✅ ' + message);
  } else {
    failed++;
    console.error('  ❌ ' + message);
  }
}

console.log('🧪 sanitizeGoogleSchema const→enum type regression tests\n');

// 1. const 标量约束应保留对应标量 type，而不是退化成 object
{
  const params = sanitizeGoogleSchema({
    type: 'object',
    properties: { language: { const: 'js' } },
    required: ['language']
  });
  const language = params.properties.language;
  assert(language.type === 'string', 'const string 约束生成 type=string');
  assert(JSON.stringify(language.enum) === '["js"]', 'const string 约束生成 enum=["js"]');
  assert(language.type !== 'object', 'const string 约束不退化成 object');
  assert(language.properties === undefined, 'const string 约束不产生空 properties');
}

// 2. const 字符串约束生成合法 string enum schema
{
  const params = sanitizeGoogleSchema({
    type: 'object',
    properties: { op: { const: 'view' } }
  });
  assert(params.properties.op.type === 'string', 'const string 约束生成 type=string');
  assert(JSON.stringify(params.properties.op.enum) === '["view"]', 'const string 约束生成 enum=["view"]');
}

// 3. const 数字 / 布尔约束生成对应类型
{
  const numberParams = sanitizeGoogleSchema({
    type: 'object',
    properties: { timeout: { const: 30 } }
  });
  assert(numberParams.properties.timeout.type === 'number', 'const number 约束生成 type=number');
  assert(JSON.stringify(numberParams.properties.timeout.enum) === '[30]', 'const number 约束生成 enum=[30]');

  const boolParams = sanitizeGoogleSchema({
    type: 'object',
    properties: { pty: { const: true } }
  });
  assert(boolParams.properties.pty.type === 'boolean', 'const boolean 约束生成 type=boolean');
  assert(JSON.stringify(boolParams.properties.pty.enum) === '[true]', 'const boolean 约束生成 enum=[true]');
}

// 4. const 对象约束保留 object 类型，不产生错误枚举
{
  const params = sanitizeGoogleSchema({
    type: 'object',
    properties: {
      ready: { const: { type: 'object', properties: {} } }
    }
  });
  assert(params.properties.ready.type === 'object', 'const object 约束保留 type=object');
  assert(params.properties.ready.enum === undefined, 'const object 约束不产生错误 enum');
}

console.log(`\n结果: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
