const STAGES = [
  ["agent_planning", "LLM 规划"], ["agent_tool", "工具调用"],
  ["agent_orchestration", "规划与编排"],
  ["value_retrieval", "值检索"], ["few_shot_preparation", "Few-shot"],
  ["schema_linking", "Schema"], ["sql_generation", "SQL 生成"],
  ["sql_revision", "SQL 修订"], ["sql_selection", "SQL 选择"],
  ["execution", "安全执行"], ["visualization", "可视化"],
];
const INTENT_NAMES = {query: "查数", ranking: "排名", trend: "趋势", proportion: "占比", chat: "闲聊"};
const VARIANT_NAMES = {generation_only: "仅生成", generation_revision: "生成＋修订", full_pipeline: "完整流程"};
const $ = selector => document.querySelector(selector);
const formatNumber = value => value == null ? "—" : new Intl.NumberFormat("zh-CN").format(value);
const formatSeconds = value => value == null ? "—" : `${Number(value).toFixed(value < 10 ? 2 : 1)} 秒`;
const formatPercent = value => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const valueText = value => typeof value === "object" ? JSON.stringify(value) : String(value ?? "");
const Recovery = window.DeepEyeRecovery;
let databases = [];
let activeRun = null;
let activeSource = null;
let timer = null;
let reconnects = 0;
let recovering = false;
// 刷新后必须复用同一个会话 ID，否则「做成柱状图」会掉到新会话里读不到上一轮结果。
let conversationId = Recovery.readConversationId(sessionStorage) || newConversationId();
Recovery.writeConversationId(sessionStorage, conversationId);

function newConversationId() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : `conv-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// The Agent writes its answer in Markdown (headings, bold, lists, tables), but
// the bubble used textContent, so `## 标题`, `**加粗**` and 60 个 `|` 全部当成字面
// 字符显示，换行又被 HTML 折叠成一坨。这里按 DOM 节点逐段构建，不用 innerHTML，
// 模型输出里的任何尖括号或伪标签都只能是文本。
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
  // 粗体用非贪婪匹配，允许内部夹行内代码；粗体之外的内容再单独解析代码。
  // 刻意不支持斜体 `*文字*`：结果里全是 `SUM(a*b)`、`1 - discount_rate` 这类乘法，
  // 当斜体解析会把公式咬掉一段。
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

function setService(text, state = "neutral") {
  const element = $("#service-state");
  element.lastChild.textContent = text;
  element.className = `status-pill ${state}`;
}

function selectedDatabase() {
  return databases.find(item => item.id === $("#database").value);
}

function examplesFor(db) {
  return (db.examples || []).map(example => ({
    ...example,
    database_id: db.id,
    database_name: db.name,
    domain_name: db.domain_name,
  }));
}

function currentExamples() {
  const selected = selectedDatabase();
  if (selected) return examplesFor(selected);
  return databases.flatMap(db => examplesFor(db).slice(0, 3));
}

async function loadDatabases() {
  const response = await fetch("/api/databases");
  if (!response.ok) throw new Error("无法读取数据库列表");
  databases = (await response.json()).databases;
  const select = $("#database");
  select.replaceChildren(
    new Option("自动识别业务域（推荐）", "auto"),
    ...databases.map(db => new Option(db.name, db.id)),
  );
  updateDatabaseUI();
}

function updateDatabaseUI() {
  const db = selectedDatabase();
  const summary = $("#source-summary");
  summary.replaceChildren();
  if (db) {
    $("#conversation-title").textContent = db.domain === "hr" ? "HR 人力数据分析" : "企业销售数据分析";
    $("#database-description").textContent = `已手动锁定 ${db.name}，自动路由不会覆盖`;
    $("#question").placeholder = db.domain === "hr"
      ? "例如：最近一年各部门离职率是多少？"
      : "例如：按月统计 2026 年销售额趋势";
    const badge = document.createElement("span");
    badge.className = `domain-pill ${db.domain || "other"}`;
    badge.textContent = db.domain_name || "业务域";
    const detail = document.createElement("p");
    detail.textContent = `${db.description || "只读分析数据源"}${db.table_count ? ` · ${db.table_count} 张表` : ""}`;
    summary.append(badge, detail);
  } else {
    $("#conversation-title").textContent = "跨业务域数据分析";
    $("#database-description").textContent = "Agent 将先识别业务域，再检索对应指标与 Schema";
    $("#question").placeholder = "可提问销售或 HR 数据，例如：各部门在职人数是多少？";
    databases.forEach(item => {
      const badge = document.createElement("span");
      badge.className = `domain-pill ${item.domain || "other"}`;
      badge.textContent = `${item.domain_name || item.name} · ${item.table_count || "?"} 表`;
      summary.appendChild(badge);
    });
  }
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
  renderWelcome();
}

function renderWelcome() {
  const db = selectedDatabase();
  const messages = $("#messages");
  messages.replaceChildren();
  const row = document.createElement("article");
  row.className = "message-row assistant welcome-message";
  row.innerHTML = `<div class="avatar">D</div><div class="message-content"><div class="message-author">DeepEye Agent</div><div class="bubble assistant-bubble"><h2>${db ? db.name : "销售 + HR 跨业务域分析"}</h2><p>${db ? `当前已锁定${db.domain_name || "业务域"}。` : "我会先判断问题属于销售还是 HR，再使用对应数据库和指标口径。"} LLM 将继续规划 Schema、Text-to-SQL、只读执行与可视化。</p><div class="welcome-domains"></div><div class="suggestion-grid"></div></div></div>`;
  const domains = row.querySelector(".welcome-domains");
  (db ? [db] : databases).forEach(item => {
    const badge = document.createElement("span");
    badge.className = `domain-pill ${item.domain || "other"}`;
    badge.textContent = `${item.domain_name || item.name} · ${item.description || "只读分析"}`;
    domains.appendChild(badge);
  });
  const grid = row.querySelector(".suggestion-grid");
  currentExamples().slice(0, 6).forEach(example => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion";
    button.textContent = `${example.domain_name ? `${example.domain_name}｜` : ""}${example.question}`;
    button.addEventListener("click", () => askExample(example));
    grid.appendChild(button);
  });
  messages.appendChild(row);
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

function appendProgressMessage() {
  const row = document.createElement("article");
  row.className = "message-row assistant";
  row.innerHTML = `<div class="avatar">D</div><div class="message-content"><div class="message-author">DeepEye Agent</div><div class="bubble assistant-bubble"><div class="thinking-card"><div class="thinking-head"><span class="thinking-spinner"></span><strong>正在分析</strong><span class="elapsed-live">0s</span></div><p class="request-meta">正在提交问题…</p><div class="stage-track"></div></div></div></div>`;
  const stageTrack = row.querySelector(".stage-track");
  const chips = {};
  STAGES.forEach(([id, name]) => {
    const chip = document.createElement("span");
    chip.className = "stage-chip";
    chip.textContent = name;
    stageTrack.appendChild(chip);
    chips[id] = chip;
  });
  $("#messages").appendChild(row);
  scrollToLatest();
  return {row, bubble: row.querySelector(".assistant-bubble"), meta: row.querySelector(".request-meta"), elapsed: row.querySelector(".elapsed-live"), chips, startedAt: Date.now()};
}

function scrollToLatest() {
  const messages = $("#messages");
  requestAnimationFrame(() => messages.scrollTop = messages.scrollHeight);
}

function setBusy(busy) {
  $("#run-button").disabled = busy;
  $("#database").disabled = busy;
  $("#new-chat").disabled = busy;
  // 恢复期间连推荐问题一起禁用，否则点一下就会在旧任务上再排一个新任务。
  document.querySelectorAll("#example-list button, .suggestion").forEach(button => {
    button.disabled = busy;
  });
}

function startClock(run) {
  clearInterval(timer);
  timer = setInterval(() => run.elapsed.textContent = `${Math.floor((Date.now() - run.startedAt) / 1000)}s`, 1000);
}

async function submitQuery(event) {
  event.preventDefault();
  // 一个标签页同一时刻只允许一个活动任务：双击、Enter 连击、恢复期间点推荐问题都走这里。
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
  setService("正在分析", "running");
  startClock(run);
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
    run.requestId = data.request_id;
    run.meta.textContent = `请求 ${data.request_id} 已进入分析队列`;
    // 拿到 request_id 才写记录：提交失败时不该留下一条指向不存在任务的记录。
    persistRun(run);
    followEvents(run, 0);
  } catch (error) {
    finishWithTransportError(run, error.message);
  }
}

/* 记录只写恢复页面必需的非敏感字段；Recovery.writeRecord 还会再做一次白名单过滤。 */
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

/*
 * 页面卸载时只关浏览器这一侧的连接，后端任务继续跑——刷新不取消任务，
 * 靠的就是这一点。下次打开用同一个 request_id 重新连上来。
 */
window.addEventListener("pagehide", closeSource);

function followEvents(run, after = 0) {
  closeSource();  // 同一页面只保留一个 EventSource
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
    }
    const chip = run.chips[event.stage];
    if (chip) {
      chip.className = `stage-chip ${event.status}`;
      if (event.status === "completed") chip.textContent = `${chip.textContent.replace(" ✓", "")} ✓`;
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
      // 只有明确 404 才认为记录失效；网络错误不能当成任务失败。
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
    // 用游标续传，避免重复渲染已经播过的阶段事件。
    return setTimeout(() => followEvents(run, run.sequence || 0), 1200);
  }
  renderAssistantResult(run, task);
}

function finishRun(success) {
  clearInterval(timer);
  closeSource();
  recovering = false;
  setBusy(false);
  setService(success ? "分析完成" : "分析失败", success ? "success" : "error");
  activeRun = null;
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
  finishRun(false);
}

function finishWithTransportError(run, message) {
  finishWithCard(run, "暂时无法完成分析", message);
}

function finishWithDiscard(run, message) {
  finishWithCard(run, "任务记录已失效", message);
}

/*
 * 记录失效（404 / 未知状态）：清记录、回欢迎页、把问题填回输入框。
 * 不自动重提，也不把整页留在一张错误卡片上——用户应该看到干净的起始状态。
 */
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

/* 按恢复计划落地：活跃任务重连事件流，终态任务直接复用同一套渲染。 */
function applyRestorePlan(run, task, plan) {
  if (plan.action === Recovery.ACTIONS.resume_events) {
    setService(plan.headline, "running");
    run.meta.textContent = plan.message;
    startClock(run);
    // after=0 重放已播过的阶段事件，进度卡片才能对上；终态事件会触发 loadQueryResult。
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
    // 保留问题文本，用户点一下就能重发，不用重新敲。
    $("#question").value = run.question || "";
    autoGrow();
    return;
  }
  abandonRestore(plan.message, run.question);
}

/*
 * 页面加载后的恢复入口：先查后端状态，再决定是重连、渲染结果还是提示失效。
 * 全程不会调用 POST /api/chat —— 刷新永远不产生第二个任务。
 */
async function restoreActiveTask() {
  // 同标签页刷新：本地有记录，直接按 request_id 恢复。
  // 新标签页：sessionStorage 是空的，退一步问服务端“现在有任务在跑吗”。
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
    // 网络错误不等于任务失败：保留记录，稍后刷新仍可恢复。
    finishWithTransportError(run, `暂时无法查询任务状态：${error.message}`);
    return true;
  }
  applyRestorePlan(run, task, Recovery.planForStatus(task.status));
  return true;
}

/*
 * 新标签页接手：服务端串行队列同一时刻只有一个任务在跑，
 * 直接问它要当前任务，比在本地猜一个 request_id 可靠。
 * 查询失败只当作“没有任务”，不能挡住欢迎页。
 */
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

function renderAssistantResult(run, task) {
  const result = task.result || {};
  const artifacts = result.artifacts || {};
  const success = ["success", "empty_result"].includes(result.status);
  run.bubble.replaceChildren();
  if (!success) run.bubble.classList.add("error-card");

  const header = document.createElement("div");
  header.className = "result-header";
  const title = document.createElement("strong");
  title.textContent = success ? "分析完成" : "分析未完成";
  const intent = document.createElement("span");
  intent.className = "intent-badge";
  intent.textContent = INTENT_NAMES[result.intent] || result.intent || "通用查询";
  const source = document.createElement("span");
  source.className = "intent-badge source-badge";
  source.textContent = databases.find(item => item.id === result.database_id)?.name || result.database_id || "未选择数据源";
  header.append(title, source, intent);
  run.bubble.appendChild(header);

  // div, not p: the answer now contains block children (tables, lists).
  const answer = document.createElement("div");
  answer.className = `answer-text${success ? "" : " error-text"}`;
  renderMarkdown(answer, result.answer || result.error || (success ? "查询已经完成。" : "请稍后重试。"));
  run.bubble.appendChild(answer);
  if (!success) {
    const stage = document.createElement("p");
    stage.className = "answer-text error-text";
    stage.textContent = `失败阶段：${result.failed_stage || "unknown"}`;
    run.bubble.appendChild(stage);
    finishRun(false);
    return;
  }

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
    run.bubble.appendChild(buildMetrics(result, artifacts));
    if (result.visualization && result.visualization.type !== "table") run.bubble.appendChild(buildChart(result.visualization));
    const tableColumns = (result.columns || []).length
      ? result.columns : (result.visualization?.columns || []);
    const tableRows = (result.rows || []).length
      ? result.rows : (result.visualization?.rows || []);
    if (tableColumns.length) run.bubble.appendChild(buildTable(tableColumns, tableRows, result.truncated));
    run.bubble.appendChild(buildTechnicalDetails(result, artifacts));
  }
  finishRun(true);
}

function buildMetrics(result, artifacts) {
  const usage = result.usage || {}, agentUsage = result.agent_usage || {};
  const values = [
    // The live timer above is client-measured and includes Agent planning, so
    // the final card must use the same scope or it ends up reading lower than
    // the spinner it just replaced.
    ["总耗时", formatSeconds(result.agent_elapsed_seconds ?? result.elapsed_seconds)],
    ["Token", formatNumber((usage.chat?.total_tokens || 0) + (usage.embedding?.total_tokens || 0) + (agentUsage.total_tokens || 0))],
    ["Agent 决策", formatNumber(result.model_calls || 0)],
    ["结果行数", formatNumber(result.rows?.length || 0)],
  ];
  const grid = document.createElement("div");
  grid.className = "metric-grid";
  values.forEach(([label, value]) => {
    const card = document.createElement("div"); card.className = "metric";
    const name = document.createElement("span"); name.textContent = label;
    const strong = document.createElement("strong"); strong.textContent = value;
    card.append(name, strong); grid.appendChild(card);
  });
  return grid;
}

function buildChart(chart) {
  const card = document.createElement("section");
  card.className = "chart-card";
  const head = document.createElement("div"); head.className = "result-subhead";
  const title = document.createElement("strong"); title.textContent = chart.title || "数据可视化";
  const type = document.createElement("span"); type.textContent = {bar:"柱状图",line:"折线图",pie:"饼图",metric:"指标卡"}[chart.type] || chart.type;
  head.append(title, type);
  const container = document.createElement("div"); container.className = "chart-container";
  card.append(head, container);
  renderVisualization(container, chart);
  return card;
}

function buildTable(columns, rows, truncated) {
  const card = document.createElement("section"); card.className = "table-card";
  const head = document.createElement("div"); head.className = "result-subhead";
  const title = document.createElement("strong"); title.textContent = "查询结果";
  const note = document.createElement("span"); note.textContent = truncated ? `前 ${rows.length} 行` : `${rows.length} 行`;
  head.append(title, note);
  const wrap = document.createElement("div"); wrap.className = "table-wrap";
  const table = document.createElement("table");
  const thead = document.createElement("thead"), headerRow = document.createElement("tr");
  columns.forEach(column => { const th = document.createElement("th"); th.textContent = column; headerRow.appendChild(th); });
  thead.appendChild(headerRow); table.appendChild(thead);
  const tbody = document.createElement("tbody");
  rows.forEach(row => { const tr = document.createElement("tr"); row.forEach(value => { const td = document.createElement("td"); td.textContent = valueText(value); tr.appendChild(td); }); tbody.appendChild(tr); });
  table.appendChild(tbody); wrap.appendChild(table); card.append(head, wrap);
  return card;
}

function buildTechnicalDetails(result, artifacts) {
  const details = document.createElement("details"); details.className = "technical-details";
  const summary = document.createElement("summary"); summary.textContent = "查看 SQL、Schema 与执行详情";
  const grid = document.createElement("div"); grid.className = "technical-grid";
  const sql = document.createElement("div"); sql.className = "technical-block";
  const sqlTitle = document.createElement("strong"); sqlTitle.textContent = "最终 SQL";
  const pre = document.createElement("pre"); pre.textContent = artifacts.final_sql || result.sql || "未生成";
  sql.append(sqlTitle, pre);
  const timing = document.createElement("div"); timing.className = "technical-block";
  const timingTitle = document.createElement("strong"); timingTitle.textContent = "阶段耗时";
  const stageTimes = document.createElement("div"); stageTimes.className = "stage-times";
  const times = result.stage_seconds || {}, max = Math.max(...Object.values(times), 1);
  STAGES.filter(([id]) => times[id] != null).forEach(([id, name]) => {
    const row = document.createElement("div"); row.className = "time-row";
    const label = document.createElement("span"); label.textContent = name;
    const bar = document.createElement("span"); bar.className = "bar";
    const fill = document.createElement("i"); fill.style.width = `${Math.max(2, times[id] / max * 100)}%`; bar.appendChild(fill);
    const seconds = document.createElement("span"); seconds.textContent = formatSeconds(times[id]);
    row.append(label, bar, seconds); stageTimes.appendChild(row);
  });
  timing.append(timingTitle, stageTimes); grid.append(sql, timing);
  const trace = document.createElement("div"); trace.className = "technical-block";
  const traceTitle = document.createElement("strong"); traceTitle.textContent = "工具调用轨迹";
  const traceList = document.createElement("div"); traceList.className = "tool-trace";
  (result.tool_trace || []).forEach((item, index) => { const row=document.createElement("div"); row.textContent=`${index+1}. ${item.tool} · ${item.status}`; traceList.appendChild(row); });
  if (!(result.tool_trace || []).length) traceList.textContent = "本轮没有工具调用";
  trace.append(traceTitle, traceList); grid.appendChild(trace); details.append(summary, grid);
  return details;
}

function svgNode(name, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function renderVisualization(container, chart) {
  if (chart.type === "metric") {
    const grid = document.createElement("div"); grid.className = "chart-metrics";
    (chart.items || []).forEach(item => { const node = document.createElement("div"); node.className = "chart-metric"; const label=document.createElement("span"); label.textContent=item.name; const value=document.createElement("strong"); value.textContent=formatNumber(item.value); node.append(label,value); grid.appendChild(node); });
    container.appendChild(grid); return;
  }
  if (chart.type === "pie") { renderPie(container, chart.items || []); return; }
  renderCartesian(container, chart);
}

function renderCartesian(container, chart) {
  const labels = chart.labels || [], series = chart.series || [];
  const width=760, height=250, left=58, right=18, top=30, bottom=55;
  const svg=svgNode("svg",{viewBox:`0 0 ${width} ${height}`,role:"img"});
  const values=series.flatMap(item=>item.values||[]).map(Number).filter(Number.isFinite);
  const max=Math.max(...values,1), plotW=width-left-right, plotH=height-top-bottom;
  svg.appendChild(svgNode("line",{x1:left,y1:top,x2:left,y2:top+plotH,class:"chart-axis"}));
  svg.appendChild(svgNode("line",{x1:left,y1:top+plotH,x2:left+plotW,y2:top+plotH,class:"chart-axis"}));
  const count=Math.max(labels.length,1), step=plotW/count;
  if(chart.type==="bar") (series[0]?.values||[]).forEach((value,index)=>{
    const num=Number(value);
    const h=Number.isFinite(num)?num/max*plotH:0, barY=top+plotH-h;
    svg.appendChild(svgNode("rect",{x:left+index*step+step*.18,y:barY,width:step*.64,height:h,rx:4,class:"chart-bar"}));
    if(Number.isFinite(num)){const valueNode=svgNode("text",{x:left+index*step+step/2,y:barY-6,class:"chart-value","text-anchor":"middle"});valueNode.textContent=formatNumber(num);svg.appendChild(valueNode);}
  });
  else series.forEach((item,s)=>{
    const points=(item.values||[]).map((value,index)=>`${left+index*step+step/2},${top+plotH-Number(value)/max*plotH}`).join(" ");
    svg.appendChild(svgNode("polyline",{points,fill:"none",class:`chart-line series-${s}`}));
    (item.values||[]).forEach((value,index)=>{
      const num=Number(value);
      if(!Number.isFinite(num))return;
      const valueNode=svgNode("text",{x:left+index*step+step/2,y:top+plotH-num/max*plotH-8,class:`chart-value series-${s}`,"text-anchor":"middle"});
      valueNode.textContent=formatNumber(num);
      svg.appendChild(valueNode);
    });
  });
  labels.forEach((label,index)=>{const text=svgNode("text",{x:left+index*step+step/2,y:top+plotH+21,class:"chart-label","text-anchor":"middle"});text.textContent=String(label).slice(0,9);svg.appendChild(text);});
  container.appendChild(svg);
}

function renderPie(container, items) {
  const total=items.reduce((sum,item)=>sum+Number(item.value||0),0)||1, colors=["#3867e8","#16a7b7","#8b5cf6","#f59e0b","#ef5b5b","#38a169"];
  let angle=0; const stops=[];
  items.forEach((item,index)=>{const next=angle+Number(item.value||0)/total*360;stops.push(`${colors[index%colors.length]} ${angle}deg ${next}deg`);angle=next;});
  const wrap=document.createElement("div");wrap.className="pie-layout";
  const pie=document.createElement("div");pie.className="pie-chart";pie.style.background=`conic-gradient(${stops.join(",")})`;wrap.appendChild(pie);
  const legend=document.createElement("div");legend.className="pie-legend";
  items.forEach((item,index)=>{const row=document.createElement("div"),dot=document.createElement("i"),name=document.createElement("span"),pct=document.createElement("strong");dot.style.background=colors[index%colors.length];name.textContent=item.name;pct.textContent=`${(Number(item.value||0)/total*100).toFixed(1)}%`;row.append(dot,name,pct);legend.appendChild(row);});
  wrap.appendChild(legend);container.appendChild(wrap);
}

function autoGrow() {
  const input=$("#question"); input.style.height="auto"; input.style.height=`${Math.min(input.scrollHeight,140)}px`;
}

async function loadEvaluations() {
  const target=$("#evaluation-list"); target.innerHTML=`<div>正在读取报告…</div>`;
  try {
    const response=await fetch("/api/evaluations"), data=await response.json();
    if(!data.evaluations.length){target.innerHTML=`<div class="eval-card"><h3>暂无评测报告</h3></div>`;return;}
    target.innerHTML=data.evaluations.map(item=>{
      let score, label;
      if(item.kind==="agent") { score=item.pass_rate; label="Agent 通过率"; }
      else if(item.kind==="retrieval") { score=item.variants?.hybrid?.recall_at_k; label="混合 RAG Recall@3"; }
      else { score=item.kind==="ablation"?item.variants?.full_pipeline?.execution_accuracy:item.execution_accuracy; label="完整流程集合式 EX"; }
      return `<article class="eval-card" data-eval="${item.id}"><h3>${item.title}</h3><div>${item.id} · ${item.sample_count||0} 题</div><div class="eval-kpi">${formatPercent(score)}</div><div>${label}</div></article>`;
    }).join("");
    target.querySelectorAll("[data-eval]").forEach(card=>card.addEventListener("click",()=>loadEvaluationDetail(card.dataset.eval)));
  } catch(error) { target.textContent=error.message; }
}

async function loadEvaluationDetail(id) {
  const response=await fetch(`/api/evaluations/${encodeURIComponent(id)}`), data=await response.json(), target=$("#evaluation-detail");
  target.classList.remove("hidden");
  if(data.kind==="agent"){
    const failures=(data.cases||[]).filter(row=>!row.passed);
    const failureRows=failures.length?failures.map(row=>`<tr><td>${row.id}</td><td>${row.intent||"-"}</td><td>${row.status||"-"}</td><td>${row.error||"检查未通过"}</td></tr>`).join(""):`<tr><td colspan="4">无功能性失败</td></tr>`;
    target.innerHTML=`<div class="section-heading"><h2>${data.title}</h2><p>${data.model||"模型未记录"}</p></div><div class="metric-grid"><div class="metric"><span>通过率</span><strong>${formatPercent(data.pass_rate)}</strong></div><div class="metric"><span>通过题数</span><strong>${data.passed_count}/${data.sample_count}</strong></div><div class="metric"><span>平均耗时</span><strong>${formatSeconds(data.mean_item_seconds)}</strong></div><div class="metric"><span>Token</span><strong>${formatNumber(data.total_tokens)}</strong></div></div><div class="table-wrap"><table><thead><tr><th>未通过案例</th><th>意图</th><th>状态</th><th>原因</th></tr></thead><tbody>${failureRows}</tbody></table></div>`;
  } else if(data.kind==="retrieval"){
    const rows=Object.entries(data.variants||{}).map(([name,m])=>`<tr><td>${name==="hybrid"?"混合 RAG":"关键词基线"}</td><td>${m.case_count}</td><td>${formatPercent(m.recall_at_k)}</td><td>${formatPercent(m.mrr)}</td><td>${formatSeconds(m.mean_seconds)}</td></tr>`).join("");
    target.innerHTML=`<div class="section-heading"><h2>${data.title}</h2></div><div class="table-wrap"><table><thead><tr><th>方案</th><th>题数</th><th>Recall@3</th><th>MRR</th><th>平均耗时</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  } else if(data.kind==="ablation"){
    const rows=Object.entries(data.variants).map(([name,m])=>`<tr><td>${VARIANT_NAMES[name]||name}</td><td>${m.correct_count}/${m.sample_count}</td><td>${formatPercent(m.execution_accuracy)}</td><td>${formatPercent(m.strict_multiset_accuracy)}</td><td>${m.executable_count}/${m.sample_count}</td><td>${formatSeconds(m.batch_wall_seconds)}</td><td>${formatNumber(m.total_tokens)}</td></tr>`).join("");
    target.innerHTML=`<div class="section-heading"><h2>${data.title}</h2></div><div class="eval-table"><table><thead><tr><th>方案</th><th>正确</th><th>集合式 EX</th><th>严格 EX</th><th>可执行</th><th>耗时</th><th>Token</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  } else target.innerHTML=`<div class="section-heading"><h2>${data.title}</h2></div><div class="metric-grid"><div class="metric"><span>集合式 EX</span><strong>${formatPercent(data.execution_accuracy)}</strong></div><div class="metric"><span>严格 EX</span><strong>${formatPercent(data.strict_multiset_accuracy)}</strong></div><div class="metric"><span>正确题数</span><strong>${data.correct_count}/${data.sample_count}</strong></div><div class="metric"><span>平均耗时</span><strong>${formatSeconds(data.mean_item_seconds)}</strong></div><div class="metric"><span>Token</span><strong>${formatNumber(data.total_tokens)}</strong></div></div>`;
  target.scrollIntoView({behavior:"smooth",block:"start"});
}

/* 只有用户主动「新建对话」或切换数据源时才换会话，并清掉待恢复记录。 */
function startNewConversation() {
  conversationId = newConversationId();
  Recovery.writeConversationId(sessionStorage, conversationId);
  Recovery.clearRecord(sessionStorage);
  closeSource();
}

document.addEventListener("DOMContentLoaded", async () => {
  try { await loadDatabases(); } catch(error) { setService("配置错误","error"); $("#messages").textContent=error.message; }
  if (databases.length) {
    try { await restoreActiveTask(); } catch (error) { setService("恢复失败", "error"); }
  }
  $("#database").addEventListener("change", () => { startNewConversation(); updateDatabaseUI(); });
  $("#new-chat").addEventListener("click", () => { startNewConversation(); $("#evidence").value=""; renderWelcome(); $("#question").focus(); });
  $("#query-form").addEventListener("submit", submitQuery);
  $("#question").addEventListener("input", autoGrow);
  $("#question").addEventListener("keydown", event => { if(event.key==="Enter"&&!event.shiftKey){event.preventDefault();$("#query-form").requestSubmit();} });
  $("#refresh-evals").addEventListener("click", loadEvaluations);
  document.querySelectorAll(".tab").forEach(tab=>tab.addEventListener("click",()=>{
    document.querySelectorAll(".tab").forEach(item=>item.classList.toggle("active",item===tab));
    document.querySelectorAll(".panel").forEach(panel=>panel.classList.remove("active"));
    $(`#${tab.dataset.tab}-panel`).classList.add("active");
    if(tab.dataset.tab==="evaluation")loadEvaluations();
  }));
});
