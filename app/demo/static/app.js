const STAGES = [
  ["intent_classification", "意图分类"],
  ["business_context", "业务口径 RAG"],
  ["value_retrieval", "值检索"],
  ["few_shot_preparation", "Few-shot 准备"],
  ["schema_linking", "Schema Linking"],
  ["sql_generation", "SQL 生成"],
  ["sql_revision", "SQL 修订"],
  ["sql_selection", "SQL 选择"],
  ["execution", "只读执行"],
  ["visualization", "结果可视化"],
];
const INTENT_NAMES = {query: "查数", ranking: "排名", trend: "趋势", proportion: "占比", chat: "闲聊"};
const VARIANT_NAMES = {generation_only: "仅生成", generation_revision: "生成＋修订", full_pipeline: "完整流程"};
let databases = [];
let stageState = {};
let timer = null;
let startedAt = null;
let reconnects = 0;

const $ = (selector) => document.querySelector(selector);
const formatNumber = (value) => value == null ? "—" : new Intl.NumberFormat("zh-CN").format(value);
const formatSeconds = (value) => value == null ? "—" : `${Number(value).toFixed(value < 10 ? 2 : 1)} 秒`;
const formatPercent = (value) => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const valueText = (value) => typeof value === "object" ? JSON.stringify(value) : String(value ?? "");

function setBadge(element, text, state) {
  element.textContent = text;
  element.className = `badge ${state}`;
}

function initializeTimeline() {
  stageState = Object.fromEntries(STAGES.map(([id]) => [id, {status: "waiting", seconds: null}]));
  renderTimeline();
}

function renderTimeline() {
  $("#timeline").innerHTML = STAGES.map(([id, name], index) => {
    const state = stageState[id] || {status: "waiting"};
    const symbol = state.status === "completed" ? "✓" : state.status === "failed" ? "!" : String(index + 1);
    const label = {waiting: "等待", started: "运行中", completed: formatSeconds(state.seconds), failed: "失败"}[state.status] || state.status;
    return `<li class="${state.status}"><span class="dot">${symbol}</span><span class="stage-name">${name}</span><span class="stage-state">${label}</span></li>`;
  }).join("");
}

async function loadDatabases() {
  const response = await fetch("/api/databases");
  const data = await response.json();
  databases = data.databases;
  $("#database").innerHTML = databases.map(db => `<option value="${db.id}">${db.name}</option>`).join("");
  updateExamples();
}

function updateExamples() {
  const db = databases.find(item => item.id === $("#database").value);
  const options = [`<option value="">自行输入问题</option>`];
  (db?.examples || []).forEach((example, index) => options.push(`<option value="${index}">[${example.difficulty || "示例"}] ${example.question}</option>`));
  $("#example").innerHTML = options.join("");
}

function applyExample() {
  const db = databases.find(item => item.id === $("#database").value);
  const index = $("#example").value;
  if (index === "" || !db) return;
  const example = db.examples[Number(index)];
  $("#question").value = example.question;
  $("#evidence").value = example.evidence || "";
}

function startClock() {
  startedAt = Date.now();
  reconnects = 0;
  clearInterval(timer);
  timer = setInterval(() => {
    $("#elapsed-live").textContent = `${Math.floor((Date.now() - startedAt) / 1000)}s`;
  }, 1000);
}

// Give up loudly instead of looping quietly: a page that keeps polling a dead
// backend looks exactly like a page that is still waiting on the model.
const MAX_RECONNECTS = 8;

function abandonRequest(text, badge = "服务不可达") {
  clearInterval(timer);
  setBadge($("#service-state"), badge, "error");
  $("#request-meta").textContent = text;
  $("#run-button").disabled = false;
}

async function submitQuery(event) {
  event.preventDefault();
  $("#run-button").disabled = true;
  $("#result-section").classList.add("hidden");
  initializeTimeline();
  setBadge($("#service-state"), "正在提交", "running");
  const payload = {
    database_id: $("#database").value,
    question: $("#question").value.trim(),
    evidence: $("#evidence").value.trim(),
    max_rows: Number($("#max-rows").value),
    sql_timeout_seconds: Number($("#timeout").value),
  };
  try {
    const response = await fetch("/api/query", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "请求提交失败");
    $("#request-meta").textContent = `请求 ${data.request_id} · 已进入串行队列`;
    setBadge($("#service-state"), "排队中", "running");
    startClock();
    followEvents(data.request_id);
  } catch (error) {
    setBadge($("#service-state"), "提交失败", "error");
    $("#request-meta").textContent = error.message;
    $("#run-button").disabled = false;
  }
}

function followEvents(requestId) {
  const source = new EventSource(`/api/query/${requestId}/events`);
  source.addEventListener("progress", async (message) => {
    reconnects = 0;
    const event = JSON.parse(message.data);
    if (event.stage === "queue" && event.status === "running") {
      setBadge($("#service-state"), "运行中", "running");
      $("#request-meta").textContent = `请求 ${requestId} · 流水线正在运行`;
    }
    if (event.stage === "meter" && event.status === "usage") {
      const tokens = formatNumber(event.total_tokens);
      const budget = event.budget_tokens ? ` / ${formatNumber(event.budget_tokens)}` : "";
      const secs = Math.round(event.elapsed_seconds);
      $("#request-meta").textContent =
        `请求 ${requestId} · 已消耗 ${tokens}${budget} tokens · ${event.calls} 次调用 · ${secs}s`;
    }
    if (stageState[event.stage]) {
      stageState[event.stage] = {status: event.status, seconds: event.seconds ?? null};
      renderTimeline();
    }
    if (event.stage === "request") {
      source.close();
      clearInterval(timer);
      await loadQueryResult(requestId);
    }
  });
  source.onerror = () => {
    source.close();
    if (++reconnects > MAX_RECONNECTS) {
      abandonRequest(`事件流连续 ${MAX_RECONNECTS} 次无法重连，演示服务可能已停止（runner/run_demo.py）`);
      return;
    }
    setBadge($("#service-state"), "连接中断，重试中", "running");
    $("#request-meta").textContent = `事件流中断 · 第 ${reconnects} 次重连`;
    setTimeout(() => loadQueryResult(requestId), 1200);
  };
}

async function loadQueryResult(requestId) {
  let task;
  try {
    const response = await fetch(`/api/query/${requestId}`);
    if (response.status === 404) {
      abandonRequest(`请求 ${requestId} 已不在服务内存中（服务重启会丢弃进行中的任务），请重新提交`, "任务已丢失");
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    task = await response.json();
  } catch (error) {
    // Network failure: the backend is unreachable, not still thinking.
    if (++reconnects > MAX_RECONNECTS) {
      abandonRequest(`无法读取请求结果：${error.message}。演示服务可能已停止`);
      return;
    }
    setBadge($("#service-state"), "连接中断，重试中", "running");
    $("#request-meta").textContent = `结果读取失败（${error.message}）· 第 ${reconnects} 次重试`;
    setTimeout(() => loadQueryResult(requestId), 1500);
    return;
  }
  if (task.status === "queued" || task.status === "running") {
    setTimeout(() => followEvents(requestId), 1200);
    return;
  }
  $("#run-button").disabled = false;
  renderResult(task);
}

function renderResult(task) {
  const result = task.result || {};
  const artifacts = result.artifacts || {};
  const interrupted = task.status === "interrupted" || result.status === "interrupted";
  const success = !interrupted && ["success", "empty_result"].includes(result.status);
  setBadge($("#service-state"), interrupted ? "服务已重启" : success ? "已完成" : "运行失败", success ? "success" : "error");
  setBadge($("#result-badge"), interrupted ? "请求中断" : result.status === "success" ? "执行成功" : result.status === "empty_result" ? "结果为空" : "运行失败", success ? "success" : "error");
  $("#result-section").classList.remove("hidden");
  const chatOnly = result.intent === "chat";
  $("#sql-grid").classList.toggle("hidden", chatOnly);
  $("#details-grid").classList.toggle("hidden", chatOnly);
  $("#result-table-card").classList.toggle("hidden", chatOnly);
  $("#generated-sql").textContent = artifacts.generated_sql || "未生成";
  $("#revised-sql").textContent = artifacts.revised_sql || "未生成";
  $("#final-sql").textContent = artifacts.final_sql || result.sql || "未生成";
  $("#intent-badge").textContent = INTENT_NAMES[result.intent] || result.intent || "通用查询";
  $("#agent-answer").textContent = result.answer || (success ? "查询已完成。" : "未生成分析结论。");
  const context = result.business_context || [];
  $("#business-context").innerHTML = context.length
    ? `<strong>命中的业务口径</strong>${context.map(item => `<span class="chip">${item.name}</span>`).join("")}`
    : `<span class="muted">本次未命中企业指标口径</span>`;
  renderVisualization(result.visualization);

  const usage = result.usage || {};
  const tokens = (usage.chat?.total_tokens || 0) + (usage.embedding?.total_tokens || 0);
  const responses = (usage.chat?.responses || 0) + (usage.embedding?.responses || 0);
  const metrics = [
    ["总耗时", formatSeconds(result.elapsed_seconds)],
    ["总 Token", formatNumber(tokens)],
    ["模型响应", formatNumber(responses)],
    ["生成候选", formatNumber(artifacts.generated_candidate_count)],
    ["结果行数", formatNumber(result.rows?.length || 0)],
  ];
  $("#metrics").innerHTML = metrics.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");

  const schema = artifacts.linked_schema || {};
  $("#schema-chips").innerHTML = Object.keys(schema).length
    ? Object.entries(schema).flatMap(([table, columns]) => (columns || []).map(column => `<span class="chip">${table}.${column}</span>`)).join("")
    : `<span class="muted">暂无可展示的 Schema Linking 产物</span>`;

  const times = result.stage_seconds || {};
  const maxTime = Math.max(...Object.values(times), 1);
  $("#stage-times").innerHTML = STAGES.filter(([id]) => times[id] != null).map(([id, name]) => `<div class="time-row"><span>${name}</span><span class="bar"><i style="width:${Math.max(2, times[id] / maxTime * 100)}%"></i></span><strong>${formatSeconds(times[id])}</strong></div>`).join("");

  const message = $("#result-message");
  message.classList.add("hidden");
  if (interrupted) {
    message.textContent = result.error || "演示服务在该请求运行期间被重启，请求未完成。";
    message.classList.remove("hidden");
  } else if (!success) {
    message.textContent = `${result.failed_stage || "unknown"}：${result.error || "运行失败，请检查本地产物。"}`;
    message.classList.remove("hidden");
  } else if (result.status === "empty_result") {
    message.textContent = "SQL 执行成功，但没有返回数据。";
    message.classList.remove("hidden");
  }
  renderTable(result.columns || [], result.rows || []);
  $("#row-note").textContent = result.truncated ? `显示前 ${result.rows.length} 行，结果已截断` : `${result.rows?.length || 0} 行`;
  $("#result-section").scrollIntoView({behavior: "smooth", block: "start"});
}

function renderVisualization(chart) {
  const card = $("#chart-card");
  const container = $("#chart-container");
  container.replaceChildren();
  if (!chart || chart.type === "table") { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  $("#chart-title").textContent = chart.title || "数据可视化";
  $("#chart-type").textContent = {bar: "柱状图", line: "折线图", pie: "饼图", metric: "指标卡"}[chart.type] || chart.type;
  if (chart.type === "metric") {
    const grid = document.createElement("div"); grid.className = "chart-metrics";
    (chart.items || []).forEach(item => { const node = document.createElement("div"); node.className = "chart-metric"; node.innerHTML = `<span>${item.name}</span><strong>${formatNumber(item.value)}</strong>`; grid.appendChild(node); });
    container.appendChild(grid); return;
  }
  if (chart.type === "pie") { renderPie(container, chart.items || []); return; }
  renderCartesian(container, chart);
}

function svgNode(name, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function renderCartesian(container, chart) {
  const labels = chart.labels || [], series = chart.series || [];
  const width = 900, height = 340, left = 70, right = 24, top = 30, bottom = 72;
  const svg = svgNode("svg", {viewBox: `0 0 ${width} ${height}`, role: "img"});
  const values = series.flatMap(item => item.values || []).map(Number).filter(Number.isFinite);
  const max = Math.max(...values, 1), plotW = width - left - right, plotH = height - top - bottom;
  svg.appendChild(svgNode("line", {x1:left,y1:top,x2:left,y2:top+plotH,class:"chart-axis"}));
  svg.appendChild(svgNode("line", {x1:left,y1:top+plotH,x2:left+plotW,y2:top+plotH,class:"chart-axis"}));
  const count = Math.max(labels.length, 1), step = plotW / count;
  if (chart.type === "bar") {
    const vals = series[0]?.values || [];
    vals.forEach((value, index) => {
      const barH = Number(value) / max * plotH;
      svg.appendChild(svgNode("rect", {x:left+index*step+step*.18,y:top+plotH-barH,width:step*.64,height:barH,rx:5,class:"chart-bar"}));
    });
  } else {
    series.forEach((item, seriesIndex) => {
      const points = (item.values || []).map((value,index) => `${left+index*step+step/2},${top+plotH-Number(value)/max*plotH}`).join(" ");
      svg.appendChild(svgNode("polyline", {points,fill:"none",class:`chart-line series-${seriesIndex}`}));
    });
  }
  labels.forEach((label,index) => { const text=svgNode("text",{x:left+index*step+step/2,y:top+plotH+24,class:"chart-label","text-anchor":"middle"}); text.textContent=String(label).slice(0,10); svg.appendChild(text); });
  container.appendChild(svg);
}

function renderPie(container, items) {
  const total = items.reduce((sum,item)=>sum+Number(item.value||0),0) || 1;
  const colors = ["#14b8a6","#3b82f6","#8b5cf6","#f59e0b","#ef4444","#06b6d4"];
  let angle = 0; const stops=[];
  items.forEach((item,index)=>{ const next=angle+Number(item.value||0)/total*360; stops.push(`${colors[index%colors.length]} ${angle}deg ${next}deg`); angle=next; });
  const wrap=document.createElement("div"); wrap.className="pie-layout";
  const pie=document.createElement("div"); pie.className="pie-chart"; pie.style.background=`conic-gradient(${stops.join(",")})`; wrap.appendChild(pie);
  const legend=document.createElement("div"); legend.className="pie-legend";
  items.forEach((item,index)=>{ const row=document.createElement("div"); const pct=Number(item.value||0)/total*100; row.innerHTML=`<i style="background:${colors[index%colors.length]}"></i><span>${item.name}</span><strong>${pct.toFixed(1)}%</strong>`; legend.appendChild(row); });
  wrap.appendChild(legend); container.appendChild(wrap);
}

function renderTable(columns, rows) {
  const table = $("#result-table");
  table.replaceChildren();
  if (!columns.length) return;
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach(column => { const th = document.createElement("th"); th.textContent = column; headRow.appendChild(th); });
  head.appendChild(headRow); table.appendChild(head);
  const body = document.createElement("tbody");
  rows.forEach(row => { const tr = document.createElement("tr"); row.forEach(value => { const td = document.createElement("td"); td.textContent = valueText(value); tr.appendChild(td); }); body.appendChild(tr); });
  table.appendChild(body);
}

async function loadEvaluations() {
  const target = $("#evaluation-list");
  target.innerHTML = `<div class="muted">正在读取报告…</div>`;
  try {
    const response = await fetch("/api/evaluations");
    const data = await response.json();
    if (!data.evaluations.length) {
      target.innerHTML = `<div class="card eval-card"><h3>暂无评测报告</h3><p class="muted">主评测完成后点击“刷新结果”。</p></div>`;
      return;
    }
    target.innerHTML = data.evaluations.map(item => {
      const accuracy = item.kind === "ablation" ? item.variants?.full_pipeline?.execution_accuracy : item.execution_accuracy;
      return `<article class="card eval-card" data-eval="${item.id}"><h3>${item.title}</h3><div class="muted">${item.id} · ${item.sample_count || 0} 题</div><div class="eval-kpi">${formatPercent(accuracy)}</div><div class="muted">完整流程集合式 EX</div></article>`;
    }).join("");
    target.querySelectorAll("[data-eval]").forEach(card => card.addEventListener("click", () => loadEvaluationDetail(card.dataset.eval)));
  } catch (error) {
    target.innerHTML = `<div class="message">${error.message}</div>`;
  }
}

async function loadEvaluationDetail(id) {
  const response = await fetch(`/api/evaluations/${encodeURIComponent(id)}`);
  const data = await response.json();
  const target = $("#evaluation-detail");
  target.classList.remove("hidden");
  if (data.kind === "ablation") {
    const rows = Object.entries(data.variants).map(([name, metrics]) => `<tr><td>${VARIANT_NAMES[name] || name}</td><td>${metrics.correct_count}/${metrics.sample_count}</td><td>${formatPercent(metrics.execution_accuracy)}</td><td>${formatPercent(metrics.strict_multiset_accuracy)}</td><td>${metrics.executable_count}/${metrics.sample_count}</td><td>${formatSeconds(metrics.batch_wall_seconds)}</td><td>${formatNumber(metrics.total_tokens)}</td></tr>`).join("");
    target.innerHTML = `<div class="section-heading"><h2>${data.title}</h2><span class="badge success">三组对照</span></div><div class="eval-table"><table><thead><tr><th>方案</th><th>正确</th><th>集合式 EX</th><th>严格 EX</th><th>可执行</th><th>批次耗时</th><th>Token</th></tr></thead><tbody>${rows}</tbody></table></div>${renderTransitions(data.transitions)}${renderQuestionRows(data.questions, true)}`;
  } else {
    target.innerHTML = `<div class="section-heading"><h2>${data.title}</h2><span class="badge neutral">基线</span></div><div class="metric-grid"><div class="metric"><span>集合式 EX</span><strong>${formatPercent(data.execution_accuracy)}</strong></div><div class="metric"><span>严格 EX</span><strong>${formatPercent(data.strict_multiset_accuracy)}</strong></div><div class="metric"><span>正确题数</span><strong>${data.correct_count}/${data.sample_count}</strong></div><div class="metric"><span>平均耗时</span><strong>${formatSeconds(data.mean_item_seconds)}</strong></div><div class="metric"><span>Token</span><strong>${formatNumber(data.total_tokens)}</strong></div></div>${renderQuestionRows(data.questions, false)}`;
  }
  target.scrollIntoView({behavior: "smooth", block: "start"});
}

function renderTransitions(transitions = {}) {
  const items = [
    ["修订修复", transitions.revision_fixes], ["修订退化", transitions.revision_regressions],
    ["选择修复", transitions.selection_fixes], ["选择退化", transitions.selection_regressions],
  ];
  return `<div class="metric-grid">${items.map(([label, values]) => `<div class="metric"><span>${label}</span><strong>${values?.length || 0}</strong><span>${values?.length ? `题号：${values.join(", ")}` : "无"}</span></div>`).join("")}</div>`;
}

function renderQuestionRows(questions = [], ablation) {
  if (!questions.length) return "";
  const rows = questions.map(item => {
    const status = ablation ? Object.values(item.variants || {}).map(v => v.execution_correct ? "✓" : "×").join(" / ") : (item.execution_correct ? "✓" : "×");
    return `<tr><td>${item.question_id}</td><td>${item.database_id || ""}</td><td>${item.difficulty || ""}</td><td>${item.question || ""}</td><td>${status}</td></tr>`;
  }).join("");
  return `<div class="eval-table"><table><thead><tr><th>ID</th><th>数据库</th><th>难度</th><th>问题</th><th>${ablation ? "生成 / 修订 / 完整" : "正确"}</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

document.addEventListener("DOMContentLoaded", async () => {
  initializeTimeline();
  try { await loadDatabases(); } catch (error) { setBadge($("#service-state"), "配置错误", "error"); $("#request-meta").textContent = error.message; }
  $("#database").addEventListener("change", updateExamples);
  $("#example").addEventListener("change", applyExample);
  $("#query-form").addEventListener("submit", submitQuery);
  $("#refresh-evals").addEventListener("click", loadEvaluations);
  document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === tab));
    document.querySelectorAll(".panel").forEach(panel => panel.classList.remove("active"));
    $(`#${tab.dataset.tab}-panel`).classList.add("active");
    if (tab.dataset.tab === "evaluation") loadEvaluations();
  }));
  document.querySelectorAll(".copy").forEach(button => button.addEventListener("click", async () => {
    await navigator.clipboard.writeText($(`#${button.dataset.copy}`).textContent);
    const original = button.textContent; button.textContent = "已复制"; setTimeout(() => button.textContent = original, 1200);
  }));
});
