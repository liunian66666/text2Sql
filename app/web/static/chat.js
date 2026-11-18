/* DeepEye Data Copilot 前端逻辑（原生 JS，无框架）。
 *
 * 结构：
 *   1. 常量与格式化工具
 *   2. 数据源 / 系统状态 / 推荐问题
 *   3. 历史会话（搜索 / 删除 / 切换）
 *   4. 欢迎页与消息渲染
 *   5. 提交、SSE 跟踪、Agent 时间轴
 *   6. 结果渲染（状态判定 / KPI / 图表 / 数据明细 / SQL / 执行详情）
 *   7. GraphRAG 知识证据面板
 *   8. 知识库页面 / 评测工作台
 *   9. 恢复与初始化（依赖 recovery.js，逻辑未改动）
 */
const STAGE_ORDER = [
  ["queue", "任务排队"],
  ["agent_orchestration", "规划与编排"],
  ["agent_planning", "LLM 规划"],
  ["knowledge_retrieval", "知识检索 / GraphRAG"],
  ["prepare", "请求准备"],
  ["value_retrieval", "Value Retrieval"],
  ["few_shot_preparation", "Few-shot 准备"],
  ["schema_linking", "Schema Linking"],
  ["sql_generation", "SQL 生成"],
  ["sql_revision", "SQL 修订"],
  ["sql_selection", "SQL 选择"],
  ["execution", "SQLite 执行"],
];
const STAGE_LABELS = Object.fromEntries(STAGE_ORDER);
const INTENT_NAMES = {query: "查数", ranking: "排名", trend: "趋势", proportion: "占比", chat: "闲聊"};
const VARIANT_NAMES = {generation_only: "仅生成", generation_revision: "生成＋修订", full_pipeline: "完整流程"};
const MAX_TABLE_ROWS_COLLAPSED = 30;

const $ = selector => document.querySelector(selector);
const Recovery = window.DeepEyeRecovery;

const formatNumber = value => {
  if (value == null) return "—";
  if (typeof value === "string") return value; // 已格式化的比值（如 0/51）原样展示。
  return new Intl.NumberFormat("zh-CN").format(value);
};
const formatSeconds = value => value == null ? "未知"
  : `${Number(value).toFixed(Number(value) < 10 ? 2 : 1)} 秒`;
const formatPercent = value => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const valueText = value => typeof value === "object" ? JSON.stringify(value) : String(value ?? "");
const isBlankValue = value => value === null || value === undefined || value === "";
const escapeHtml = text => String(text)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#39;");

let databases = [];
let systemStatus = null;
let activeRun = null;
let activeSource = null;
let timer = null;
let reconnects = 0;
let recovering = false;
let kbCache = null;
let kbActiveTab = "metrics";
// 刷新后必须复用同一个会话 ID，否则「做成柱状图」会掉到新会话里读不到上一轮结果。
let conversationId = Recovery.readConversationId(sessionStorage) || newConversationId();
Recovery.writeConversationId(sessionStorage, conversationId);

function newConversationId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : `conv-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/* ==================================================================== */
/* 1. 格式化：数值列语义识别（只做 UI 层展示格式化，不改原始结果）          */
/* ==================================================================== */
const PERCENT_COLUMN = /率|占比|比例|percent|rate|ratio|margin|share/i;
const MONEY_COLUMN = /amount|subtotal|total_due|freight|revenue|sales|cost|price|fee|list_price|standard_cost|金额|额$|费用|成本/i;

function columnKind(column, rows, columnIndex) {
  const values = rows.map(row => row[columnIndex]).filter(value => !isBlankValue(value));
  if (!values.length || !values.every(value => typeof value === "number" || (typeof value === "string" && value !== "" && !isNaN(Number(value))))) return "text";
  if (PERCENT_COLUMN.test(column)) return "percent";
  if (MONEY_COLUMN.test(column)) return "money";
  if (values.every(value => Number.isInteger(Number(value)))) return "integer";
  return "number";
}

function formatCellValue(value, kind) {
  if (isBlankValue(value)) return "—";
  const num = Number(value);
  if (kind === "text" || Number.isNaN(num)) return String(value);
  if (kind === "percent") {
    // 仅当列语义明确是比率且取值像小数比例时才换算成百分比；>1.5 视为已是百分数值。
    if (Math.abs(num) <= 1.5) return `${(num * 100).toFixed(2)}%`;
    return `${num.toFixed(2)}%`;
  }
  if (kind === "money") return new Intl.NumberFormat("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(num);
  if (kind === "integer") return formatNumber(num);
  return new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(num);
}

/* ==================================================================== */
/* 2. 数据源 / 系统状态                                                  */
/* ==================================================================== */
function setService(text, state = "neutral") {
  const element = $("#service-state");
  element.lastChild.textContent = text;
  element.className = `status-pill ${state}`;
}

function selectedDatabase() {
  return databases.find(item => item.id === $("#database").value);
}

function statusForDatabase(databaseId) {
  return (systemStatus?.databases || []).find(item => item.id === databaseId) || null;
}

function optionalDatabaseInfo(databaseId) {
  return (systemStatus?.optional_databases || []).find(item => item.id === databaseId) || null;
}

async function loadDatabases() {
  const [databasesResponse, statusResponse] = await Promise.all([
    fetch("/api/databases"),
    fetch("/api/system/status").catch(() => null),
  ]);
  if (!databasesResponse.ok) throw new Error("无法读取数据库列表");
  databases = (await databasesResponse.json()).databases;
  if (statusResponse && statusResponse.ok) systemStatus = await statusResponse.json();
  const select = $("#database");
  select.replaceChildren(
    new Option("自动识别业务域（推荐）", "auto"),
    ...databases.map(db => new Option(db.name, db.id)),
  );
  updateDatabaseUI();
}

function sourceBadge(item) {
  const status = statusForDatabase(item.id);
  const badge = document.createElement("span");
  badge.className = `domain-pill ${item.domain || "other"}`;
  const dot = document.createElement("i");
  let state = "off";
  if (status) {
    if (status.database_ready && status.knowledge_artifacts) state = "ok";
    else state = "warn";
  }
  badge.classList.add(state);
  badge.title = state === "ok" ? "数据库与知识产物均就绪"
    : state === "warn" ? "数据库已注册，但部分产物未就绪" : "状态未知";
  badge.append(dot, document.createTextNode(`${item.domain_name || item.name} · ${item.table_count ?? "?"} 表`));
  return badge;
}

function updateDatabaseUI() {
  const db = selectedDatabase();
  const summary = $("#source-summary");
  summary.replaceChildren();
  if (db) {
    $("#conversation-title").textContent = db.domain === "hr" ? "HR 人力数据分析" : "企业数据分析工作台";
    $("#database-description").textContent = `已手动锁定 ${db.name}，自动路由不会覆盖`;
    $("#question").placeholder = db.domain === "hr"
      ? "例如：最近一年各部门离职率是多少？"
      : "例如：按月统计 2026 年销售额趋势";
    summary.appendChild(sourceBadge(db));
    const detail = document.createElement("p");
    detail.textContent = `${db.description || "只读分析数据源"}`;
    summary.appendChild(detail);
  } else {
    $("#conversation-title").textContent = "跨业务域数据分析";
    $("#database-description").textContent = "Agent 将先识别业务域，再检索对应指标与 Schema";
    $("#question").placeholder = "可提问销售或 HR 数据，例如：各部门在职人数是多少？";
    databases.forEach(item => summary.appendChild(sourceBadge(item)));
  }
  // AdventureWorks 真实启用状态：未启用 / 已启用但产物未就绪 / 就绪，绝不写死。
  const aw = optionalDatabaseInfo("adventureworks");
  if (aw && !aw.registered) {
    const note = document.createElement("p");
    note.className = "source-aw-note";
    note.textContent = aw.enabled
      ? `AdventureWorks（${aw.table_count} 表 / ${aw.column_count} 字段）已启用但未完成注册：数据文件${aw.file_present ? "存在" : "缺失"}。`
      : `AdventureWorks（${aw.table_count} 表 / ${aw.column_count} 字段）未启用：${aw.enable_hint}。`;
    summary.appendChild(note);
  } else if (aw && aw.registered) {
    const status = statusForDatabase("adventureworks");
    const note = document.createElement("p");
    note.className = "source-aw-note";
    note.textContent = status
      ? `AdventureWorks 已注册（71 表 / 486 字段）${status.knowledge_artifacts ? "，知识产物就绪" : "，知识产物缺失"}。`
      : "AdventureWorks 已注册。";
    summary.appendChild(note);
  }
  $("#composer-source").textContent = `当前数据源：${db ? db.name : "自动识别业务域"}`;
  renderExamples();
  renderWelcome();
}

function currentExamples() {
  const selected = selectedDatabase();
  if (selected) return selected.examples || [];
  return databases.flatMap(db => (db.examples || []).slice(0, 3)
    .map(example => ({...example, domain_name: db.domain_name})));
}

function renderExamples() {
  const list = $("#example-list");
  list.replaceChildren();
  currentExamples().forEach(example => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "example-button";
    const tag = document.createElement("span");
    tag.textContent = `${example.domain_name || "业务"} · ${example.difficulty || "示例"}`;
    button.append(tag, document.createTextNode(example.question));
    button.addEventListener("click", () => askExample(example));
    list.appendChild(button);
  });
}

/* ==================================================================== */
/* 3. 历史会话                                                           */
/* ==================================================================== */
let latestConversations = [];

async function loadConversations() {
  const list = $("#conversation-list");
  if (!list) return;
  try {
    const response = await fetch("/api/conversations?limit=30");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    latestConversations = (await response.json()).conversations || [];
    renderConversationList(latestConversations);
  } catch (error) {
    list.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = "历史会话暂时无法读取。";
    list.appendChild(empty);
  }
}

function renderConversationList(conversations) {
  const list = $("#conversation-list");
  const keyword = ($("#conversation-search").value || "").trim().toLowerCase();
  list.replaceChildren();
  const filtered = conversations.filter(item => !keyword
    || (item.title || "").toLowerCase().includes(keyword)
    || (item.last_question || "").toLowerCase().includes(keyword));
  if (!conversations.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = "完成一次分析后，会话会保存在本地状态库中。";
    list.appendChild(empty);
    return;
  }
  if (!filtered.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = "没有匹配的会话。";
    list.appendChild(empty);
    return;
  }
  filtered.forEach(conversation => {
    const item = document.createElement("div");
    item.setAttribute("role", "button");
    item.tabIndex = 0;
    item.className = `conversation-item${conversation.id === conversationId ? " active" : ""}`;
    const title = document.createElement("strong");
    title.textContent = conversation.title || conversation.last_question || "未命名对话";
    title.title = title.textContent;
    const meta = document.createElement("small");
    const date = conversation.updated_at ? new Date(conversation.updated_at).toLocaleString("zh-CN") : "";
    meta.textContent = `${conversation.task_count || 0} 轮 · ${date}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "conversation-delete";
    remove.title = "删除会话";
    remove.textContent = "×";
    remove.addEventListener("click", event => {
      event.stopPropagation();
      deleteConversation(conversation.id);
    });
    item.append(title, meta, remove);
    item.addEventListener("click", () => openConversation(conversation.id));
    item.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") openConversation(conversation.id);
    });
    list.appendChild(item);
  });
}

async function openConversation(id) {
  if (activeRun || recovering) return;
  const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`);
  if (!response.ok) return setService("会话读取失败", "error");
  const conversation = await response.json();
  conversationId = conversation.id;
  Recovery.writeConversationId(sessionStorage, conversationId);
  Recovery.clearRecord(sessionStorage);
  const select = $("#database");
  const requested = conversation.requested_database_id || "auto";
  if ([...select.options].some(option => option.value === requested)) select.value = requested;
  updateDatabaseUI();
  const activeTask = (conversation.tasks || []).find(task => Recovery.isActiveStatus(task.status));
  if (activeTask) {
    const record = Recovery.recordFromTask(activeTask, conversationId);
    if (record) Recovery.writeRecord(sessionStorage, record);
    await restoreActiveTask();
    await loadConversations();
    return;
  }
  $("#messages").replaceChildren();
  (conversation.tasks || []).forEach(task => {
    appendUserMessage(task.request?.message || task.request?.question || "（历史问题）");
    const run = appendProgressMessage();
    run.requestId = task.id;
    renderAssistantResult(run, task, false);
  });
  if (!(conversation.tasks || []).length) renderWelcome();
  $("#conversation-title").textContent = conversation.title || "历史对话";
  setService("历史会话已恢复", "success");
  await loadConversations();
}

async function deleteConversation(id) {
  if (activeRun || recovering) return;
  const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {method: "DELETE"});
  if (!response.ok && response.status !== 404) return setService("删除失败", "error");
  if (id === conversationId) {
    startNewConversation();
    renderWelcome();
  }
  await loadConversations();
}

/* ==================================================================== */
/* 4. Markdown 渲染（DOM 节点构建，天然防 XSS）                           */
/* ==================================================================== */
function renderCodeSpans(node, text) {
  const pattern = /`[^`\n]+`/g;
  let cursor = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) node.appendChild(document.createTextNode(text.slice(cursor, match.index)));
    const code = document.createElement("code");
    code.textContent = match[0].slice(1, -1);
    node.appendChild(code);
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) node.appendChild(document.createTextNode(text.slice(cursor)));
  return node;
}

function renderInlineMarkdown(node, text) {
  // 粗体用非贪婪匹配，允许内部夹行内代码；刻意不支持斜体，避免咬掉乘法公式。
  const pattern = /\*\*(.+?)\*\*/g;
  let cursor = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) renderCodeSpans(node, text.slice(cursor, match.index));
    const strong = document.createElement("strong");
    renderCodeSpans(strong, match[1]);
    node.appendChild(strong);
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) renderCodeSpans(node, text.slice(cursor));
  return node;
}

function renderMarkdown(container, source) {
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const cells = line => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(item => item.trim());
  const isDivider = line => /^\|?[\s:|-]+\|?$/.test(line.trim()) && line.includes("-");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index].trim();
    if (!line) { index += 1; continue; }

    if (/^#{1,6}\s/.test(line)) {
      const heading = document.createElement("div");
      heading.className = "md-heading";
      renderInlineMarkdown(heading, line.replace(/^#{1,6}\s+/, ""));
      container.appendChild(heading);
      index += 1;
      continue;
    }

    if (line.startsWith("|") && isDivider(lines[index + 1] || "")) {
      const table = document.createElement("table");
      table.className = "md-table";
      const head = document.createElement("thead");
      const headRow = document.createElement("tr");
      cells(line).forEach(cell => { const th = document.createElement("th"); renderInlineMarkdown(th, cell); headRow.appendChild(th); });
      head.appendChild(headRow);
      table.appendChild(head);
      const body = document.createElement("tbody");
      index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        const row = document.createElement("tr");
        cells(lines[index]).forEach(cell => { const td = document.createElement("td"); renderInlineMarkdown(td, cell); row.appendChild(td); });
        body.appendChild(row);
        index += 1;
      }
      table.appendChild(body);
      container.appendChild(table);
      continue;
    }

    if (/^([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\d+\./.test(line);
      const list = document.createElement(ordered ? "ol" : "ul");
      list.className = "md-list";
      while (index < lines.length && /^([-*+]|\d+\.)\s+/.test(lines[index].trim())) {
        const item = document.createElement("li");
        renderInlineMarkdown(item, lines[index].trim().replace(/^([-*+]|\d+\.)\s+/, ""));
        list.appendChild(item);
        index += 1;
      }
      container.appendChild(list);
      continue;
    }

    const paragraph = document.createElement("div");
    paragraph.className = "md-para";
    while (index < lines.length && lines[index].trim() && !/^#{1,6}\s/.test(lines[index].trim())
      && !lines[index].trim().startsWith("|") && !/^([-*+]|\d+\.)\s+/.test(lines[index].trim())) {
      if (paragraph.childNodes.length) paragraph.appendChild(document.createElement("br"));
      renderInlineMarkdown(paragraph, lines[index].trim());
      index += 1;
    }
    container.appendChild(paragraph);
  }
  return container;
}

/* ==================================================================== */
/* 5. 欢迎页与消息                                                       */
/* ==================================================================== */
function renderWelcome() {
  const db = selectedDatabase();
  const messages = $("#messages");
  if (!messages || activeRun || recovering) return;
  messages.replaceChildren();
  const row = document.createElement("article");
  row.className = "message-row assistant welcome-message";
  const content = document.createElement("div");
  content.className = "message-content";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = "D";
  const bubble = document.createElement("div");
  bubble.className = "bubble assistant-bubble welcome-bubble";
  const title = document.createElement("h2");
  title.className = "welcome-title";
  title.textContent = "今天想分析什么？";
  const sub = document.createElement("p");
  sub.className = "welcome-sub";
  sub.textContent = "基于企业业务知识、数据库关系和 AI 查询规划，获取可追溯的经营分析结果。"
    + (db ? ` 当前数据源：${db.name}。` : " 当前为自动业务域路由。");
  const grid = document.createElement("div");
  grid.className = "capability-grid";
  const domains = document.createElement("div");
  domains.className = "welcome-domains";
  bubble.append(title, sub, grid, domains);
  content.append(avatar, bubble);
  row.appendChild(content);
  messages.appendChild(row);

  // 四个能力入口，示例一律来自当前数据源的真实题单。
  const capabilities = [
    {name: "自然语言查数", desc: "用一句话查询库内任意业务指标", match: ex => (ex.intent || "query") === "query"},
    {name: "销售与经营分析", desc: "销售额、订单、退款等经营口径分析", match: ex => /销售|经营|订单|金额|额/.test(ex.question)},
    {name: "多表关联分析", desc: "跨表 JOIN 与必要中间表补全", match: ex => /中间表|关联|join|跨表/i.test(`${ex.difficulty || ""}${ex.question}`)},
    {name: "趋势与排名分析", desc: "时间趋势、分组排名与占比", match: ex => ["trend", "ranking", "proportion"].includes(ex.intent)},
  ];
  const examples = currentExamples();
  capabilities.forEach((capability, position) => {
    const example = examples.find(capability.match) || examples[position] || examples[0];
    const button = document.createElement("button");
    button.type = "button";
    button.className = "capability";
    button.disabled = !example;
    const strong = document.createElement("strong");
    strong.textContent = capability.name;
    const desc = document.createElement("span");
    desc.textContent = example ? `示例：${example.question}` : capability.desc;
    button.append(strong, desc);
    if (example) button.addEventListener("click", () => askExample(example));
    grid.appendChild(button);
  });

  (db ? [db] : databases).forEach(item => {
    const badge = document.createElement("span");
    badge.className = `domain-pill ${item.domain || "other"}`;
    badge.textContent = `${item.domain_name || item.name} · ${item.table_count ?? "?"} 表`;
    domains.appendChild(badge);
  });
}

function askExample(example) {
  if (activeRun) return;
  $("#question").value = example.question;
  $("#evidence").value = example.evidence || "";
  $("#query-form").requestSubmit();
}

function appendUserMessage(question) {
  const row = document.createElement("article");
  row.className = "message-row user";
  const bubble = document.createElement("div");
  bubble.className = "bubble user-bubble";
  bubble.textContent = question;
  row.appendChild(bubble);
  $("#messages").appendChild(row);
}

function scrollToLatest() {
  const messages = $("#messages");
  requestAnimationFrame(() => messages.scrollTop = messages.scrollHeight);
}

function setBusy(busy) {
  $("#run-button").disabled = busy;
  $("#database").disabled = busy;
  $("#new-chat").disabled = busy;
  document.querySelectorAll("#example-list button, .capability, .suggestion").forEach(button => {
    button.disabled = busy;
  });
}

/* ==================================================================== */
/* 6. 提交 / SSE / Agent 时间轴                                          */
/* ==================================================================== */
function appendProgressMessage() {
  const row = document.createElement("article");
  row.className = "message-row assistant";
  const content = document.createElement("div");
  content.className = "message-content";
  const author = document.createElement("div");
  author.className = "message-author";
  author.textContent = "DeepEye Agent";
  const bubble = document.createElement("div");
  bubble.className = "bubble assistant-bubble";
  const card = document.createElement("div");
  card.className = "thinking-card";
  const head = document.createElement("div");
  head.className = "thinking-head";
  const spinner = document.createElement("span");
  spinner.className = "thinking-spinner";
  const label = document.createElement("strong");
  label.textContent = "模型思考中";
  const elapsed = document.createElement("span");
  elapsed.className = "elapsed-live";
  elapsed.textContent = "0s";
  head.append(spinner, label, elapsed);
  const meta = document.createElement("p");
  meta.className = "request-meta";
  meta.textContent = "正在提交问题…";
  // 可折叠 Agent 分析过程：运行中默认展开，结束后收起摘要。
  const timeline = document.createElement("details");
  timeline.className = "agent-timeline";
  timeline.open = true;
  const timelineSummary = document.createElement("summary");
  timelineSummary.textContent = "Agent 分析过程";
  const timelineProgress = document.createElement("span");
  timelineProgress.className = "timeline-progress";
  timelineProgress.textContent = "0 阶段";
  timelineSummary.appendChild(timelineProgress);
  const timelineList = document.createElement("div");
  timelineList.className = "timeline-list";
  timeline.append(timelineSummary, timelineList);
  card.append(head, meta, timeline);
  bubble.appendChild(card);
  content.appendChild(author);
  content.appendChild(bubble);
  row.appendChild(content);
  $("#messages").appendChild(row);
  scrollToLatest();
  return {
    row, bubble, meta, elapsed,
    timeline, timelineSummary, timelineProgress, timelineList,
    stages: new Map(),          // stageId -> {status, seconds, detail, order}
    stageOrder: [],
    toolCalls: [],
    knowledge: null,
    startedAt: Date.now(),
  };
}

function recordStage(run, stageId, status, extra = {}) {
  if (!run.stages.has(stageId)) {
    run.stageOrder.push(stageId);
    run.stages.set(stageId, {status: "pending", seconds: null, detail: ""});
  }
  const stage = run.stages.get(stageId);
  stage.status = status;
  if (extra.seconds != null) stage.seconds = extra.seconds;
  if (extra.detail) stage.detail = extra.detail;
  renderTimeline(run);
}

function stageDisplayLabel(stageId) {
  return STAGE_LABELS[stageId] || stageId;
}

function renderTimeline(run) {
  const list = run.timelineList;
  list.replaceChildren();
  let done = 0;
  run.stageOrder.forEach(stageId => {
    const stage = run.stages.get(stageId);
    if (stage.status === "completed") done += 1;
    const rowEl = document.createElement("div");
    rowEl.className = `timeline-row ${stage.status}`;
    const dot = document.createElement("span");
    dot.className = "timeline-dot";
    const label = document.createElement("span");
    label.className = "timeline-label";
    label.textContent = stageDisplayLabel(stageId);
    const right = document.createElement("span");
    right.className = "timeline-seconds";
    right.textContent = stage.seconds != null ? formatSeconds(stage.seconds)
      : stage.status === "running" ? "进行中…" : stage.status === "failed" ? "失败" : "";
    rowEl.append(dot, label, right);
    if (stage.detail) {
      const detail = document.createElement("div");
      detail.className = "timeline-detail";
      detail.style.gridColumn = "2 / 4";
      detail.textContent = stage.detail;
      rowEl.appendChild(detail);
    }
    list.appendChild(rowEl);
  });
  run.timelineProgress.textContent = `${done}/${run.stageOrder.length} 阶段`;
}

function startClock(run) {
  clearInterval(timer);
  timer = setInterval(() => run.elapsed.textContent = `${Math.floor((Date.now() - run.startedAt) / 1000)}s`, 1000);
}

async function submitQuery(event) {
  event.preventDefault();
  if (activeRun || recovering) return;
  const question = $("#question").value.trim();
  if (!question) return;
  appendUserMessage(question);
  $("#question").value = "";
  autoGrow();
  const run = appendProgressMessage();
  run.question = question;
  run.databaseId = $("#database").value;
  run.createdAt = new Date().toISOString();
  run.sequence = 0;
  activeRun = run;
  reconnects = 0;
  setBusy(true);
  setService("模型思考中", "running");
  startClock(run);
  recordStage(run, "agent_orchestration", "running");
  const payload = {
    database_id: $("#database").value, conversation_id: conversationId, question,
    user_hint: $("#evidence").value.trim(),
    max_rows: Number($("#max-rows").value),
    sql_timeout_seconds: Number($("#timeout").value),
  };
  try {
    const response = await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "请求提交失败");
    if (data.conversation_id) {
      conversationId = data.conversation_id;
      Recovery.writeConversationId(sessionStorage, conversationId);
    }
    run.requestId = data.request_id;
    run.meta.textContent = `请求 ${data.request_id} 已进入分析队列`;
    recordStage(run, "queue", "completed");
    persistRun(run);
    followEvents(run, 0);
  } catch (error) {
    finishWithTransportError(run, error.message);
  }
}

function persistRun(run) {
  if (!run || !run.requestId) return;
  Recovery.writeRecord(sessionStorage, {
    request_id: run.requestId,
    conversation_id: conversationId,
    question: run.question || "",
    database_id: run.databaseId || $("#database").value,
    created_at: run.createdAt || new Date().toISOString(),
    sequence: run.sequence || 0,
  });
}

const MAX_RECONNECTS = 8;

function closeSource() {
  if (activeSource) {
    activeSource.close();
    activeSource = null;
  }
}

window.addEventListener("pagehide", closeSource);

function followEvents(run, after = 0) {
  closeSource();
  const source = new EventSource(`/api/query/${run.requestId}/events?after=${after}`);
  activeSource = source;
  source.addEventListener("progress", async message => {
    reconnects = 0;
    const event = JSON.parse(message.data);
    if (Number.isInteger(event.sequence)) {
      run.sequence = event.sequence;
      persistRun(run);
    }
    if (event.stage === "queue" && event.status === "running") run.meta.textContent = "Agent 正在理解问题并规划分析流程";
    if (event.stage === "meter" && event.status === "usage") {
      run.meta.textContent = `已调用 ${event.calls} 次 · ${formatNumber(event.total_tokens)} tokens · ${Math.round(event.elapsed_seconds)}s`;
      return;
    }
    // 知识增强事件：结构化 observability 由工具层在 run_text_to_sql 内发出。
    if (event.stage === "knowledge_enhancement") {
      run.knowledge = {enabled: event.enabled, error: event.error, observability: event.observability || null};
      if (event.enabled && event.observability) {
        recordStage(run, "knowledge_retrieval", "completed",
          {seconds: event.observability.total_elapsed_seconds,
           detail: `${event.observability.retrieval_mode} · ${event.observability.context_tokens} tokens`});
      }
    }
    // 工具调用：聚合展示，不伪造阶段。
    if (event.stage === "agent_tool") {
      run.toolCalls.push(`${event.tool || "?"}:${event.status}`);
      const running = event.status === "started";
      recordStage(run, "agent_tool", running ? "running" : "completed",
        {detail: run.toolCalls.slice(-6).join(" · ")});
    }
    // 其余真实阶段事件：只渲染确实发生过的阶段（未知阶段按原始 ID 追加）。
    if (event.stage !== "queue" && event.stage !== "agent_tool"
        && event.stage !== "knowledge_enhancement" && event.stage !== "request") {
      if (event.status === "failed" && event.error) {
        recordStage(run, event.stage, "failed", {detail: event.error});
      } else if (event.status === "started") {
        recordStage(run, event.stage, "running");
      } else {
        recordStage(run, event.stage, event.status, {seconds: event.seconds});
      }
    }
    if (event.stage === "request") {
      closeSource();
      clearInterval(timer);
      await loadQueryResult(run);
    }
  });
  source.onerror = () => {
    closeSource();
    if (!Recovery.shouldReconnect(reconnects, MAX_RECONNECTS)) {
      return finishWithTransportError(run, "事件流多次重连失败，演示服务可能已停止");
    }
    reconnects += 1;
    run.meta.textContent = `连接中断，正在第 ${reconnects} 次重试…`;
    setTimeout(() => loadQueryResult(run), 1200);
  };
}

async function loadQueryResult(run) {
  let task;
  try {
    const response = await fetch(`/api/query/${run.requestId}`);
    if (response.status === 404) {
      Recovery.clearRecord(sessionStorage);
      return finishWithDiscard(run, "任务记录已失效，请重新提交");
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    task = await response.json();
  } catch (error) {
    if (!Recovery.shouldReconnect(reconnects, MAX_RECONNECTS)) {
      return finishWithTransportError(run, error.message);
    }
    reconnects += 1;
    run.meta.textContent = `结果读取失败，正在第 ${reconnects} 次重试…`;
    return setTimeout(() => loadQueryResult(run), 1500);
  }
  if (Recovery.isActiveStatus(task.status)) {
    return setTimeout(() => followEvents(run, run.sequence || 0), 1200);
  }
  renderAssistantResult(run, task);
}

/* ==================================================================== */
/* 7. 结束状态（成功 / 部分完成 / 预算 / 失败 / 中断）                     */
/* ==================================================================== */
/*
 * 只根据后端真实字段推导终止状态，不虚构：
 *  - success/empty_result 且回答是「步骤过多已安全停止」→ 部分完成（已有查询结果，
 *    但后续总结/可视化因达到工具轮数上限未执行）；
 *  - error 且 error 含「预算」→ 达到 Token/时间预算；
 *  - error 且 failed_stage=agent → 达到步骤上限（没有查询结果）；
 *  - error 其余 → 查询失败；
 *  - interrupted → 服务重启导致任务中断。
 */
function deriveOutcome(task) {
  const result = task.result || {};
  const status = task.status || result.status;
  const answer = result.answer || "";
  const error = result.error || "";
  const hasData = (result.rows || []).length > 0;
  const stopped = /步骤过多|安全停止/.test(answer);
  if (status === "interrupted") {
    return {kind: "interrupted", tone: "error", title: "任务已中断",
      note: "后端在任务执行期间重启，本次任务未完成。请重新提交。",
      showRetry: true};
  }
  if (status === "partial") {
    // 后端显式标记 partial：拿到了部分真实结果但没跑完全部要求。
    // 不能再显示「分析完成」——这正是上一轮 143 秒只拿到日期范围却报成功的缺陷。
    const gaps = Object.values(result.capability_gap || {});
    // 四类终止原因各有明确文案：只写 tool_limit 会让另外三种（业务 SQL 上限、
    // 探查上限、能力缺口）退化成笼统的「未完成全部要求」。
    const reasonLabels = {
      tool_limit: "已达到工具调用轮数上限",
      sql_budget_limit: "本轮业务查询次数已达上限",
      probe_limit: "本轮数据库探查次数已达上限",
      capability_gap: "所选数据源缺少所需的数据能力",
    };
    const reason = reasonLabels[result.termination_reason]
      || result.error || "未完成全部要求";
    const probeOnly = !hasData && (result.probe_results || []).length > 0;
    return {kind: "partial", tone: "partial", title: "部分完成 · 未跑完全部分析",
      note: `${reason}。${probeOnly
        ? "本次仅完成了数据库事实探查（如日期覆盖），没有产生业务查询结果，不能作为经营分析结论。"
        : "下方展示的是真实执行到的部分结果。"}${gaps.length ? ` 数据能力缺失：${gaps.join("；")}` : ""}`,
      showRetry: true};
  }
  if (["success", "empty_result"].includes(status)) {
    if (stopped && result.failed_stage === "agent") {
      return {kind: "partial", tone: "partial", title: "部分完成",
        note: "Agent 已达到工具调用轮数上限，后续总结未执行；下方查询结果、SQL 与执行过程真实有效。请缩小问题范围后重试。",
        showRetry: true};
    }
    if (stopped) {
      return {kind: "partial", tone: "partial", title: "部分完成",
        note: hasData
          ? "已获得真实查询结果，但后续总结因达到步骤上限未执行。"
          : "本轮未产生查询结果即达到步骤上限。请缩小问题范围后重试。",
        showRetry: true};
    }
    return {kind: "success", tone: "success",
      title: status === "empty_result" ? "查询完成（空结果）" : "分析成功", showRetry: false};
  }
  if (status === "error") {
    if (/预算|Budget/i.test(error)) {
      return {kind: "budget", tone: "partial", title: "达到 Token / 时间预算",
        note: `${error} 请缩小问题范围或降低 max_rows 后重试。`, showRetry: true};
    }
    if (result.failed_stage === "agent" || /上限|步骤过多/.test(`${error} ${answer}`)) {
      return {kind: "step_limit", tone: "partial", title: "部分终止 · 达到工具调用轮数上限",
        note: "Agent 在本轮已达到最大工具调用轮数（8 轮）并安全停止，未生成可用的 SQL 结果。请缩小问题范围后重试。",
        showRetry: true};
    }
    if (hasData || result.sql) {
      return {kind: "partial", tone: "partial", title: "部分完成",
        note: "已获得查询结果，但后续步骤未完成。", showRetry: true};
    }
    return {kind: "failed", tone: "error", title: "查询失败",
      note: result.error || `失败阶段：${result.failed_stage || "unknown"}`, showRetry: true};
  }
  return {kind: "unknown", tone: "error", title: "状态未知", note: "任务状态无法识别，请刷新或重新提交。", showRetry: true};
}

function finishRun(outcome) {
  clearInterval(timer);
  closeSource();
  recovering = false;
  setBusy(false);
  setService(outcome.title, outcome.tone === "partial" ? "running" : outcome.tone === "success" ? "success" : "error");
  activeRun = null;
  loadConversations();
  $("#question").focus();
  scrollToLatest();
}

function finishWithCard(run, title, message) {
  run.bubble.classList.add("error-card");
  run.bubble.replaceChildren();
  const strong = document.createElement("strong");
  strong.textContent = title;
  const text = document.createElement("p");
  text.className = "answer-text error-text";
  text.textContent = message;
  run.bubble.append(strong, text);
  finishRun({title, tone: "error"});
}

function finishWithTransportError(run, message) {
  if (run.timeline) run.timeline.open = false;
  finishWithCard(run, "暂时无法完成分析", message);
}

function finishWithDiscard(run, message) {
  finishWithCard(run, "任务记录已失效", message);
}

function abandonRestore(message, question) {
  Recovery.clearRecord(sessionStorage);
  clearInterval(timer);
  closeSource();
  activeRun = null;
  recovering = false;
  setBusy(false);
  renderWelcome();
  setService("任务记录已失效", "error");
  if (message) {
    const notice = document.createElement("p");
    notice.className = "restore-notice";
    notice.textContent = message;
    $("#messages").prepend(notice);
  }
  $("#question").value = question || "";
  autoGrow();
  $("#question").focus();
}

function applyRestorePlan(run, task, plan) {
  if (plan.action === Recovery.ACTIONS.resume_events) {
    setService(plan.headline, "running");
    run.meta.textContent = plan.message;
    startClock(run);
    recordStage(run, "agent_orchestration", "running");
    followEvents(run, 0);
    return;
  }
  if (plan.action === Recovery.ACTIONS.render_result) {
    setService("分析完成", "success");
    renderAssistantResult(run, task);
    return;
  }
  if (plan.action === Recovery.ACTIONS.render_error) {
    renderAssistantResult(run, task);
    return;
  }
  if (plan.clearRecord) Recovery.clearRecord(sessionStorage);
  if (plan.action === Recovery.ACTIONS.interrupted) {
    finishWithCard(run, plan.headline, plan.message);
    $("#question").value = run.question || "";
    autoGrow();
    return;
  }
  abandonRestore(plan.message, run.question);
}

async function restoreActiveTask() {
  const record = Recovery.readRecord(sessionStorage) || await adoptServerTask();
  if (!record) return false;
  conversationId = record.conversation_id;
  Recovery.writeConversationId(sessionStorage, conversationId);
  const select = $("#database");
  if (record.database_id && [...select.options].some(option => option.value === record.database_id)) {
    select.value = record.database_id;
  }
  updateDatabaseUI();
  recovering = true;
  setBusy(true);
  setService(Recovery.RESTORE_START_MESSAGE, "running");
  $("#messages").replaceChildren();
  appendUserMessage(record.question || "（上一次任务）");
  const run = appendProgressMessage();
  run.requestId = record.request_id;
  run.question = record.question || "";
  run.databaseId = record.database_id;
  run.createdAt = record.created_at;
  run.sequence = record.sequence || 0;
  run.meta.textContent = Recovery.RESTORE_START_MESSAGE;
  activeRun = run;

  let task;
  try {
    const response = await fetch(`/api/query/${record.request_id}`);
    if (response.status === 404) {
      abandonRestore("任务记录已失效，请重新提交", record.question);
      return true;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    task = await response.json();
  } catch (error) {
    finishWithTransportError(run, `暂时无法查询任务状态：${error.message}`);
    return true;
  }
  applyRestorePlan(run, task, Recovery.planForStatus(task.status));
  return true;
}

async function adoptServerTask() {
  try {
    const response = await fetch("/api/query/active");
    if (!response.ok) return null;
    const data = await response.json();
    const record = Recovery.recordFromTask(data.task, conversationId);
    if (record) Recovery.writeRecord(sessionStorage, record);
    return record;
  } catch (error) {
    return null;
  }
}

/* ==================================================================== */
/* 8. 结果渲染                                                           */
/* ==================================================================== */
function renderAssistantResult(run, task, finalize = true) {
  const result = task.result || {};
  const artifacts = result.artifacts || {};
  const outcome = deriveOutcome(task);
  if (run.timeline) run.timeline.open = false;
  const spinner = run.row.querySelector(".thinking-spinner");
  if (spinner) spinner.remove();
  const headLabel = run.row.querySelector(".thinking-head strong");
  if (headLabel) headLabel.textContent = outcome.title;

  run.bubble.replaceChildren();
  if (outcome.tone !== "success") run.bubble.classList.add(outcome.tone === "partial" ? "" : "error-card");

  const header = document.createElement("div");
  header.className = "result-header";
  const title = document.createElement("strong");
  title.textContent = outcome.title;
  const statusBadge = document.createElement("span");
  statusBadge.className = `status-badge ${outcome.tone}`;
  statusBadge.textContent = {success: "成功", partial: "部分完成", error: "未完成"}[outcome.tone] || outcome.tone;
  const intent = document.createElement("span");
  intent.className = "intent-badge";
  intent.textContent = INTENT_NAMES[result.intent] || result.intent || "通用查询";
  const source = document.createElement("span");
  source.className = "intent-badge source-badge";
  source.textContent = databases.find(item => item.id === result.database_id)?.name || result.database_id || "未选择数据源";
  header.append(title, statusBadge, source, intent);
  run.bubble.appendChild(header);

  if (outcome.tone !== "success" && outcome.note) {
    const note = document.createElement("p");
    note.className = "partial-note";
    note.textContent = outcome.note;
    run.bubble.appendChild(note);
  }

  const isQueryFailure = ["error", "interrupted", "unknown"].includes(outcome.kind)
    && !(result.rows || []).length && !result.sql;
  const answer = document.createElement("div");
  answer.className = `answer-text${outcome.tone === "error" ? " error-text" : ""}`;
  renderMarkdown(answer, result.answer || result.error || (outcome.tone === "success" ? "查询已经完成。" : "请稍后重试。"));
  run.bubble.appendChild(answer);

  if (outcome.showRetry) {
    const retryRow = document.createElement("div");
    retryRow.className = "retry-row";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "retry-button";
    retry.textContent = "重试此问题";
    retry.addEventListener("click", () => {
      if (activeRun) return;
      $("#question").value = run.question || "";
      autoGrow();
      $("#query-form").requestSubmit();
    });
    retryRow.appendChild(retry);
    run.bubble.appendChild(retryRow);
  }

  if (isQueryFailure) {
    const stage = document.createElement("p");
    stage.className = "answer-text error-text";
    stage.textContent = `失败阶段：${result.failed_stage || "unknown"}`;
    run.bubble.appendChild(stage);
    if (finalize) finishRun(outcome);
    return;
  }

  // GraphRAG 知识证据（仅在真实事件存在时渲染）
  const evidencePanel = buildKnowledgeEvidence(run, result);
  if (evidencePanel) run.bubble.appendChild(evidencePanel);

  const contexts = result.business_context || [];
  if (contexts.length) {
    const context = document.createElement("div");
    context.className = "business-context";
    const label = document.createElement("small");
    label.textContent = "参考口径";
    context.appendChild(label);
    contexts.forEach(item => {
      const chip = document.createElement("span");
      chip.className = "chip";
      const retrieval = item.retrieval || {};
      chip.textContent = retrieval.score == null ? item.name : `${item.name} · ${Math.round(retrieval.score * 100)}%`;
      chip.title = retrieval.matched_keywords?.length
        ? `关键词命中：${retrieval.matched_keywords.join("、")}`
        : "由语义检索召回";
      context.appendChild(chip);
    });
    run.bubble.appendChild(context);
  }

  if (result.intent !== "chat") {
    const kpiGrid = buildKpiCards(result);
    if (kpiGrid) run.bubble.appendChild(kpiGrid);
    if (result.visualization && result.visualization.type !== "table") run.bubble.appendChild(buildChart(result.visualization));
    const tableColumns = (result.columns || []).length
      ? result.columns : (result.visualization?.columns || []);
    const tableRows = (result.rows || []).length
      ? result.rows : (result.visualization?.rows || []);
    if (tableColumns.length) run.bubble.appendChild(buildDataTable(tableColumns, tableRows, result.truncated));
    run.bubble.appendChild(buildExecutionDetails(result, artifacts, run));
  }
  if (finalize) finishRun(outcome);
}

/* KPI：只展示本轮真实查询结果中可读出的数字，不固定无关 KPI。 */
function buildKpiCards(result) {
  let items = null;
  if (result.visualization?.type === "metric" && (result.visualization.items || []).length) {
    items = result.visualization.items.map(item => ({label: item.name, value: item.value, kind: "number"}));
  } else if ((result.rows || []).length === 1 && (result.columns || []).length >= 2) {
    items = result.columns.map((column, index) => ({
      label: column,
      value: result.rows[0][index],
      kind: columnKind(column, result.rows, index),
    })).filter(item => !isBlankValue(item.value));
  }
  if (!items || !items.length) return null;
  const grid = document.createElement("div");
  grid.className = "kpi-grid";
  items.slice(0, 6).forEach(item => {
    const card = document.createElement("div");
    card.className = "kpi-card";
    const label = document.createElement("span");
    label.textContent = item.label;
    const strong = document.createElement("strong");
    strong.textContent = formatCellValue(item.value, item.kind);
    card.append(label, strong);
    grid.appendChild(card);
  });
  return grid;
}

function buildChart(chart) {
  const card = document.createElement("section");
  card.className = "result-card";
  const head = document.createElement("div"); head.className = "result-subhead";
  const title = document.createElement("strong"); title.textContent = chart.title || "数据可视化";
  const type = document.createElement("span");
  type.textContent = {bar: "柱状图", line: "折线图", pie: "饼图", metric: "指标卡"}[chart.type] || chart.type;
  head.append(title, type);
  const container = document.createElement("div"); container.className = "chart-container";
  card.append(head, container);
  renderVisualization(container, chart);
  return card;
}

/* 数据明细：排序 / 复制 / CSV / 展开收起 / 行数与截断状态 */
function buildDataTable(columns, rows, truncated) {
  const card = document.createElement("section");
  card.className = "table-card";
  const head = document.createElement("div");
  head.className = "result-subhead";
  const title = document.createElement("strong");
  title.textContent = "数据明细";
  const actions = document.createElement("div");
  actions.className = "result-actions";
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.className = "mini-button";
  copyButton.textContent = "复制数据";
  const csvButton = document.createElement("button");
  csvButton.type = "button";
  csvButton.className = "mini-button";
  csvButton.textContent = "下载 CSV";
  actions.append(copyButton, csvButton);
  head.append(title, actions);

  const kinds = columns.map((column, index) => columnKind(column, rows, index));
  let sortColumn = -1;
  let sortDirection = 1;
  let expanded = rows.length <= MAX_TABLE_ROWS_COLLAPSED;

  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "data-table";
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  columns.forEach((column, index) => {
    const th = document.createElement("th");
    th.textContent = column;
    th.addEventListener("click", () => {
      if (sortColumn === index) sortDirection = -sortDirection;
      else { sortColumn = index; sortDirection = 1; }
      renderBody();
      renderHead();
    });
    headerRow.appendChild(th);
  });
  thead.appendChild(headerRow);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  table.appendChild(tbody);
  wrap.appendChild(table);

  const foot = document.createElement("div");
  foot.className = "table-foot";
  const footInfo = document.createElement("span");
  const expandButton = document.createElement("button");
  expandButton.type = "button";
  expandButton.className = "mini-button";

  function sortedRows() {
    if (sortColumn < 0) return rows;
    return [...rows].sort((a, b) => {
      const left = a[sortColumn], right = b[sortColumn];
      if (isBlankValue(left)) return 1;
      if (isBlankValue(right)) return -1;
      const leftNum = Number(left), rightNum = Number(right);
      if (!Number.isNaN(leftNum) && !Number.isNaN(rightNum)) return (leftNum - rightNum) * sortDirection;
      return String(left).localeCompare(String(right), "zh-CN") * sortDirection;
    });
  }

  function renderHead() {
    [...headerRow.children].forEach((th, index) => {
      const arrow = th.querySelector(".sort-arrow");
      if (arrow) arrow.remove();
      if (index === sortColumn) {
        const mark = document.createElement("span");
        mark.className = "sort-arrow";
        mark.textContent = sortDirection === 1 ? "↑" : "↓";
        th.appendChild(mark);
      }
    });
  }

  function renderBody() {
    const visible = sortedRows();
    const shown = expanded ? visible : visible.slice(0, MAX_TABLE_ROWS_COLLAPSED);
    tbody.replaceChildren();
    shown.forEach(rowData => {
      const tr = document.createElement("tr");
      rowData.forEach((value, index) => {
        const td = document.createElement("td");
        if (isBlankValue(value)) td.className = "null-cell";
        else if (kinds[index] !== "text") td.className = "num";
        td.textContent = formatCellValue(value, kinds[index]);
        td.title = isBlankValue(value) ? "NULL / 缺失" : String(value);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    const total = rows.length;
    footInfo.textContent = truncated
      ? `实际返回前 ${total} 行（已截断，可能并非全部数据） · 当前显示 ${shown.length} 行`
      : `实际结果共 ${total} 行 · 当前显示 ${shown.length} 行`;
    expandButton.textContent = visible.length > MAX_TABLE_ROWS_COLLAPSED
      ? (expanded ? "收起" : `展开全部 ${visible.length} 行`) : "";
    expandButton.style.display = visible.length > MAX_TABLE_ROWS_COLLAPSED ? "" : "none";
  }

  expandButton.addEventListener("click", () => { expanded = !expanded; renderBody(); });
  copyButton.addEventListener("click", async () => {
    const tsv = [columns.join("\t"), ...rows.map(row => row.map(valueText).join("\t"))].join("\n");
    try { await navigator.clipboard.writeText(tsv); copyButton.textContent = "已复制"; }
    catch { copyButton.textContent = "复制失败"; }
    setTimeout(() => copyButton.textContent = "复制数据", 1500);
  });
  csvButton.addEventListener("click", () => {
    const escapeCell = cell => {
      const text = valueText(cell);
      return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
    };
    const csv = "\uFEFF" + [columns.map(escapeCell).join(","), ...rows.map(row => row.map(escapeCell).join(","))].join("\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([csv], {type: "text/csv;charset=utf-8"}));
    link.download = `deepeye_result_${Date.now()}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  });

  renderBody();
  foot.append(footInfo, expandButton);
  card.append(head, wrap, foot);
  return card;
}

/* SQL 详情：生成 / 修订 / 最终执行 三段如实区分，附高亮、复制、耗时与行数。 */
const SQL_KEYWORDS = /\b(SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|JOIN|LEFT(?:\s+OUTER)?\s+JOIN|INNER\s+JOIN|FULL\s+JOIN|CROSS\s+JOIN|ON|AS|AND|OR|NOT|IN|BETWEEN|IS|NULL|CASE|WHEN|THEN|ELSE|END|CAST|SUM|COUNT|AVG|MIN|MAX|ROUND|DISTINCT|DESC|ASC|WITH|UNION|LIKE|COALESCE|SUBSTR|DATE|STRFTIME)\b/gi;

function renderSqlBlock(sqlText, compact = false) {
  const block = document.createElement("div");
  block.className = "sql-block";
  const pre = document.createElement("pre");
  pre.innerHTML = escapeHtml(sqlText).replace(SQL_KEYWORDS, '<span class="sql-kw">$1</span>');
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.className = "mini-button";
  copyButton.textContent = "复制 SQL";
  copyButton.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(sqlText); copyButton.textContent = "已复制"; }
    catch { copyButton.textContent = "复制失败"; }
    setTimeout(() => copyButton.textContent = "复制 SQL", 1500);
  });
  block.append(copyButton, pre);
  return block;
}

function buildSqlDetails(result, artifacts) {
  const card = document.createElement("section");
  card.className = "result-card";
  const head = document.createElement("div");
  head.className = "result-subhead";
  const title = document.createElement("strong");
  title.textContent = "SQL 详情";
  const execSeconds = result.stage_seconds?.execution;
  const note = document.createElement("span");
  note.textContent = `执行耗时 ${execSeconds != null ? formatSeconds(execSeconds) : "未知"} · 结果 ${formatNumber((result.rows || []).length)} 行`;
  head.append(title, note);
  card.appendChild(head);

  const finalSql = artifacts.final_sql || result.sql;
  if (finalSql) card.appendChild(renderSqlBlock(finalSql));
  else {
    const missing = document.createElement("p");
    missing.className = "answer-text";
    missing.textContent = "本轮没有实际执行的 SQL。";
    card.appendChild(missing);
  }
  // 参考链路（生成 → 修订）与最终执行 SQL 明确区分，不冒充执行 SQL。
  const variants = [];
  if (artifacts.generated_sql && artifacts.generated_sql !== finalSql) variants.push(["生成候选 SQL", artifacts.generated_sql]);
  if (artifacts.revised_sql && artifacts.revised_sql !== finalSql && artifacts.revised_sql !== artifacts.generated_sql) variants.push(["修订候选 SQL", artifacts.revised_sql]);
  variants.forEach(([label, sql]) => {
    const wrapper = document.createElement("details");
    wrapper.className = "sql-variant";
    const summary = document.createElement("summary");
    summary.className = "sql-variant-label";
    summary.textContent = `${label}（参考链路，非最终执行）`;
    wrapper.append(summary, renderSqlBlock(sql, true));
    card.appendChild(wrapper);
  });
  return card;
}

/* 执行详情：端到端耗时 / 调用次数 / Token / 检索与 SQL 耗时 / 阶段耗时条 */
function buildExecutionDetails(result, artifacts, run) {
  const details = document.createElement("details");
  details.className = "technical-details";
  const summary = document.createElement("summary");
  summary.textContent = "执行详情（耗时 · Token · 阶段）";
  const grid = document.createElement("div");
  grid.className = "technical-grid";

  const usage = result.usage || {}, agentUsage = result.agent_usage || {};
  const chatPrompt = usage.chat?.prompt_tokens ?? null;
  const chatCompletion = usage.chat?.completion_tokens ?? null;
  const totalTokens = (usage.chat?.total_tokens || 0) + (usage.embedding?.total_tokens || 0) + (agentUsage.total_tokens || 0);
  const retrievalSeconds = run?.knowledge?.observability?.total_elapsed_seconds
    ?? result.stage_seconds?.value_retrieval ?? null;
  const usageItems = [
    ["端到端耗时", formatSeconds(result.agent_elapsed_seconds ?? result.elapsed_seconds)],
    ["LLM 调用次数", result.model_calls != null ? formatNumber(result.model_calls) : "未知"],
    ["工具调用次数", run?.toolCalls?.length ? formatNumber(run.toolCalls.length)
      : (result.tool_trace || []).length ? formatNumber(result.tool_trace.length) : "未知"],
    ["输入 Token", chatPrompt != null ? formatNumber(chatPrompt) : "未知"],
    ["输出 Token", chatCompletion != null ? formatNumber(chatCompletion) : "未知"],
    ["总 Token", totalTokens ? formatNumber(totalTokens) : "未知"],
    ["SQL 执行耗时", result.stage_seconds?.execution != null ? formatSeconds(result.stage_seconds.execution) : "未知"],
    ["检索耗时", retrievalSeconds != null ? formatSeconds(retrievalSeconds) : "未知"],
    ["结果行数", formatNumber((result.rows || []).length)],
    ["值检索列数", artifacts.retrieved_value_columns != null ? formatNumber(artifacts.retrieved_value_columns) : "未知"],
    ["Few-shot 示例数", artifacts.few_shot_example_count != null ? formatNumber(artifacts.few_shot_example_count) : "未知"],
  ];
  const usageBlock = document.createElement("div");
  usageBlock.className = "technical-block";
  const usageTitle = document.createElement("strong");
  usageTitle.textContent = "关键指标";
  const usageGrid = document.createElement("div");
  usageGrid.className = "usage-grid";
  usageItems.forEach(([label, value]) => {
    const item = document.createElement("div");
    item.className = "usage-item";
    const name = document.createElement("span");
    name.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = value;
    item.append(name, strong);
    usageGrid.appendChild(item);
  });
  usageBlock.append(usageTitle, usageGrid);
  grid.appendChild(usageBlock);

  const timing = document.createElement("div");
  timing.className = "technical-block";
  const timingTitle = document.createElement("strong");
  timingTitle.textContent = "阶段耗时";
  const stageTimes = document.createElement("div");
  stageTimes.className = "stage-times";
  const times = result.stage_seconds || {}, max = Math.max(...Object.values(times), 1);
  const orderedStages = [...STAGE_ORDER.map(([id]) => id), ...Object.keys(times).filter(id => !STAGE_LABELS[id])];
  orderedStages.filter(id => times[id] != null).forEach(id => {
    const rowEl = document.createElement("div");
    rowEl.className = "time-row";
    const label = document.createElement("span");
    label.textContent = stageDisplayLabel(id);
    const bar = document.createElement("span");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = `${Math.max(2, times[id] / max * 100)}%`;
    bar.appendChild(fill);
    const seconds = document.createElement("span");
    seconds.textContent = formatSeconds(times[id]);
    rowEl.append(label, bar, seconds);
    stageTimes.appendChild(rowEl);
  });
  if (!Object.keys(times).length) stageTimes.textContent = "无阶段耗时记录";
  timing.append(timingTitle, stageTimes);
  grid.appendChild(timing);

  const trace = document.createElement("div");
  trace.className = "technical-block";
  const traceTitle = document.createElement("strong");
  traceTitle.textContent = "工具调用轨迹";
  const traceList = document.createElement("div");
  traceList.className = "tool-trace";
  (result.tool_trace || []).forEach((item, index) => {
    const rowEl = document.createElement("div");
    rowEl.textContent = `${index + 1}. ${item.tool} · ${item.status}`;
    traceList.appendChild(rowEl);
  });
  if (!(result.tool_trace || []).length) traceList.textContent = "本轮没有工具调用";
  trace.append(traceTitle, traceList);
  grid.appendChild(trace);
  details.append(summary, grid);
  return details;
}

/* ==================================================================== */
/* 9. 图表（复用原 SVG 渲染）                                             */
/* ==================================================================== */
function svgNode(name, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function renderVisualization(container, chart) {
  if (chart.type === "metric") {
    const grid = document.createElement("div");
    grid.className = "chart-metrics";
    (chart.items || []).forEach(item => {
      const node = document.createElement("div");
      node.className = "chart-metric";
      const label = document.createElement("span");
      label.textContent = item.name;
      const value = document.createElement("strong");
      value.textContent = formatNumber(item.value);
      node.append(label, value);
      grid.appendChild(node);
    });
    container.appendChild(grid);
    return;
  }
  if (chart.type === "pie") { renderPie(container, chart.items || []); return; }
  renderCartesian(container, chart);
}

function renderCartesian(container, chart) {
  const labels = chart.labels || [], series = chart.series || [];
  const width = 760, height = 250, left = 58, right = 18, top = 30, bottom = 55;
  const svg = svgNode("svg", {viewBox: `0 0 ${width} ${height}`, role: "img"});
  const values = series.flatMap(item => item.values || []).map(Number).filter(Number.isFinite);
  const max = Math.max(...values, 1), plotW = width - left - right, plotH = height - top - bottom;
  svg.appendChild(svgNode("line", {x1: left, y1: top, x2: left, y2: top + plotH, class: "chart-axis"}));
  svg.appendChild(svgNode("line", {x1: left, y1: top + plotH, x2: left + plotW, y2: top + plotH, class: "chart-axis"}));
  const count = Math.max(labels.length, 1), step = plotW / count;
  if (chart.type === "bar") (series[0]?.values || []).forEach((value, index) => {
    const num = Number(value);
    const h = Number.isFinite(num) ? num / max * plotH : 0, barY = top + plotH - h;
    svg.appendChild(svgNode("rect", {x: left + index * step + step * .18, y: barY, width: step * .64, height: h, rx: 4, class: "chart-bar"}));
    if (Number.isFinite(num)) {
      const valueNode = svgNode("text", {x: left + index * step + step / 2, y: barY - 6, class: "chart-value", "text-anchor": "middle"});
      valueNode.textContent = formatNumber(num);
      svg.appendChild(valueNode);
    }
  });
  else series.forEach((item, s) => {
    const points = (item.values || []).map((value, index) => `${left + index * step + step / 2},${top + plotH - Number(value) / max * plotH}`).join(" ");
    svg.appendChild(svgNode("polyline", {points, class: `chart-line series-${s}`}));
    (item.values || []).forEach((value, index) => {
      const num = Number(value);
      if (!Number.isFinite(num)) return;
      const valueNode = svgNode("text", {x: left + index * step + step / 2, y: top + plotH - num / max * plotH - 8, class: `chart-value series-${s}`, "text-anchor": "middle"});
      valueNode.textContent = formatNumber(num);
      svg.appendChild(valueNode);
    });
  });
  labels.forEach((label, index) => {
    const text = svgNode("text", {x: left + index * step + step / 2, y: top + plotH + 21, class: "chart-label", "text-anchor": "middle"});
    text.textContent = String(label).slice(0, 9);
    svg.appendChild(text);
  });
  container.appendChild(svg);
}

function renderPie(container, items) {
  const total = items.reduce((sum, item) => sum + Number(item.value || 0), 0) || 1;
  const colors = ["#1d4ed8", "#0e9aa7", "#7c5cd6", "#d97706", "#b3392f", "#0f7a4f"];
  let angle = 0;
  const stops = [];
  items.forEach((item, index) => {
    const next = angle + Number(item.value || 0) / total * 360;
    stops.push(`${colors[index % colors.length]} ${angle}deg ${next}deg`);
    angle = next;
  });
  const wrap = document.createElement("div");
  wrap.className = "pie-layout";
  const pie = document.createElement("div");
  pie.className = "pie-chart";
  pie.style.background = `conic-gradient(${stops.join(",")})`;
  wrap.appendChild(pie);
  const legend = document.createElement("div");
  legend.className = "pie-legend";
  items.forEach((item, index) => {
    const rowEl = document.createElement("div");
    const dot = document.createElement("i");
    const name = document.createElement("span");
    const pct = document.createElement("strong");
    dot.style.background = colors[index % colors.length];
    name.textContent = item.name;
    pct.textContent = `${(Number(item.value || 0) / total * 100).toFixed(1)}%`;
    rowEl.append(dot, name, pct);
    legend.appendChild(rowEl);
  });
  wrap.appendChild(legend);
  container.appendChild(wrap);
}

/* ==================================================================== */
/* 10. GraphRAG 知识证据面板（只渲染真实 observability 事件）              */
/* ==================================================================== */
function shortTableName(fullName) {
  return String(fullName || "").replace(/^[a-z_]+\./i, "");
}

function buildKnowledgeEvidence(run, result) {
  const observability = run?.knowledge?.observability;
  const isAdventureWorks = result.database_id === "adventureworks";
  if (!observability) {
    if (isAdventureWorks && run?.knowledge && run.knowledge.enabled === false) {
      const note = document.createElement("div");
      note.className = "knowledge-evidence";
      const body = document.createElement("div");
      body.className = "evidence-body";
      const text = document.createElement("p");
      text.className = "evidence-disclaimer";
      text.textContent = `本轮未启用 GraphRAG 知识增强：${run.knowledge.error || "ENABLE_KNOWLEDGE_ENHANCEMENT 未开启"}。以上结果来自原始 Schema Linking 流程，未经过知识层证据注入。`;
      body.appendChild(text);
      note.appendChild(body);
      return note;
    }
    return null;
  }

  const panel = document.createElement("details");
  panel.className = "knowledge-evidence";
  panel.open = true;
  const summary = document.createElement("summary");
  const summaryTitle = document.createElement("span");
  summaryTitle.textContent = "知识证据（GraphRAG 检索）";
  const modeBadge = document.createElement("span");
  modeBadge.className = "evidence-mode";
  modeBadge.textContent = observability.retrieval_mode === "hybrid" ? "hybrid 混合检索" : `keyword 关键词`;
  const meta = document.createElement("span");
  meta.className = "evidence-meta";
  meta.textContent = `${formatNumber(observability.context_tokens)} tokens · ${formatSeconds(observability.total_elapsed_seconds)}`;
  summary.append(summaryTitle, modeBadge, meta);
  panel.appendChild(summary);

  const body = document.createElement("div");
  body.className = "evidence-body";

  function chipGroup(labelText, names, chipClass) {
    if (!names || !names.length) return;
    const section = document.createElement("div");
    section.className = "evidence-section";
    const strong = document.createElement("strong");
    strong.textContent = labelText;
    const chips = document.createElement("div");
    chips.className = "evidence-chips";
    names.forEach(name => {
      const chip = document.createElement("span");
      chip.className = `evidence-chip ${chipClass}`;
      chip.textContent = name;
      chips.appendChild(chip);
    });
    section.append(strong, chips);
    body.appendChild(section);
  }

  // 命中指标：来自真实 business_context + review_audit 审核状态。
  const metricNames = (result.business_context || []).map(item => {
    const score = item.retrieval?.score;
    return score == null ? item.name : `${item.name} · ${Math.round(score * 100)}%`;
  });
  chipGroup("命中的业务指标（候选口径，非已确认规则）", metricNames, "metric");
  const audit = observability.review_audit;
  if (audit && typeof audit === "object") {
    const applied = audit.applied || audit.reviewed || [];
    const pendingCount = Array.isArray(applied) ? applied.filter(item => item && item.trusted === false).length : null;
    if (pendingCount != null && pendingCount > 0) {
      chipGroup("待人工审核指标", [`${pendingCount} 条指标 trusted=false，仅作为检索提示`], "risk");
    }
  }

  const seeds = (observability.original_seeds?.tables || []).map(shortTableName);
  const added = (observability.graph_added_nodes?.tables || []).map(shortTableName);
  const intermediate = (observability.necessary_intermediate_tables || []).map(shortTableName);
  chipGroup("原始种子表（检索直接命中）", seeds, "seed");
  chipGroup("图扩展补充表（GraphRAG 关系扩展）", added, "added");
  chipGroup("必要中间表", intermediate, "intermediate");

  // 候选 Join 路径：可折叠路径卡片（真实候选路径，不冒充已验证）。
  const paths = observability.candidate_join_paths || [];
  if (paths.length) {
    const section = document.createElement("div");
    section.className = "evidence-section";
    const strong = document.createElement("strong");
    strong.textContent = `候选 Join 路径（${paths.length} 条，未经语义验证）`;
    section.appendChild(strong);
    paths.slice(0, 6).forEach(pathTables => {
      const cardEl = document.createElement("div");
      cardEl.className = "path-card";
      cardEl.textContent = pathTables.map(shortTableName).join("  →  ");
      section.appendChild(cardEl);
    });
    if (paths.length > 6) {
      const more = document.createElement("div");
      more.className = "evidence-disclaimer";
      more.textContent = `另有 ${paths.length - 6} 条路径未展开。`;
      section.appendChild(more);
    }
    body.appendChild(section);
    renderSubgraph(section, seeds, added, intermediate, paths);
  }

  const composite = observability.composite_fk_handling;
  if (composite) {
    const ok = composite.all_paths_complete;
    const section = document.createElement("div");
    section.className = "evidence-section";
    const strong = document.createElement("strong");
    strong.textContent = "复合外键处理";
    const chips = document.createElement("div");
    chips.className = "evidence-chips";
    const chip = document.createElement("span");
    chip.className = `evidence-chip ${ok ? "seed" : "risk"}`;
    chip.textContent = `${ok ? "候选路径均完整闭合全部复合外键列" : "存在复合外键未完整闭合的路径"}（${(composite.groups || []).length} 组复合外键）`;
    chips.appendChild(chip);
    section.append(strong, chips);
    body.appendChild(section);
  }

  const risks = observability.grain_risks || [];
  if (risks.length) {
    chipGroup("业务粒度风险提示", risks.map(risk => risk.kind || risk), "risk");
  }
  if (observability.ambiguity_count > 0) {
    chipGroup("歧义提示", [`${observability.ambiguity_count} 处路径歧义，需人工确认口径`], "risk");
  }

  const disclaimer = document.createElement("p");
  disclaimer.className = "evidence-disclaimer";
  disclaimer.textContent = "以上证据为 GraphRAG 检索注入的提示（HINT），不构成已完成验证的 SQL 语义约束；"
    + "候选指标与 Join 路径未经人工审核，不是已确认业务规则。";
  body.appendChild(disclaimer);
  panel.appendChild(body);
  return panel;
}

/* 相关子图 SVG：节点全部来自真实检索结果，仅在本轮涉及的表之间画路径边。 */
function renderSubgraph(container, seeds, added, intermediate, paths) {
  const nodes = [...new Set([...seeds, ...added, ...intermediate])].slice(0, 14);
  if (nodes.length < 2 || nodes.length > 14) return;
  const edges = [];
  paths.forEach(pathTables => {
    const short = pathTables.map(shortTableName);
    for (let index = 0; index < short.length - 1; index += 1) {
      if (nodes.includes(short[index]) && nodes.includes(short[index + 1])) {
        const key = `${short[index]}->${short[index + 1]}`;
        if (!edges.some(edge => edge.key === key)) edges.push({key, from: short[index], to: short[index + 1]});
      }
    }
  });
  const width = 680, height = Math.max(200, nodes.length * 34);
  const cx = width / 2, cy = height / 2, radius = Math.min(width, height) / 2 - 60;
  const position = new Map();
  nodes.forEach((node, index) => {
    const theta = index / nodes.length * Math.PI * 2 - Math.PI / 2;
    position.set(node, [cx + radius * Math.cos(theta), cy + radius * Math.sin(theta)]);
  });
  const svg = svgNode("svg", {viewBox: `0 0 ${width} ${height}`, class: "subgraph-svg", role: "img"});
  const defs = svgNode("defs");
  const marker = svgNode("marker", {id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse"});
  marker.appendChild(svgNode("path", {d: "M 0 0 L 10 5 L 0 10 z", fill: "#b6c1d2"}));
  defs.appendChild(marker);
  svg.appendChild(defs);
  edges.forEach(edge => {
    const [x1, y1] = position.get(edge.from);
    const [x2, y2] = position.get(edge.to);
    svg.appendChild(svgNode("line", {x1, y1, x2, y2, class: "edge"}));
  });
  const nodeKind = new Map([
    ...seeds.map(name => [name, "seed"]),
    ...added.map(name => [name, "added"]),
    ...intermediate.map(name => [name, "intermediate"]),
  ]);
  const kindColor = {seed: "#1d4ed8", added: "#0e9aa7", intermediate: "#d97706"};
  nodes.forEach(node => {
    const [x, y] = position.get(node);
    svg.appendChild(svgNode("circle", {cx: x, cy: y - 14, r: 6, fill: kindColor[nodeKind.get(node) || "seed"]}));
    const label = svgNode("text", {x, y: y + 6, "text-anchor": "middle"});
    label.textContent = node.length > 22 ? `${node.slice(0, 21)}…` : node;
    svg.appendChild(label);
  });
  const legend = document.createElement("div");
  legend.className = "evidence-chips";
  [["seed", "指标/种子表"], ["added", "图扩展表"], ["intermediate", "中间表"]].forEach(([kind, name]) => {
    const chip = document.createElement("span");
    chip.className = "evidence-chip";
    chip.style.borderLeftColor = kindColor[kind];
    chip.style.borderLeftWidth = "3px";
    chip.textContent = name;
    legend.appendChild(chip);
  });
  container.appendChild(svg);
  container.appendChild(legend);
}

/* ==================================================================== */
/* 11. 企业知识库页面（只读）                                             */
/* ==================================================================== */
async function loadKnowledge(force = false) {
  if (kbCache && !force) return kbCache;
  const [overviewResponse, metricsResponse, schemaResponse, relationsResponse] = await Promise.all([
    fetch("/api/knowledge/overview"),
    fetch("/api/knowledge/metrics"),
    fetch("/api/knowledge/schema"),
    fetch("/api/knowledge/relations"),
  ]);
  if (!overviewResponse.ok || !metricsResponse.ok || !schemaResponse.ok || !relationsResponse.ok) {
    throw new Error("知识产物读取失败");
  }
  kbCache = {
    overview: await overviewResponse.json(),
    metrics: (await metricsResponse.json()).metrics || [],
    schemas: (await schemaResponse.json()).databases || [],
    relations: (await relationsResponse.json()).databases || [],
  };
  return kbCache;
}

function renderKnowledgeSummary(overview) {
  const target = $("#knowledge-summary");
  const totals = overview.totals || {};
  const items = [
    ["数据库", totals.database_count, "已构建知识产物"],
    ["表", totals.table_count, "覆盖全部注册库"],
    ["字段", totals.column_count, "列级字典"],
    ["指标口径", totals.metric_count, "来自业务口径 JSON"],
    ["外键关系", totals.relation_count, "PRAGMA 声明"],
    ["知识边", totals.edge_count, "图谱边总数"],
    ["已审核指标", `${totals.reviewed_metric_count}/${totals.metric_count}`, "其余为待审核"],
  ];
  target.replaceChildren();
  items.forEach(([label, value, note]) => {
    const card = document.createElement("div");
    card.className = "kpi-card";
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = formatNumber(value);
    const noteEl = document.createElement("small");
    noteEl.textContent = note;
    card.append(labelEl, strong, noteEl);
    target.appendChild(card);
  });
}

function renderKnowledgeTab(tab) {
  kbActiveTab = tab;
  document.querySelectorAll(".kb-tab").forEach(item => item.classList.toggle("active", item.dataset.kbtab === tab));
  const body = $("#kb-body");
  if (!kbCache) return;
  body.replaceChildren();
  if (tab === "metrics") renderKbMetrics(body);
  else if (tab === "schema") renderKbSchema(body);
  else renderKbRelations(body);
}

function renderKbMetrics(body) {
  const grid = document.createElement("div");
  grid.className = "metric-card-grid";
  kbCache.metrics.forEach(metric => {
    const card = document.createElement("div");
    card.className = "metric-detail-card";
    const header = document.createElement("header");
    const name = document.createElement("strong");
    name.textContent = metric.name || metric.metric_id;
    const id = document.createElement("small");
    id.textContent = metric.database_id;
    const badge = document.createElement("span");
    badge.className = `review-badge ${metric.reviewed ? "reviewed" : "pending"}`;
    badge.textContent = metric.reviewed ? "已审核" : "待审核";
    header.append(name, badge, id);
    const desc = document.createElement("p");
    desc.className = "metric-desc";
    desc.textContent = metric.description || "无定义。";
    const facts = document.createElement("div");
    facts.className = "metric-facts";
    const factChips = [
      ["聚合", metric.aggregation],
      ["粒度", metric.grain !== "UNKNOWN" ? metric.grain : "未知"],
      ["依赖字段", (metric.source_columns || []).length ? `${metric.source_columns.length} 列` : "未知"],
      ["依赖表", (metric.source_tables || []).length ? metric.source_tables.map(shortTableName).join(", ") : "未知"],
      ["时间字段", metric.time_field || "未声明"],
    ];
    factChips.forEach(([label, value]) => {
      const chip = document.createElement("span");
      chip.className = "evidence-chip";
      chip.textContent = `${label}: ${value ?? "未知"}`;
      facts.appendChild(chip);
    });
    card.append(header, desc);
    if (metric.expression) {
      const expression = document.createElement("div");
      expression.className = "metric-expression";
      expression.textContent = metric.expression;
      card.appendChild(expression);
    }
    card.appendChild(facts);
    grid.appendChild(card);
  });
  if (!kbCache.metrics.length) {
    body.innerHTML = '<p class="empty-note">暂无指标产物。</p>';
    return;
  }
  body.appendChild(grid);
}

function renderKbSchema(body) {
  const selector = document.createElement("div");
  selector.className = "kb-db-selector";
  const label = document.createElement("label");
  label.textContent = "选择数据库";
  const select = document.createElement("select");
  kbCache.schemas.forEach(db => select.appendChild(new Option(db.database_id, db.database_id)));
  selector.append(label, select);
  const list = document.createElement("div");
  list.className = "schema-table-list";

  function renderDb(databaseId) {
    const db = kbCache.schemas.find(item => item.database_id === databaseId);
    list.replaceChildren();
    if (!db) return;
    (db.tables || []).forEach(table => {
      const details = document.createElement("details");
      details.className = "schema-table";
      const summary = document.createElement("summary");
      const tableName = document.createElement("span");
      tableName.textContent = table.table_name || table.table_id;
      const count = document.createElement("small");
      count.textContent = `${table.column_count} 列`;
      summary.append(tableName, count);
      const columns = document.createElement("div");
      columns.className = "schema-columns";
      (table.columns || []).forEach(column => {
        const item = document.createElement("div");
        item.className = "schema-column";
        const name = document.createElement("span");
        name.className = `col-name${column.primary_key ? " pk" : ""}`;
        name.textContent = column.name;
        const type = document.createElement("span");
        type.className = "col-type";
        type.textContent = `${column.data_type || "?"}${column.nullable ? "" : " NOT NULL"}`;
        item.append(name, type);
        columns.appendChild(item);
      });
      details.append(summary, columns);
      list.appendChild(details);
    });
  }

  select.addEventListener("change", () => renderDb(select.value));
  renderDb(kbCache.schemas[0]?.database_id);
  body.append(selector, list);
}

function renderKbRelations(body) {
  const selector = document.createElement("div");
  selector.className = "kb-db-selector";
  const label = document.createElement("label");
  label.textContent = "选择数据库";
  const select = document.createElement("select");
  kbCache.relations.forEach(db => select.appendChild(new Option(db.database_id, db.database_id)));
  selector.append(label, select);

  const tableSelector = document.createElement("div");
  tableSelector.className = "kb-db-selector";
  const tableLabel = document.createElement("label");
  tableLabel.textContent = "选择表";
  const tableSelect = document.createElement("select");
  tableSelector.append(tableLabel, tableSelect);

  const list = document.createElement("div");
  list.className = "relation-list";

  function renderTables(databaseId) {
    const db = kbCache.relations.find(item => item.database_id === databaseId);
    const tableNames = new Set();
    (db?.relations || []).forEach(relation => {
      if (relation.source_table) tableNames.add(relation.source_table);
      if (relation.target_table) tableNames.add(relation.target_table);
    });
    tableSelect.replaceChildren(new Option("全部表", "*"),
      ...[...tableNames].sort().map(name => new Option(shortTableName(name), name)));
    renderRelations();
  }

  function renderRelations() {
    const db = kbCache.relations.find(item => item.database_id === select.value);
    const filter = tableSelect.value;
    list.replaceChildren();
    const rows = (db?.relations || []).filter(relation => filter === "*"
      || relation.source_table === filter || relation.target_table === filter);
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "empty-note";
      empty.textContent = "该表没有外键关系记录。";
      list.appendChild(empty);
      return;
    }
    rows.forEach(relation => {
      const item = document.createElement("div");
      item.className = "relation-item";
      const text = document.createElement("span");
      text.innerHTML = `${escapeHtml(relation.source_table)}<span class="arrow"> → </span>${escapeHtml(relation.target_table)}`;
      item.appendChild(text);
      const cardinality = document.createElement("span");
      cardinality.className = "relation-tag";
      cardinality.textContent = relation.cardinality || "";
      item.appendChild(cardinality);
      if (relation.composite) {
        const tag = document.createElement("span");
        tag.className = "relation-tag composite";
        tag.textContent = "复合外键";
        item.appendChild(tag);
      }
      if (relation.duplicate_aggregation_risk) {
        const tag = document.createElement("span");
        tag.className = "relation-tag risk";
        tag.textContent = "重复聚合风险";
        item.appendChild(tag);
      }
      const columns = document.createElement("small");
      columns.style.color = "var(--faint)";
      columns.textContent = `${relation.source_columns.join(", ")} ↔ ${relation.target_columns.join(", ")}`;
      item.appendChild(columns);
      list.appendChild(item);
    });
  }

  select.addEventListener("change", () => renderTables(select.value));
  tableSelect.addEventListener("change", renderRelations);
  renderTables(kbCache.relations[0]?.database_id);
  body.append(selector, tableSelector, list);
}

function renderSimulatedNote(overview) {
  const target = $("#kb-simulated");
  const assets = overview.simulated_assets || {};
  target.textContent = `${assets.statement || ""} 当前共 ${assets.document_count || 0} 份模拟制度文档：${(assets.documents || []).join("、") || "无"}`;
}

async function openKnowledgePanel(force = false) {
  try {
    const data = await loadKnowledge(force);
    renderKnowledgeSummary(data.overview);
    renderKnowledgeTab(kbActiveTab);
    renderSimulatedNote(data.overview);
    setService("知识库已加载", "success");
  } catch (error) {
    $("#kb-body").replaceChildren();
    const note = document.createElement("p");
    note.className = "empty-note";
    note.textContent = `知识产物暂时无法读取：${error.message}`;
    $("#kb-body").appendChild(note);
    setService("知识库读取失败", "error");
  }
}

/* ==================================================================== */
/* 12. 离线评测工作台                                                    */
/* ==================================================================== */
async function loadEvaluations() {
  const target = $("#evaluation-list");
  target.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "empty-note";
  loading.textContent = "正在读取报告…";
  target.appendChild(loading);
  try {
    const response = await fetch("/api/evaluations");
    const data = await response.json();
    const evaluations = data.evaluations || [];
    target.replaceChildren();
    if (!evaluations.length) {
      const empty = document.createElement("div");
      empty.className = "eval-card";
      empty.innerHTML = "<h3>暂无评测报告</h3>";
      target.appendChild(empty);
      return;
    }
    evaluations.forEach(item => {
      const card = document.createElement("article");
      card.className = "eval-card";
      card.dataset.eval = item.id;
      const title = document.createElement("h3");
      title.textContent = item.title;
      const idLine = document.createElement("div");
      idLine.className = "eval-id";
      idLine.textContent = `${item.id} · ${item.sample_count ?? 0} 题`;
      let kpi, label, noteText = "";
      if (item.kind === "agent") {
        kpi = item.pass_rate;
        label = "Agent 功能检查通过率";
        noteText = "在线端到端评测（消耗 LLM 配额），检查各意图路由与安全约束。";
      } else if (item.kind === "retrieval") {
        kpi = item.variants?.hybrid?.recall_at_k;
        label = "混合 RAG Recall@3（离线检索）";
        const keywordScore = item.variants?.keyword?.recall_at_k;
        noteText = keywordScore != null ? `关键词基线 ${formatPercent(keywordScore)} → hybrid ${formatPercent(kpi)}；纯离线检索评测，未消耗 LLM 配额。` : "";
      } else if (item.kind === "ablation") {
        kpi = item.variants?.full_pipeline?.execution_accuracy;
        label = "完整流程 集合式 EX";
        noteText = "在线 SQL 评测（消耗配额）；三种消融配置对照，题目为 BIRD 开发集抽样。";
      } else if (item.kind === "join_paths") {
        kpi = item.methods?.ours?.gold_edge_recall_micro;
        label = "GraphRAG 边召回（与最短路径持平）";
        const baselineScore = item.methods?.baseline_shortest?.gold_edge_recall_micro;
        if (baselineScore != null && kpi != null && Math.abs(baselineScore - kpi) < 1e-9) {
          noteText = "公平对照：GraphRAG 与最短路径边召回相同，优势体现在歧义标记与复合外键完整性，不体现为召回率提升。";
        }
      } else {
        kpi = item.execution_accuracy;
        label = "集合式 EX";
        noteText = "历史基线评测产物。";
      }
      const kpiEl = document.createElement("div");
      kpiEl.className = "eval-kpi";
      kpiEl.textContent = formatPercent(kpi);
      const labelEl = document.createElement("div");
      labelEl.className = "eval-kpi-label";
      labelEl.textContent = label;
      card.append(title, idLine, kpiEl, labelEl);
      if (noteText) {
        const note = document.createElement("div");
        note.className = "eval-note";
        note.textContent = noteText;
        card.appendChild(note);
      }
      card.addEventListener("click", () => loadEvaluationDetail(item.id));
      target.appendChild(card);
    });
  } catch (error) {
    target.replaceChildren();
    const note = document.createElement("p");
    note.className = "empty-note";
    note.textContent = error.message;
    target.appendChild(note);
  }
}

function detailHeading(title, subtitle = "") {
  const head = document.createElement("div");
  head.className = "section-heading";
  const main = document.createElement("div");
  main.className = "section-heading-main";
  const text = document.createElement("div");
  const h2 = document.createElement("h2");
  h2.textContent = title;
  text.appendChild(h2);
  if (subtitle) {
    const p = document.createElement("p");
    p.className = "section-intro";
    p.textContent = subtitle;
    text.appendChild(p);
  }
  main.appendChild(text);
  head.appendChild(main);
  return head;
}

function buildSimpleTable(headerCells, rowsData) {
  const wrap = document.createElement("div");
  wrap.className = "eval-table";
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headerCells.forEach(cell => { const th = document.createElement("th"); th.textContent = cell; headRow.appendChild(th); });
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  rowsData.forEach(row => {
    const tr = document.createElement("tr");
    row.forEach(cell => { const td = document.createElement("td"); td.textContent = cell; tr.appendChild(td); });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

async function loadEvaluationDetail(id) {
  const response = await fetch(`/api/evaluations/${encodeURIComponent(id)}`);
  if (!response.ok) return setService("评测详情读取失败", "error");
  const data = await response.json();
  const target = $("#evaluation-detail");
  target.classList.remove("hidden");
  target.replaceChildren();

  if (data.kind === "join_paths") {
    target.appendChild(detailHeading(data.title, "同预算、同端点对、同图投影的公平方法对照；数据来自真实实验产物。"));
    const methodNames = [["baseline_shortest", "最短路径基线"], ["ours", "GraphRAG JoinPathFinder"]];
    const metricRows = [
      ["含金边题数", "cases_with_gold_edges", value => formatNumber(value)],
      ["金边召回（micro）", "gold_edge_recall_micro", value => formatPercent(value)],
      ["金边命中 / 总数", "gold_edge_hits", (value, row) => `${formatNumber(value)} / ${formatNumber(row.gold_edges_total)}`],
      ["路径找到率", "path_found_rate", value => formatPercent(value)],
      ["歧义标记率", "ambiguity_flag_rate", value => formatPercent(value)],
      ["Top-1 边召回", "top1_edge_recall_micro", value => formatPercent(value)],
      ["Top-1 完整率", "top1_complete_rate", value => formatPercent(value)],
      ["复合外键完整率", "composite_fk_complete_rate", value => value == null ? "N/A" : formatPercent(value)],
      ["无关路径总数", "irrelevant_paths_total", value => formatNumber(value)],
    ];
    const rowsData = metricRows.map(([label, key, formatter]) => [
      label,
      ...methodNames.map(([name]) => formatter(data.methods?.[name]?.[key], data.methods?.[name] || {})),
    ]);
    target.appendChild(buildSimpleTable(["指标", "最短路径基线", "GraphRAG"], rowsData));
    [...(data.notes || []), ...(data.fairness || [])].forEach(text => {
      const note = document.createElement("p");
      note.className = "tie-note";
      note.textContent = text;
      target.appendChild(note);
    });
    const cases = data.cases || [];
    if (cases.length) {
      const grouped = new Map();
      cases.forEach(row => {
        if (!grouped.has(row.case_id)) grouped.set(row.case_id, []);
        grouped.get(row.case_id).push(row);
      });
      const caseRows = [];
      grouped.forEach((rowsList, caseId) => {
        rowsList.forEach(row => {
          caseRows.push([
            row.case_id, row.method === "ours" ? "GraphRAG" : "最短路径",
            row.category || "-", formatNumber(row.gold_edge_hits),
            formatPercent(row.gold_edge_recall ?? null), formatNumber(row.path_count),
            row.ambiguous_flagged ? "是" : "否",
          ]);
        });
      });
      target.appendChild(buildSimpleTable(
        ["题目", "方法", "类别", "金边命中", "该题金边召回", "路径数", "歧义标记"], caseRows));
    }
    target.scrollIntoView({behavior: "smooth", block: "start"});
    return;
  }

  if (data.kind === "agent") {
    target.appendChild(detailHeading(data.title, `模型：${data.model || "未记录"} · 模式：${data.evaluation_mode || "full"} · 在线评测`));
    const grid = document.createElement("div");
    grid.className = "kpi-row";
    [["通过率", formatPercent(data.pass_rate)], ["通过题数", `${data.passed_count ?? "?"}/${data.sample_count ?? "?"}`],
     ["平均耗时", formatSeconds(data.mean_item_seconds)], ["Token", formatNumber(data.total_tokens)],
    ].forEach(([label, value]) => {
      const card = document.createElement("div");
      card.className = "kpi-card";
      const labelEl = document.createElement("span");
      labelEl.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = value;
      card.append(labelEl, strong);
      grid.appendChild(card);
    });
    target.appendChild(grid);
    const failures = (data.cases || []).filter(row => !row.passed);
    target.appendChild(buildSimpleTable(
      ["未通过案例", "意图", "状态", "原因"],
      failures.length ? failures.map(row => [row.id, row.intent || "-", row.status || "-", row.error || "检查未通过"])
        : [["（无）", "-", "-", "无功能性失败"]]));
    target.scrollIntoView({behavior: "smooth", block: "start"});
    return;
  }

  if (data.kind === "retrieval") {
    target.appendChild(detailHeading(data.title, "离线指标检索对照（不消耗 LLM 配额）；embedding 调用仅用于 hybrid 向量通道。"));
    const rowsData = Object.entries(data.variants || {}).map(([name, metrics]) => [
      name === "hybrid" ? "混合 RAG（关键词 + 向量）" : "关键词基线",
      formatNumber(metrics.case_count), formatPercent(metrics.recall_at_k),
      formatPercent(metrics.mrr), formatSeconds(metrics.mean_seconds),
    ]);
    target.appendChild(buildSimpleTable(["方案", "题数", "Recall@3", "MRR", "平均耗时"], rowsData));
    const usage = data.embedding_usage || {};
    if (usage.total_tokens) {
      const note = document.createElement("p");
      note.className = "tie-note";
      note.textContent = `hybrid 向量通道消耗：${usage.responses} 次调用 / ${formatNumber(usage.total_tokens)} embedding tokens / ${formatSeconds(usage.elapsed_seconds)}`;
      target.appendChild(note);
    }
    target.scrollIntoView({behavior: "smooth", block: "start"});
    return;
  }

  if (data.kind === "ablation") {
    target.appendChild(detailHeading(data.title, "在线 SQL 评测（消耗配额）；三组消融对照同一批题目。"));
    const rowsData = Object.entries(data.variants || {}).map(([name, metrics]) => [
      VARIANT_NAMES[name] || name, `${metrics.correct_count}/${metrics.sample_count}`,
      formatPercent(metrics.execution_accuracy), formatPercent(metrics.strict_multiset_accuracy),
      `${metrics.executable_count}/${metrics.sample_count}`, formatSeconds(metrics.batch_wall_seconds),
      formatNumber(metrics.total_tokens),
    ]);
    target.appendChild(buildSimpleTable(
      ["方案", "正确", "集合式 EX", "严格 EX", "可执行", "耗时", "Token"], rowsData));
    target.scrollIntoView({behavior: "smooth", block: "start"});
    return;
  }

  target.appendChild(detailHeading(data.title, "历史基线评测产物。"));
  const grid = document.createElement("div");
  grid.className = "kpi-row";
  [["集合式 EX", formatPercent(data.execution_accuracy)], ["严格 EX", formatPercent(data.strict_multiset_accuracy)],
   ["正确题数", `${data.correct_count ?? "?"}/${data.sample_count ?? "?"}`], ["平均耗时", formatSeconds(data.mean_item_seconds)],
   ["Token", formatNumber(data.total_tokens)],
  ].forEach(([label, value]) => {
    const card = document.createElement("div");
    card.className = "kpi-card";
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = value;
    card.append(labelEl, strong);
    grid.appendChild(card);
  });
  target.appendChild(grid);
  target.scrollIntoView({behavior: "smooth", block: "start"});
}

/* ==================================================================== */
/* 13. 输入框 / 初始化                                                   */
/* ==================================================================== */
function autoGrow() {
  const input = $("#question");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
}

/* 只有用户主动「新建对话」或切换数据源时才换会话，并清掉待恢复记录。 */
function startNewConversation() {
  conversationId = newConversationId();
  Recovery.writeConversationId(sessionStorage, conversationId);
  Recovery.clearRecord(sessionStorage);
  closeSource();
}

function switchPanel(tabId, updateHash = true) {
  document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item.dataset.tab === tabId));
  document.querySelectorAll(".panel").forEach(panel => panel.classList.remove("active"));
  $(`#${tabId}-panel`).classList.add("active");
  if (tabId === "evaluation") loadEvaluations();
  if (tabId === "knowledge") openKnowledgePanel();
  // 支持 #knowledge / #evaluation 深链接，便于直接分享某个页面。
  if (updateHash) {
    try {
      const target = tabId === "query" ? "" : tabId;
      if (location.hash !== `#${target}`) history.replaceState(null, "", `#${target}`);
    } catch {}
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  try { await loadDatabases(); }
  catch (error) {
    setService("配置错误", "error");
    $("#messages").textContent = error.message;
  }
  if (databases.length) {
    try { await restoreActiveTask(); } catch (error) { setService("恢复失败", "error"); }
  }
  await loadConversations();

  $("#database").addEventListener("change", () => { startNewConversation(); updateDatabaseUI(); });
  $("#new-chat").addEventListener("click", () => {
    startNewConversation();
    $("#evidence").value = "";
    renderWelcome();
    loadConversations();
    $("#question").focus();
  });
  $("#refresh-conversations").addEventListener("click", loadConversations);
  $("#conversation-search").addEventListener("input", () => renderConversationList(latestConversations));
  $("#query-form").addEventListener("submit", submitQuery);
  $("#question").addEventListener("input", autoGrow);
  $("#question").addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#query-form").requestSubmit(); }
  });
  $("#refresh-evals").addEventListener("click", () => loadEvaluations());
  $("#refresh-knowledge").addEventListener("click", () => openKnowledgePanel(true));
  document.querySelectorAll(".kb-tab").forEach(tab => tab.addEventListener("click", () => renderKnowledgeTab(tab.dataset.kbtab)));

  const toggleBtn = $("#toggle-sidebar");
  if (toggleBtn) {
    const shell = document.querySelector(".chat-shell");
    const applySidebar = collapsed => shell.classList.toggle("sidebar-collapsed", collapsed);
    try { applySidebar(localStorage.getItem("sidebar-collapsed") === "1"); } catch {}
    toggleBtn.addEventListener("click", () => {
      const next = !shell.classList.contains("sidebar-collapsed");
      applySidebar(next);
      try { localStorage.setItem("sidebar-collapsed", next ? "1" : "0"); } catch {}
    });
  }

  document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => switchPanel(tab.dataset.tab)));

  // 初始 hash 深链接：#knowledge / #evaluation 直接落到对应页面。
  const initialTab = location.hash.replace("#", "");
  if (initialTab === "knowledge" || initialTab === "evaluation") switchPanel(initialTab, false);
});
