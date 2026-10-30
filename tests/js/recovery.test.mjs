/*
 * 恢复状态机的纯逻辑测试（无第三方依赖）。
 *
 *   node tests/js/recovery.test.mjs
 *
 * 用 node 而不是浏览器，是因为这里断言的都是「刷新后该做什么」的判定，
 * 不涉及 DOM；DOM 部分由 tests/test_demo_recovery.py 的接口契约测试兜底。
 */
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const recovery = require("../../app/demo/static/recovery.js");

let passed = 0;
const failures = [];

function check(name, condition, detail) {
  if (condition) {
    passed += 1;
    return;
  }
  failures.push(`${name}${detail ? ` — ${detail}` : ""}`);
}

function equal(name, actual, expected) {
  check(name, Object.is(actual, expected) || JSON.stringify(actual) === JSON.stringify(expected),
    `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

/* 最小 sessionStorage 替身：只需要 get/set/remove。 */
function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: key => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: key => data.delete(key),
    dump: () => Object.fromEntries(data),
  };
}

const sample = {
  request_id: "d73028805c79",
  conversation_id: "3d41787d-f9a5-4d99-b71b-57203037f6bd",
  question: "统计2026年销售额最高的10个商品",
  database_id: "auto",
  created_at: "2026-10-29T07:53:28.000Z",
};

// 1. 提交成功后保存 request_id
{
  const storage = memoryStorage();
  const saved = recovery.writeRecord(storage, sample);
  equal("1. 保存后回读到同一 request_id", recovery.readRecord(storage).request_id, sample.request_id);
  equal("1. 写入返回规范化记录", saved.request_id, sample.request_id);
}

// 2. 刷新后读取原任务（新 storage 实例模拟新页面）
{
  const storage = memoryStorage({ [recovery.STORAGE_KEY]: JSON.stringify(sample) });
  const record = recovery.readRecord(storage);
  equal("2. 刷新后仍能读出原任务", record.question, sample.question);
  equal("2. 刷新后保留原 conversation_id", record.conversation_id, sample.conversation_id);
}

// 3+4. running 不重复提交，但会重连事件流
{
  const plan = recovery.planForStatus("running");
  equal("3. running 的动作是恢复事件流", plan.action, recovery.ACTIONS.resume_events);
  equal("3. running 期间禁止新提交", plan.canSubmit, false);
  equal("4. running 期间保持忙碌态", plan.busy, true);
  check("4. running 提示已恢复进度连接", plan.message.includes("已恢复进度连接"), plan.message);
}

// queued 与 running 同路径，但提示不同
{
  const plan = recovery.planForStatus("queued");
  equal("queued 的动作是恢复事件流", plan.action, recovery.ACTIONS.resume_events);
  check("queued 提示仍在排队", plan.headline.includes("排队"), plan.headline);
}

// 5. success 恢复完整结果（表格与图表由 chat.js 复用同一渲染函数）
{
  const plan = recovery.planForStatus("success");
  equal("5. success 的动作是渲染结果", plan.action, recovery.ACTIONS.render_result);
  equal("5. success 之后可以继续提问", plan.canSubmit, true);
  equal("5. success 不清理记录", plan.clearRecord, false);
  equal("5. empty_result 同样渲染结果", recovery.planForStatus("empty_result").action,
    recovery.ACTIONS.render_result);
}

// 6. error 恢复失败信息
{
  const plan = recovery.planForStatus("error");
  equal("6. error 的动作是渲染错误", plan.action, recovery.ACTIONS.render_error);
  equal("6. error 之后允许重新提交", plan.canSubmit, true);
}

// 7. interrupted 给出明确提示并清理记录
{
  const plan = recovery.planForStatus("interrupted");
  equal("7. interrupted 的动作是中断提示", plan.action, recovery.ACTIONS.interrupted);
  check("7. interrupted 明确说明后端重启", plan.message.includes("重启"), plan.message);
  check("7. interrupted 要求重新提交", plan.message.includes("重新提交"), plan.message);
  equal("7. interrupted 清理活动任务标识", plan.clearRecord, true);
}

// 8. 404 / 未知状态清理失效记录
{
  const plan = recovery.planForStatus(undefined);
  equal("8. 未知状态判定为失效", plan.action, recovery.ACTIONS.discard);
  equal("8. 失效记录需要清理", plan.clearRecord, true);
  equal("8. 失效时不自动提交", plan.canSubmit, true);
  equal("8. 非终态非活跃的状态也按失效处理", recovery.planForStatus("weird").action,
    recovery.ACTIONS.discard);
}

// 9. 新建对话清理记录
{
  const storage = memoryStorage();
  recovery.writeRecord(storage, sample);
  recovery.clearRecord(storage);
  equal("9. 新建对话后记录被清空", recovery.readRecord(storage), null);
}

// 10. 恢复后继续追问沿用原 conversation_id
{
  const storage = memoryStorage({ [recovery.CONVERSATION_KEY]: sample.conversation_id });
  equal("10. 会话 ID 跨刷新保持一致", recovery.readConversationId(storage), sample.conversation_id);
  recovery.writeConversationId(storage, "conversation-2");
  equal("10. 新建对话才更换会话 ID", recovery.readConversationId(storage), "conversation-2");
}

// 记录校验：损坏 / 缺字段 / 非对象一律拒绝
{
  equal("校验：数组被拒绝", recovery.sanitizeRecord([]), null);
  equal("校验：字符串被拒绝", recovery.sanitizeRecord("deepeye"), null);
  equal("校验：缺少 request_id 被拒绝", recovery.sanitizeRecord({ conversation_id: "c" }), null);
  equal("校验：request_id 含非法字符被拒绝",
    recovery.sanitizeRecord({ ...sample, request_id: "../../etc/passwd" }), null);
  equal("校验：缺少 conversation_id 被拒绝",
    recovery.sanitizeRecord({ request_id: "abcd1234" }), null);
}

// 11. 新标签页：sessionStorage 是空的，只能把服务端任务翻译成记录
{
  const task = {
    id: "a1b2c3d4e5f6",
    status: "running",
    created_at: "2026-10-29T08:10:00+00:00",
    request: {
      message: "统计2026年销售额最高的10个商品",
      database_id: "enterprise_sales",
      conversation_id: "conv-from-server",
    },
    result: null,
    events: [{ sequence: 1 }],
  };
  const record = recovery.recordFromTask(task);
  equal("11. 服务端任务可翻译成本地记录", record.request_id, task.id);
  equal("11. 沿用服务端会话 ID", record.conversation_id, "conv-from-server");
  equal("11. 沿用服务端问题文本", record.question, task.request.message);
  equal("11. 沿用服务端数据源", record.database_id, "enterprise_sales");
  equal("11. 游标从头开始", record.sequence, 0);
  // 事件序列与结果不进本地记录
  check("11. 不落事件序列", !JSON.stringify(record).includes("events"));
  check("11. 不落结果", !JSON.stringify(record).includes("result"));
  equal("11. 缺少会话 ID 时用当前标签页的兜底", recovery.recordFromTask(
    { ...task, request: { ...task.request, conversation_id: undefined } }, "conv-local").conversation_id,
    "conv-local");
  equal("11. 服务端没有任务时返回 null", recovery.recordFromTask(null), null);
  equal("11. 非法任务体返回 null", recovery.recordFromTask("nope"), null);
}

// 只保留白名单字段：误存的密钥/路径/响应体不能落盘
{
  const storage = memoryStorage();
  recovery.writeRecord(storage, {
    ...sample,
    api_key: "sk-should-not-persist",
    artifacts_dir: "E:\\secret\\path",
    result: { rows: [[1, 2, 3]] },
  });
  const raw = JSON.stringify(storage.dump());
  check("脱敏：不写入 api_key", !raw.includes("sk-should-not-persist"), raw);
  check("脱敏：不写入本地路径", !raw.includes("E:\\\\secret"), raw);
  check("脱敏：不写入查询结果", !raw.includes("rows"), raw);
  equal("脱敏：未知键被丢弃", Object.keys(recovery.readRecord(storage)).sort().join(","),
    "conversation_id,created_at,database_id,question,request_id,sequence");
}

// 断点续传游标：非法 sequence 归零
{
  equal("游标：负数归零", recovery.sanitizeRecord({ ...sample, sequence: -5 }).sequence, 0);
  equal("游标：非整数归零", recovery.sanitizeRecord({ ...sample, sequence: 1.5 }).sequence, 0);
  equal("游标：合法值保留", recovery.sanitizeRecord({ ...sample, sequence: 7 }).sequence, 7);
}

// 存储被禁用（隐私模式）时不能抛异常
{
  const broken = {
    getItem() { throw new Error("denied"); },
    setItem() { throw new Error("denied"); },
    removeItem() { throw new Error("denied"); },
  };
  equal("降级：读取失败返回 null", recovery.readRecord(broken), null);
  equal("降级：写入失败返回 null", recovery.writeRecord(broken, sample), null);
  recovery.clearRecord(broken);
  equal("降级：会话 ID 读取失败返回空串", recovery.readConversationId(broken), "");
  passed += 1;
}

// 重连上限
{
  equal("重连：未达上限继续", recovery.shouldReconnect(7, 8), true);
  equal("重连：达到上限停止", recovery.shouldReconnect(8, 8), false);
}

if (failures.length) {
  console.error(`FAIL ${failures.length} / ${failures.length + passed}`);
  failures.forEach(item => console.error(`  - ${item}`));
  process.exit(1);
}
console.log(`ok ${passed} assertions`);
