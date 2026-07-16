const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  runId: null,
  timer: null,
  selectedTaskId: null,
  currentRun: null,
  showAllEvents: false,
  phase: null,
  resultMarkdown: "",
};

const statusLabels = {
  created: "已创建",
  planning: "正在规划",
  executing: "并行执行中",
  replanning: "重新规划中",
  completed: "已完成",
  failed: "失败",
};

const taskLabels = {
  pending: "等待依赖",
  running: "执行中",
  validated: "已验收",
  suspect: "待复核",
  rejected: "未通过",
  invalidated: "已替换",
};

const workerLabels = {
  "rule-core": "规则检查器",
  "research-scout": "研究侦察员",
  "solution-architect": "方案架构师",
  "code-specialist": "实现工程师",
  "data-analyst": "数据分析师",
  "risk-reviewer": "风险审查员",
  "synthesis-editor": "综合编辑",
  "senior-planner": "高级规划顾问",
};

const resultLabels = {
  objective: "交付目标",
  covered_scope: "覆盖范围",
  acceptance_criteria: "验收条件",
  note: "说明",
  requirements: "需求",
  constraints: "约束",
};

const goalInput = $("#goal");
const form = $("#run-form");
const submitButton = $("#submit-button");

goalInput.addEventListener("input", () => {
  updateGoalCount();
  clearFieldError();
});

$("#quality").addEventListener("input", (event) => {
  $("#quality-value").textContent = `${event.target.value}%`;
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearFieldError();
  $("#form-error").textContent = "";

  if (!goalInput.value.trim()) {
    showFieldError("请先描述希望完成的任务。");
    return;
  }
  if (goalInput.value.trim().length < 6) {
    showFieldError("请再具体一些，任务目标至少需要 6 个字符。");
    return;
  }
  const invalidNumber = [$("#budget"), $("#concurrency")].find((input) => !input.checkValidity());
  if (invalidNumber) {
    $("#form-error").textContent = invalidNumber.id === "budget"
      ? "预算需要在 0.05 到 100 模拟点数之间。"
      : "最大并发需要在 1 到 12 之间。";
    invalidNumber.focus();
    return;
  }

  await createRun();
});

$("#refresh-history").addEventListener("click", () => refreshHistory());
$("#events-toggle").addEventListener("click", () => {
  state.showAllEvents = !state.showAllEvents;
  if (state.currentRun) renderTimeline(state.currentRun.events || []);
});

$("#edit-and-retry").addEventListener("click", () => {
  scrollToControls();
  if ((state.currentRun?.error || "").includes("预算")) $("#budget").focus();
  else goalInput.focus();
});

$("#retry-run").addEventListener("click", () => {
  if (!submitButton.disabled) form.requestSubmit();
});

$("#copy-result").addEventListener("click", copyResult);
$("#download-result").addEventListener("click", downloadResult);

async function createRun() {
  setSubmitState(true, "正在创建计划…");
  const payload = {
    goal: goalInput.value.trim(),
    mode: document.querySelector('input[name="mode"]:checked').value,
    budget: Number($("#budget").value),
    quality_floor: Number($("#quality").value) / 100,
    max_concurrency: Number($("#concurrency").value),
    simulate_replan: $("#simulate-replan").checked,
  };

  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法启动任务");

    state.runId = data.id;
    state.currentRun = data;
    state.selectedTaskId = null;
    state.showAllEvents = false;
    state.phase = null;
    updateRunHash(data.id);
    updateHistorySelection(data.id);
    revealRunContent();
    render(data);
    setSubmitState(true, "任务运行中…");
    startPolling();
    refreshHistory();
    bringResultsIntoView();
  } catch (error) {
    const message = error instanceof Error ? error.message : "无法启动任务";
    if (message.includes("目标") || message.includes("字符")) showFieldError(message);
    else $("#form-error").textContent = message;
    setSubmitState(false);
  }
}

function startPolling() {
  clearInterval(state.timer);
  state.timer = setInterval(poll, 420);
}

async function poll() {
  if (!state.runId) return;
  try {
    const previousStatus = state.currentRun?.status;
    const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}`);
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法获取任务状态");
    state.currentRun = data;
    render(data);
    if (previousStatus !== data.status && ["completed", "failed"].includes(data.status)) {
      revealTerminalState(data.status);
    }
    if (["completed", "failed"].includes(data.status)) {
      clearInterval(state.timer);
      state.timer = null;
      setSubmitState(false);
      refreshHistory();
    }
  } catch (error) {
    clearInterval(state.timer);
    state.timer = null;
    $("#form-error").textContent = `状态更新失败：${error instanceof Error ? error.message : "网络不可用"}`;
    $("#run-live").textContent = "任务状态更新失败，请检查服务是否仍在运行。";
    setSubmitState(false);
  }
}

async function loadRun(runId, shouldScroll = true) {
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法读取任务");
    clearInterval(state.timer);
    state.timer = null;
    state.runId = data.id;
    state.currentRun = data;
    state.selectedTaskId = null;
    state.showAllEvents = false;
    state.phase = null;
    updateRunHash(data.id);
    updateHistorySelection(data.id);
    revealRunContent();
    render(data);
    if (["completed", "failed"].includes(data.status)) setSubmitState(false);
    else {
      setSubmitState(true, "任务运行中…");
      startPolling();
    }
    if (shouldScroll) bringResultsIntoView(true);
  } catch (error) {
    $("#form-error").textContent = error instanceof Error ? error.message : "无法读取任务";
  }
}

async function refreshHistory(options = {}) {
  try {
    const response = await fetch("/api/runs");
    const data = await readJson(response);
    if (!response.ok) throw new Error("无法读取历史任务");
    const runs = data.runs || [];
    renderHistory(runs);
    if (options.restore) {
      const params = new URLSearchParams(location.hash.replace(/^#/, ""));
      const requestedId = params.get("run");
      const active = runs.find((run) => !["completed", "failed"].includes(run.status));
      const target = requestedId || active?.id || runs[0]?.id;
      if (target) await loadRun(target, false);
    }
    return runs;
  } catch (_error) {
    $("#history-list").innerHTML = '<p class="history-empty">暂时无法读取最近任务。</p>';
    return [];
  }
}

function render(run) {
  state.currentRun = run;
  const status = $("#run-status");
  status.textContent = statusLabels[run.status] || run.status;
  status.dataset.status = run.status;
  status.classList.toggle("muted", !["executing", "completed"].includes(run.status));

  if (state.phase !== run.status) {
    state.phase = run.status;
    $("#run-live").textContent = `任务状态更新为：${statusLabels[run.status] || run.status}`;
  }

  const metrics = run.metrics || {};
  const probability = Number(metrics.success_probability || 0);
  $("#metric-success").textContent = `${Math.round(probability * 100)}%`;
  $("#success-meter").style.width = `${Math.max(0, Math.min(100, probability * 100))}%`;
  $("#metric-cost").textContent = Number(metrics.spent || 0).toFixed(3);
  $("#metric-budget").textContent = `/ ${Number(metrics.budget || 0).toFixed(2)} 点预算`;
  $("#metric-parallel").textContent = `${metrics.peak_parallelism || 0}×`;
  $("#metric-time").textContent = metrics.duration_ms == null ? "进行中" : formatTime(metrics.duration_ms);
  $("#metric-replan").textContent = `${metrics.replans || 0} 次重规划`;

  renderRunError(run);
  if (!run.plan) return;

  $("#difficulty").textContent = `L${run.plan.difficulty} / 5`;
  $("#rationale").textContent = run.plan.rationale;
  $("#type-tags").innerHTML = run.plan.task_types.map((type) => `<i>${escapeHtml(type)}</i>`).join("");

  if (!state.selectedTaskId || !run.plan.tasks.some((task) => task.id === state.selectedTaskId)) {
    const finalTask = findFinalTask(run.plan.tasks);
    state.selectedTaskId = run.status === "completed" && finalTask ? finalTask.id : run.plan.tasks[0]?.id;
  }

  renderOutcome(run);
  renderDag(run.plan.tasks);
  renderTimeline(run.events || []);
  const selected = run.plan.tasks.find((task) => task.id === state.selectedTaskId) || run.plan.tasks[0];
  renderContract(selected);
}

function renderRunError(run) {
  const panel = $("#run-error-panel");
  if (run.status !== "failed") {
    panel.classList.add("hidden");
    return;
  }
  const message = run.error || "任务未能完成，请查看运行事件了解详细原因。";
  $("#run-error-message").textContent = message;
  $("#run-error-hint").textContent = message.includes("预算")
    ? "建议提高预算、降低并发，或缩小任务范围后重新运行。"
    : "可以调整目标或参数后重新运行；已经验收的节点仍可在下方查看。";
  panel.classList.remove("hidden");
}

function renderOutcome(run) {
  const card = $("#outcome-card");
  const finalTask = findFinalTask(run.plan.tasks);
  if (!finalTask?.result || finalTask.status !== "validated") {
    card.classList.add("hidden");
    state.resultMarkdown = "";
    return;
  }

  const result = finalTask.result;
  $("#outcome-summary").textContent = result.summary || "最终成果已经完成。";
  const deliverable = result.deliverable && typeof result.deliverable === "object" ? result.deliverable : {};
  $("#outcome-body").innerHTML = Object.entries(deliverable).map(([key, value]) => `
    <section>
      <span>${escapeHtml(resultLabels[key] || humanize(key))}</span>
      ${renderValue(value)}
    </section>`).join("");

  const evidence = Array.isArray(result.evidence) ? result.evidence : [];
  const assumptions = Array.isArray(result.assumptions) ? result.assumptions : [];
  $("#outcome-evidence-body").innerHTML = `
    <div><h4>证据</h4>${renderValue(evidence)}</div>
    <div><h4>当前假设</h4>${renderValue(assumptions)}</div>`;
  $("#outcome-confidence").textContent = `成果置信度 ${Math.round(Number(result.confidence || 0) * 100)}%`;
  $("#copy-feedback").textContent = "";
  state.resultMarkdown = buildResultMarkdown(run, finalTask);
  card.classList.remove("hidden");
}

function renderDag(tasks) {
  const focusedTaskId = document.activeElement?.dataset?.taskId || null;
  const levels = calculateLevels(tasks);
  const grouped = new Map();
  tasks.forEach((task) => {
    const level = levels.get(task.id) || 0;
    if (!grouped.has(level)) grouped.set(level, []);
    grouped.get(level).push(task);
  });

  $("#dag").innerHTML = [...grouped.entries()].sort(([a], [b]) => a - b).map(([level, items]) => `
    <div class="dag-column">
      <p class="dag-level">阶段 ${String(level + 1).padStart(2, "0")}</p>
      ${items.map((task) => `
        <button class="task-card ${task.id === state.selectedTaskId ? "selected" : ""}"
          type="button" data-task-id="${escapeHtml(task.id)}" data-status="${escapeHtml(task.status)}"
          aria-pressed="${task.id === state.selectedTaskId}">
          <strong>${escapeHtml(task.title)}</strong>
          <small>${escapeHtml(workerLabels[task.selected_worker] || task.selected_worker || "等待路由")}</small>
          <span class="task-state">${escapeHtml(taskLabels[task.status] || task.status)}</span>
        </button>`).join("")}
    </div>`).join("");

  $$(".task-card").forEach((button) => button.addEventListener("click", () => {
    state.selectedTaskId = button.dataset.taskId;
    $$(".task-card").forEach((card) => {
      const selected = card.dataset.taskId === state.selectedTaskId;
      card.classList.toggle("selected", selected);
      card.setAttribute("aria-pressed", String(selected));
    });
    const selectedTask = state.currentRun?.plan?.tasks.find((task) => task.id === state.selectedTaskId);
    renderContract(selectedTask);
  }));

  if (focusedTaskId) {
    const target = $$(".task-card").find((button) => button.dataset.taskId === focusedTaskId);
    target?.focus({ preventScroll: true });
  }
}

function renderTimeline(events) {
  $("#event-count").textContent = `${events.length} 条`;
  const reversed = [...events].reverse();
  const visible = state.showAllEvents ? reversed.slice(0, 60) : reversed.slice(0, 12);
  $("#timeline").innerHTML = visible.map((event) => {
    const tone = event.kind.includes("validated") || event.kind === "completed"
      ? "good"
      : event.kind.includes("reject") || event.kind === "failed"
        ? "bad"
        : event.kind.includes("replan") ? "warn" : "";
    const at = new Date(event.at).toLocaleTimeString("zh-CN", { hour12: false });
    return `<li class="${tone}"><span>${escapeHtml(event.message)}</span><time>${at}${event.task_id ? ` · ${escapeHtml(event.task_id)}` : ""}</time></li>`;
  }).join("");

  const toggle = $("#events-toggle");
  toggle.classList.toggle("hidden", events.length <= 12);
  toggle.textContent = state.showAllEvents ? "收起" : `展开全部 ${events.length} 条`;
  toggle.setAttribute("aria-expanded", String(state.showAllEvents));
}

function renderContract(task) {
  if (!task) return;
  const list = (items) => items?.length ? items.map(escapeHtml).join(" · ") : "—";
  const resultSection = task.result ? `
    <div class="node-result">
      <span>节点成果</span>
      <p>${escapeHtml(task.result.summary || "已返回结构化成果")}</p>
      <small>置信度 ${Math.round(Number(task.result.confidence || 0) * 100)}%</small>
    </div>` : "";

  $("#contract").innerHTML = `
    <div class="contract-heading">
      <div><h4>${escapeHtml(task.title)}</h4><code>${escapeHtml(task.id)} · 计划 v${task.plan_version}</code></div>
      <span class="contract-status" data-status="${escapeHtml(task.status)}">${escapeHtml(taskLabels[task.status] || task.status)}</span>
    </div>
    <dl>
      <dt>目标</dt><dd>${escapeHtml(task.objective)}</dd>
      <dt>依赖</dt><dd>${list(task.dependencies)}</dd>
      <dt>所需能力</dt><dd>${list(task.required_capabilities)}</dd>
      <dt>验收标准</dt><dd>${list(task.acceptance_criteria)}</dd>
      <dt>输出格式</dt><dd>${list(task.output_schema)}</dd>
      <dt>执行预测</dt><dd>成功率 ${Math.round(task.predicted_success * 100)}% · ${task.estimated_cost.toFixed(3)} 点 · ${task.estimated_latency_ms}ms</dd>
      ${task.validation ? `<dt>最近验收</dt><dd>${escapeHtml(task.validation.findings.join("；"))}</dd>` : ""}
    </dl>
    ${resultSection}`;
}

function renderHistory(runs) {
  const container = $("#history-list");
  if (!runs.length) {
    container.innerHTML = '<p class="history-empty">运行过的任务会出现在这里。</p>';
    return;
  }

  container.innerHTML = runs.slice(0, 5).map((run) => `
    <button class="history-item ${run.id === state.runId ? "active" : ""}" type="button" data-run-id="${escapeHtml(run.id)}"${run.id === state.runId ? ' aria-current="true"' : ""}>
      <span class="history-state" data-status="${escapeHtml(run.status)}">${escapeHtml(statusLabels[run.status] || run.status)}</span>
      <strong>${escapeHtml(run.request?.goal || "未命名任务")}</strong>
      <small>${formatDate(run.created_at)} · ${run.metrics?.spent?.toFixed?.(3) || "0.000"} 点</small>
    </button>`).join("");
  $$(".history-item").forEach((button) => button.addEventListener("click", () => loadRun(button.dataset.runId)));
}

function updateHistorySelection(runId) {
  $$(".history-item").forEach((button) => {
    const active = button.dataset.runId === runId;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  });
}

function calculateLevels(tasks) {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const memo = new Map();
  function level(id, visiting = new Set()) {
    if (memo.has(id)) return memo.get(id);
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const task = byId.get(id);
    const value = !task || task.dependencies.length === 0
      ? 0
      : 1 + Math.max(...task.dependencies.map((dep) => level(dep, visiting)));
    memo.set(id, value);
    visiting.delete(id);
    return value;
  }
  tasks.forEach((task) => level(task.id));
  return memo;
}

function renderValue(value) {
  if (Array.isArray(value)) {
    if (!value.length) return "<p>—</p>";
    return `<ul>${value.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
  }
  if (value && typeof value === "object") {
    return `<p>${escapeHtml(JSON.stringify(value, null, 2))}</p>`;
  }
  return `<p>${escapeHtml(value ?? "—")}</p>`;
}

function findFinalTask(tasks) {
  return tasks.find((task) => task.id === "final-synthesis")
    || [...tasks].reverse().find((task) => task.task_type === "synthesis");
}

function buildResultMarkdown(run, task) {
  const result = task.result || {};
  const deliverable = result.deliverable || {};
  const lines = [
    `# ${run.request?.goal || "CostWeave 最终成果"}`,
    "",
    result.summary || "最终成果已经完成。",
    "",
  ];
  Object.entries(deliverable).forEach(([key, value]) => {
    lines.push(`## ${resultLabels[key] || humanize(key)}`, "");
    if (Array.isArray(value)) value.forEach((item) => lines.push(`- ${item}`));
    else lines.push(String(value ?? "—"));
    lines.push("");
  });
  if (result.evidence?.length) {
    lines.push("## 证据", "", ...result.evidence.map((item) => `- ${item}`), "");
  }
  if (result.assumptions?.length) {
    lines.push("## 当前假设", "", ...result.assumptions.map((item) => `- ${item}`), "");
  }
  lines.push(`> 成果置信度：${Math.round(Number(result.confidence || 0) * 100)}%`, "> 当前为离线模拟成果，未调用真实模型。", "");
  return lines.join("\n");
}

async function copyResult() {
  if (!state.resultMarkdown) return;
  try {
    await navigator.clipboard.writeText(state.resultMarkdown);
  } catch (_error) {
    const temporary = document.createElement("textarea");
    temporary.value = state.resultMarkdown;
    temporary.setAttribute("readonly", "");
    temporary.className = "clipboard-fallback";
    document.body.appendChild(temporary);
    temporary.select();
    document.execCommand("copy");
    temporary.remove();
  }
  $("#copy-feedback").textContent = "已复制";
  setTimeout(() => { $("#copy-feedback").textContent = ""; }, 1800);
}

function downloadResult() {
  if (!state.resultMarkdown) return;
  const blob = new Blob([state.resultMarkdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `costweave-${state.runId || "result"}.md`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function revealRunContent() {
  $("#empty-state").classList.add("hidden");
  $("#run-content").classList.remove("hidden");
}

function setSubmitState(busy, label = "生成计划并开始调度") {
  submitButton.disabled = busy;
  submitButton.classList.toggle("is-loading", busy);
  $("#submit-label").textContent = label;
}

function updateGoalCount() {
  $("#goal-count").textContent = `${goalInput.value.length} / 2000`;
}

function showFieldError(message) {
  $("#goal-error").textContent = message;
  goalInput.setAttribute("aria-invalid", "true");
  goalInput.focus();
}

function clearFieldError() {
  $("#goal-error").textContent = "";
  goalInput.removeAttribute("aria-invalid");
}

function scrollToControls() {
  $("#workspace").scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
}

function bringResultsIntoView(force = false) {
  if (force || window.matchMedia("(max-width: 980px)").matches) {
    $("#run-panel").scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
  }
}

function revealTerminalState(status) {
  const target = status === "completed" ? $("#outcome-card") : $("#run-error-panel");
  if (!target || target.classList.contains("hidden")) return;
  setTimeout(() => target.scrollIntoView({
    behavior: prefersReducedMotion() ? "auto" : "smooth",
    block: "start",
  }), 80);
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function updateRunHash(runId) {
  history.replaceState(null, "", `#run=${encodeURIComponent(runId)}`);
}

async function readJson(response) {
  try {
    return await response.json();
  } catch (_error) {
    throw new Error("服务返回了无法识别的响应");
  }
}

function formatTime(ms) {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function formatDate(value) {
  if (!value) return "时间未知";
  return new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function humanize(value) {
  return String(value).replaceAll("_", " ");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  }[char]));
}

updateGoalCount();
refreshHistory({ restore: true });
