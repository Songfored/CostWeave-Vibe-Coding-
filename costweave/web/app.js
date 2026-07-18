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
  modelLabels: {},
  catalogModels: [],
  catalogMetadata: {},
  editingModelId: null,
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

function modelLabel(modelId) {
  return state.modelLabels[modelId] || modelId || "等待路由";
}

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
  $("#metric-cost").textContent = `$${Number(metrics.spent || 0).toFixed(4)}`;
  $("#metric-budget").textContent = `/ $${Number(metrics.budget || 0).toFixed(2)} 预算`;
  $("#metric-parallel").textContent = `${metrics.peak_parallelism || 0}×`;
  $("#metric-time").textContent = metrics.duration_ms == null ? "进行中" : formatTime(metrics.duration_ms);
  $("#metric-replan").textContent = `${metrics.replans || 0} 次重规划`;

  renderRunError(run);
  if (!run.plan) return;

  $("#difficulty").textContent = `L${run.plan.difficulty} / 10`;
  $("#rationale").textContent = run.plan.rationale;
  const analysis = run.plan.analysis || {};
  const classifications = analysis.classifications || [];
  $("#type-tags").innerHTML = classifications.length
    ? classifications.slice(0, 4).map((item) => {
      const evidence = (item.evidence || []).join("、");
      const title = evidence ? `触发依据：${evidence}` : "本地分类器判断";
      return `<i title="${escapeHtml(title)}">${escapeHtml(item.label)} ${Math.round(Number(item.score || 0) * 100)}%</i>`;
    }).join("")
    : run.plan.task_types.map((type) => `<i>${escapeHtml(type)}</i>`).join("");
  $("#analysis-signals").innerHTML = [
    `主意图 ${analysis.primary_intent || "analyze"}`,
    `分类置信 ${Math.round(Number(analysis.classification_confidence || 0) * 100)}%`,
    `歧义 ${Math.round(Number(analysis.ambiguity || 0) * 100)}%`,
    `不确定性 ${Math.round(Number(analysis.uncertainty || 0) * 100)}%`,
    `风险 ${analysis.stakes || "normal"}`,
    analysis.needs_fresh_data ? "需要时效数据" : "静态知识可用",
  ].map((item) => `<span>${escapeHtml(item)}</span>`).join("");
  const routing = run.plan.routing_summary || {};
  $("#routing-summary").textContent = routing.strategy
    ? `路由：${routing.strategy} · 目录 ${routing.catalog_revision || run.plan.model_snapshot || "未知"} · 最低可行 $${Number(routing.minimum_feasible_cost_usd || 0).toFixed(4)} · 当前组合 $${Number(routing.estimated_total_cost_usd || 0).toFixed(4)} · ${routing.independent_validators || 0} 个独立验收`
    : "等待模型组合决策";

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
          <small>${escapeHtml(modelLabel(task.selected_worker))}</small>
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

  const candidates = (task.routing_candidates || []).map((candidate) => `
    <li><b>${escapeHtml(candidate.name)}</b><span>均值 ${Math.round(Number(candidate.predicted_success || 0) * 100)}% · 下界 ${Math.round(Number(candidate.success_lower_bound || 0) * 100)}% · $${Number(candidate.estimated_cost || 0).toFixed(4)}</span></li>`).join("");
  const rejections = (task.routing_rejections || []).slice(0, 5).map((item) => `
    <li><b>${escapeHtml(item.name)}</b><span>${escapeHtml((item.reasons || []).join(" · "))}</span></li>`).join("");
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
      <dt>选定模型</dt><dd>${escapeHtml(modelLabel(task.selected_worker))}</dd>
      <dt>路由理由</dt><dd>${escapeHtml(task.routing_rationale || "等待路由")}</dd>
      <dt>执行预测</dt><dd>均值 ${Math.round(task.predicted_success * 100)}% · 保守下界 ${Math.round(Number(task.predicted_success_lower_bound || 0) * 100)}% · 路由置信 ${Math.round(Number(task.route_confidence || 0) * 100)}% · $${Number(task.estimated_cost || 0).toFixed(4)} · ${task.estimated_latency_ms}ms</dd>
      <dt>任务画像</dt><dd>难度 L${task.difficulty}/10 · 风险 ${escapeHtml(task.risk_level)} · 不确定性 ${Math.round(Number(task.uncertainty || 0) * 100)}% · 关键度 ${Math.round(Number(task.criticality || 0) * 100)}% · 输入约 ${task.estimated_input_tokens} tokens · 输出约 ${task.estimated_output_tokens} tokens</dd>
      ${task.validation ? `<dt>最近验收</dt><dd>${escapeHtml(task.validation.findings.join("；"))}</dd>` : ""}
    </dl>
    <div class="candidate-list"><span>候选排序</span><ol>${candidates || "<li>暂无候选</li>"}</ol></div>
    <div class="candidate-list rejected-list"><span>硬约束淘汰</span><ol>${rejections || "<li>无</li>"}</ol></div>
    <details class="handoff-prompt"><summary>查看发送给执行模型的任务合同</summary><pre>${escapeHtml(task.handoff_prompt || "暂无")}</pre></details>
    ${resultSection}`;
}

async function loadCatalog(announcement = "") {
  try {
    const response = await fetch("/api/catalog");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "目录不可用");
    state.catalogModels = data.models || data.workers || [];
    state.catalogMetadata = data.metadata || {};
    state.modelLabels = Object.fromEntries(state.catalogModels.map((model) => [model.id, model.name]));
    const metadata = state.catalogMetadata;
    $("#catalog-meta").textContent = `修订 r${metadata.catalog_revision || "?"} · ${metadata.updated_at ? formatDate(metadata.updated_at) : metadata.snapshot_date || "时间未知"}`;
    renderCatalogStats();
    renderProviderFilter();
    renderCatalog();
    if (announcement) $("#catalog-live").textContent = announcement;
    else if (metadata.load_warning) $("#catalog-live").textContent = metadata.load_warning;
  } catch (error) {
    $("#catalog-meta").textContent = "模型目录暂时不可用";
    $("#catalog-live").textContent = error instanceof Error ? error.message : "目录读取失败";
    $("#model-catalog").innerHTML = '<p class="history-empty">请确认本地服务仍在运行。</p>';
  }
}

function renderCatalogStats() {
  const models = state.catalogModels;
  const stale = models.filter((model) => isStale(model.verified_at)).length;
  $("#catalog-stats").innerHTML = [
    ["总模型", models.length],
    ["可路由", models.filter((model) => model.routable).length],
    ["用户修改", models.filter((model) => model.custom).length],
    ["资料过期", stale],
  ].map(([label, value]) => `<article><span>${label}</span><strong>${value}</strong></article>`).join("");
}

function renderProviderFilter() {
  const select = $("#catalog-provider-filter");
  const current = select.value;
  const providers = [...new Set(state.catalogModels.map((model) => model.provider))].sort();
  select.innerHTML = '<option value="">全部</option>' + providers.map((provider) => `<option value="${escapeHtml(provider)}">${escapeHtml(provider)}</option>`).join("");
  if (providers.includes(current)) select.value = current;
}

function renderCatalog() {
  const query = $("#catalog-search").value.trim().toLowerCase();
  const provider = $("#catalog-provider-filter").value;
  const status = $("#catalog-status-filter").value;
  const models = state.catalogModels.filter((model) => {
    const haystack = `${model.name} ${model.id} ${model.model_id || ""} ${model.provider} ${model.specialty}`.toLowerCase();
    if (query && !haystack.includes(query)) return false;
    if (provider && model.provider !== provider) return false;
    if (status === "routable" && !model.routable) return false;
    if (status === "disabled" && model.routable) return false;
    if (status === "custom" && !model.custom) return false;
    return true;
  });
  $("#catalog-live").textContent = `显示 ${models.length} / ${state.catalogModels.length} 个模型`;
  if (!models.length) {
    $("#model-catalog").innerHTML = '<p class="history-empty">没有符合筛选条件的模型。</p>';
    return;
  }
  $("#model-catalog").innerHTML = models.map((model) => {
    const currency = model.pricing_currency || "USD";
    const price = model.local
      ? "API $0 · 本地算力另计"
      : `${currency} ${Number(model.input_price_per_mtok).toFixed(3)} / ${Number(model.output_price_per_mtok).toFixed(3)} 每百万输入/输出`;
    const source = safeSourceUrl(model.source_url);
    const badges = [
      model.routable ? "可路由" : "已停用",
      model.custom ? "用户修改" : "内置基线",
      isStale(model.verified_at) ? "资料过期" : "",
      model.preview ? "Preview" : "",
    ].filter(Boolean);
    return `<article class="model-card" data-tier="${escapeHtml(model.tier)}">
      <div class="model-card-head"><span>${escapeHtml(model.provider)}</span><i>${escapeHtml(model.tier)}</i></div>
      <div class="model-badges">${badges.map((badge) => `<span>${escapeHtml(badge)}</span>`).join("")}</div>
      <h3>${escapeHtml(model.name)}</h3><code>${escapeHtml(model.model_id || model.id)}</code>
      <p>${escapeHtml(model.specialty)}</p>
      <div class="model-score"><span>推理 <b>${Math.round(Number(model.reasoning) * 100)}%</b></span><span>速度 <b>${Math.round(Number(model.speed) * 100)}%</b></span><span>可靠 <b>${Math.round(Number(model.reliability) * 100)}%</b></span></div>
      <dl><dt>价格</dt><dd>${escapeHtml(price)}</dd><dt>上下文</dt><dd>${formatTokens(model.context_window)}</dd><dt>核验</dt><dd>${escapeHtml(model.verified_at || "未标注")}</dd><dt>优势</dt><dd>${escapeHtml((model.strengths || []).join(" · ") || "未填写")}</dd></dl>
      <div class="model-card-actions">${source ? `<a href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer">资料来源 ↗</a>` : "<span></span>"}<button class="text-button edit-model" type="button" data-model-id="${escapeHtml(model.id)}">编辑</button></div>
    </article>`;
  }).join("");
  $$(".edit-model").forEach((button) => button.addEventListener("click", () => openModelEditor(button.dataset.modelId)));
}

function isStale(value) {
  if (!value) return true;
  const checked = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(checked.getTime())) return true;
  return Date.now() - checked.getTime() > 180 * 24 * 60 * 60 * 1000;
}

function safeSourceUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_error) {
    return "";
  }
}

function openModelEditor(modelId = null) {
  const model = state.catalogModels.find((item) => item.id === modelId) || null;
  state.editingModelId = model?.id || null;
  $("#model-editor-title").textContent = model ? `编辑 ${model.name}` : "新增模型";
  $("#model-validation-summary").textContent = "";
  $("#model-id").disabled = Boolean(model);
  $("#delete-model").classList.toggle("hidden", !model);
  const values = model || {
    id: "", name: "", provider: "", model_id: "", tier: "custom", specialty: "",
    reasoning: .75, speed: .75, reliability: .85, data_confidence: .6,
    capabilities: { analysis: .7, planning: .7, writing: .7, validation: .7 },
    pricing_currency: "USD", input_price_per_mtok: 0, cached_input_price_per_mtok: null,
    output_price_per_mtok: 0, context_window: 128000, max_output_tokens: 16000,
    modalities: ["text"], tools: ["function_calling", "structured_output"],
    source_url: "", verified_at: new Date().toISOString().slice(0, 10),
    strengths: [], limitations: [], local: false, preview: false, routable: false,
  };
  const mappings = {
    "model-id": values.id, "model-name": values.name, "model-provider": values.provider,
    "model-api-id": values.model_id || values.id, "model-tier": values.tier,
    "model-specialty": values.specialty, "model-reasoning": values.reasoning,
    "model-speed": values.speed, "model-reliability": values.reliability,
    "model-data-confidence": values.data_confidence ?? .6,
    "model-capabilities": JSON.stringify(values.capabilities || {}, null, 2),
    "model-currency": values.pricing_currency || "USD",
    "model-input-price": values.input_price_per_mtok ?? 0,
    "model-cached-price": values.cached_input_price_per_mtok ?? "",
    "model-output-price": values.output_price_per_mtok ?? 0,
    "model-context": values.context_window, "model-max-output": values.max_output_tokens,
    "model-modalities": (values.modalities || []).join(" | "),
    "model-tools": (values.tools || []).join(" | "),
    "model-source-url": values.source_url || "", "model-verified-at": values.verified_at || "",
    "model-strengths": (values.strengths || []).join(" | "),
    "model-limitations": (values.limitations || []).join(" | "),
  };
  Object.entries(mappings).forEach(([id, value]) => { $(`#${id}`).value = value; });
  $("#model-local").checked = Boolean(values.local);
  $("#model-preview").checked = Boolean(values.preview);
  $("#model-routable").checked = Boolean(values.routable);
  $("#model-editor").showModal();
  $("#model-name").focus();
}

function splitList(value) {
  return String(value || "").split(/[|；;]/).map((item) => item.trim()).filter(Boolean);
}

function modelFormPayload() {
  let capabilities;
  try {
    capabilities = JSON.parse($("#model-capabilities").value);
  } catch (_error) {
    throw new Error("能力向量必须是有效 JSON 对象。");
  }
  const speed = Number($("#model-speed").value);
  const existing = state.catalogModels.find((item) => item.id === state.editingModelId);
  return {
    id: existing?.id || $("#model-id").value.trim(),
    name: $("#model-name").value.trim(),
    provider: $("#model-provider").value.trim(),
    model_id: $("#model-api-id").value.trim() || $("#model-id").value.trim(),
    tier: $("#model-tier").value.trim(),
    specialty: $("#model-specialty").value.trim(),
    capabilities,
    cost_per_task: 0,
    latency_factor: Math.max(.35, 1.55 - speed),
    reliability: Number($("#model-reliability").value),
    reasoning: Number($("#model-reasoning").value),
    speed,
    data_confidence: Number($("#model-data-confidence").value),
    availability: Number(existing?.availability ?? .99),
    context_window: Number($("#model-context").value),
    max_output_tokens: Number($("#model-max-output").value),
    input_price_per_mtok: Number($("#model-input-price").value),
    cached_input_price_per_mtok: $("#model-cached-price").value === "" ? null : Number($("#model-cached-price").value),
    output_price_per_mtok: Number($("#model-output-price").value),
    pricing_currency: $("#model-currency").value.trim().toUpperCase(),
    modalities: splitList($("#model-modalities").value),
    tools: splitList($("#model-tools").value),
    strengths: splitList($("#model-strengths").value),
    limitations: splitList($("#model-limitations").value),
    source_url: $("#model-source-url").value.trim(),
    verified_at: $("#model-verified-at").value,
    local: $("#model-local").checked,
    preview: $("#model-preview").checked,
    routable: $("#model-routable").checked,
    custom: true,
  };
}

async function saveModel(event) {
  event.preventDefault();
  $("#model-validation-summary").textContent = "";
  if (!$("#model-form").reportValidity()) return;
  try {
    const model = modelFormPayload();
    const editing = Boolean(state.editingModelId);
    const response = await fetch(editing ? `/api/catalog/models/${encodeURIComponent(state.editingModelId)}` : "/api/catalog/models", {
      method: editing ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, expected_revision: state.catalogMetadata.catalog_revision }),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "保存失败");
    $("#model-editor").close();
    await loadCatalog(`${editing ? "已更新" : "已新增"} ${model.name}；新任务将使用目录修订 r${data.metadata.catalog_revision}`);
  } catch (error) {
    $("#model-validation-summary").textContent = error instanceof Error ? error.message : "保存失败";
  }
}

async function deleteEditingModel() {
  const model = state.catalogModels.find((item) => item.id === state.editingModelId);
  if (!model || !confirm(`确认删除“${model.name}”？历史运行不会受到影响。`)) return;
  try {
    const response = await fetch(`/api/catalog/models/${encodeURIComponent(model.id)}?expected_revision=${encodeURIComponent(state.catalogMetadata.catalog_revision)}`, { method: "DELETE" });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "删除失败");
    $("#model-editor").close();
    await loadCatalog(`已删除 ${model.name}`);
  } catch (error) {
    $("#model-validation-summary").textContent = error instanceof Error ? error.message : "删除失败";
  }
}

async function importCatalog(event) {
  event.preventDefault();
  const file = $("#model-import-file").files[0];
  if (!file) {
    $("#import-errors").textContent = "请选择 JSON 或 CSV 文件。";
    return;
  }
  try {
    const format = file.name.toLowerCase().endsWith(".csv") ? "csv" : "json";
    const response = await fetch("/api/catalog/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        format,
        mode: $("#import-mode").value,
        data: await file.text(),
        expected_revision: state.catalogMetadata.catalog_revision,
      }),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "导入失败");
    $("#import-dialog").close();
    $("#model-import-file").value = "";
    await loadCatalog(`导入完成：新增 ${data.result.created}，更新 ${data.result.updated}，当前共 ${data.result.total} 个模型`);
  } catch (error) {
    $("#import-errors").textContent = error instanceof Error ? error.message : "导入失败";
  }
}

async function resetCatalog() {
  if (!confirm("确认恢复内置模型目录？所有用户新增和修改都会被覆盖。")) return;
  try {
    const response = await fetch("/api/catalog/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "RESET", expected_revision: state.catalogMetadata.catalog_revision }),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "恢复失败");
    await loadCatalog("已恢复内置模型目录");
  } catch (error) {
    $("#catalog-live").textContent = error instanceof Error ? error.message : "恢复失败";
  }
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

function formatTokens(value) {
  const count = Number(value || 0);
  return count >= 1_000_000 ? `${(count / 1_000_000).toFixed(2).replace(/\.00$/, "")}M` : `${Math.round(count / 1000)}K`;
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

$("#catalog-search").addEventListener("input", renderCatalog);
$("#catalog-provider-filter").addEventListener("change", renderCatalog);
$("#catalog-status-filter").addEventListener("change", renderCatalog);
$("#add-model").addEventListener("click", () => openModelEditor());
$("#import-models").addEventListener("click", () => {
  $("#import-errors").textContent = "";
  $("#import-dialog").showModal();
});
$("#export-models").addEventListener("click", () => { location.href = "/api/catalog/export?format=json"; });
$("#export-models-csv").addEventListener("click", () => { location.href = "/api/catalog/export?format=csv"; });
$("#reset-catalog").addEventListener("click", resetCatalog);
$("#model-form").addEventListener("submit", saveModel);
$("#delete-model").addEventListener("click", deleteEditingModel);
$("#close-model-editor").addEventListener("click", () => $("#model-editor").close());
$("#cancel-model").addEventListener("click", () => $("#model-editor").close());
$("#import-form").addEventListener("submit", importCatalog);
$("#cancel-import").addEventListener("click", () => $("#import-dialog").close());

updateGoalCount();
loadCatalog();
refreshHistory({ restore: true });
