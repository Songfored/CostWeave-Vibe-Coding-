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
  evidenceConsole: null,
  evidenceKind: "all",
  evidenceRequestSeq: 0,
};

const statusLabels = {
  created: "已创建",
  planning: "正在规划",
  executing: "并行执行中",
  replanning: "重新规划中",
  completed: "已完成",
  cancelling: "正在取消",
  cancelled: "已取消",
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
$("#approve-media-project").addEventListener("click", approveMediaProject);

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
    loadEvidenceConsole(data.id);
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
    if (previousStatus !== data.status && ["completed", "failed", "cancelled"].includes(data.status)) {
      revealTerminalState(data.status);
    }
    if (["completed", "failed", "cancelled"].includes(data.status)) {
      clearInterval(state.timer);
      state.timer = null;
      setSubmitState(false);
      refreshHistory();
      loadEvidenceConsole(data.id);
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
    loadEvidenceConsole(data.id);
    if (["completed", "failed", "cancelled"].includes(data.status)) setSubmitState(false);
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
      const active = runs.find((run) => !["completed", "failed", "cancelled"].includes(run.status));
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
  renderPlanning(run.planning);
  renderForecast(run.forecast);
  renderRecovery(run.recovery_control);
  renderMediaProject(run.media_project);
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
  const economics = routing.cross_provider || {};
  const economicsSuffix = economics.pre_execution_admission
    ? ` · 预执行评估 ${Number(economics.assessments || 0)} 个候选 · 分流/拒绝 ${Number(economics.diverted_or_rejected || 0)} 个 · 预计返工 $${Number(economics.expected_rework_usd || 0).toFixed(5)}`
    : "";
  $("#routing-summary").textContent = routing.strategy
    ? `路由：${routing.strategy} · 目录 ${routing.catalog_revision || run.plan.model_snapshot || "未知"} · 最低可行 $${Number(routing.minimum_feasible_cost_usd || 0).toFixed(4)} · 当前组合 $${Number(routing.estimated_total_cost_usd || 0).toFixed(4)} · ${routing.independent_validators || 0} 个独立验收`
    : "等待模型组合决策";
  $("#routing-summary").textContent += economicsSuffix;
  const scheduling = run.scheduling || {};
    $("#scheduler-summary").textContent = run.runtime?.parallel_runtime
      ? `调度：关键路径优先 · Placement ${Number(scheduling.selected_placements || 0)} 个（备用 ${Math.max(0, Number(scheduling.placements || 0) - Number(scheduling.selected_placements || 0))}） · 预测总成本 $${Number(scheduling.forecast_total || 0).toFixed(4)} · 实际账本 $${Number(scheduling.actual_total || 0).toFixed(4)} · 验收 ${Number(scheduling.validation_results || 0)} 次（失败 ${Number(scheduling.failed_validations || 0)}） · 恢复 ${Number(scheduling.failure_records || 0)} 次 · 活跃租约 ${Number(scheduling.active_leases || 0)} · 待审批 ${Number(scheduling.pending_approvals || 0)}`
      : "调度：兼容模式（未启用资源感知并行运行时）";
  const policySnapshot = run.runtime?.policy_snapshot || routing.runtime_policy_snapshot;
  $("#runtime-policy-summary").textContent = policySnapshot
    ? `运行时策略：${policySnapshot.selected_policy_id}@${policySnapshot.selected_policy_version} · ${policySnapshot.cohort} · 启动快照 ${String(policySnapshot.policy_snapshot_hash || "").slice(0, 12)}`
    : "运行时策略：未启用 Canary，继续使用稳定路由";

  if (!state.selectedTaskId || !run.plan.tasks.some((task) => task.id === state.selectedTaskId)) {
    const finalTask = findFinalTask(run.plan.tasks);
    state.selectedTaskId = run.status === "completed" && finalTask ? finalTask.id : run.plan.tasks[0]?.id;
  }

  renderOutcome(run);
  renderDag(run.plan.tasks);
  renderArtifacts(run);
  renderTimeline(run.events || []);
  const selected = run.plan.tasks.find((task) => task.id === state.selectedTaskId) || run.plan.tasks[0];
  renderContract(selected);
}

function renderPlanning(planning) {
  const card = $("#planning-card");
  const decision = planning?.decision;
  const deliberation = planning?.deliberation;
  if (!decision) {
    card.classList.add("hidden");
    return;
  }
  const modeLabels = {
    NO_DELEGATION: "主线程直接处理",
    DIRECT_DELEGATION: "直接委派",
    SINGLE_PLANNER: "单 Planner",
    MULTI_PLANNER: "MultiPlanner",
    EXPERT_OVERRIDE: "高级规划接管",
    ASK_USER: "建议先澄清",
  };
  const budget = decision.planning_budget || {};
  const panel = decision.panel_policy || {};
  const limits = decision.limits || {};
  const difficulty = decision.difficulty_vector || {};
  const interval = difficulty.overall || {};
  const candidates = Array.isArray(decision.candidates) ? decision.candidates : [];
  const selectedCandidate = candidates.find((item) => item.mode === decision.mode);
  const escalation = decision.pre_execution_escalation || {};
  $("#planning-mode").textContent = modeLabels[decision.mode] || decision.mode;
  $("#planning-mode").dataset.mode = decision.mode;
  $("#planning-reason").textContent = (decision.reasons || []).join(" ");
  $("#planning-metrics").innerHTML = [
    ["判定置信", `${Math.round(Number(decision.confidence || 0) * 100)}%`],
    ["规划预算", `$${Number(budget.max_cost || 0).toFixed(4)} · ${Math.round(Number(budget.task_budget_ratio || 0) * 100)}%`],
    ["调用 / 时延上限", `${Number(budget.max_calls || 0)} 次 · ${Number(budget.max_latency_ms || 0)} ms`],
    ["管理深度", `${Number(limits.max_management_depth || 0)} 层（固定）`],
    ["预期总损失", selectedCandidate ? Number(selectedCandidate.expected_loss?.total || 0).toFixed(4) : "—"],
    ["MultiPlanner VoD", `${Number(decision.value_of_deliberation || 0).toFixed(4)} / 门槛 ${Number(decision.vod_margin || 0).toFixed(4)}`],
  ].map(([label, value]) => `<span><b>${escapeHtml(label)}</b>${escapeHtml(value)}</span>`).join("");
  const dimensionLabels = {
    ambiguity: "目标歧义",
    decomposition_uncertainty: "拆解不确定",
    novelty: "新颖性",
    domain_specialization: "领域专业性",
    cross_domain_coupling: "跨域耦合",
    context_multimodal_load: "上下文/多模态",
    tool_chain_depth: "工具链",
    verification_difficulty: "验收难度",
    error_loss: "错误损失",
    irreversibility: "不可逆性",
    concurrency_deadline_pressure: "并发/期限",
  };
  const dimensions = Object.entries(difficulty.dimensions || {})
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 6);
  $("#planning-difficulty").innerHTML = difficulty.overall
    ? `<div class="difficulty-interval">
        <span>难度区间</span>
        <strong>${Math.round(Number(interval.lower || 0) * 100)}–${Math.round(Number(interval.upper || 0) * 100)}%</strong>
        <small>中心 ${Math.round(Number(interval.expected || 0) * 100)}% · 判断置信 ${Math.round(Number(difficulty.confidence || 0) * 100)}%</small>
      </div>
      <div class="difficulty-signals">${dimensions.map(([key, value]) => `<span><b>${escapeHtml(dimensionLabels[key] || key)}</b><i style="--signal:${Math.round(Number(value) * 100)}%"></i><em>${Math.round(Number(value) * 100)}%</em></span>`).join("")}</div>`
    : "";
  $("#planning-escalation").textContent = escalation.required
    ? `执行前升级：${(escalation.reasons || []).join("；")}`
    : "执行前升级门未触发：当前置信度、风险与本地容量允许按已选路线继续。";
  $("#planning-escalation").dataset.required = escalation.required ? "true" : "false";
  $("#planning-candidates").innerHTML = candidates.map((item) => {
    const selected = item.mode === decision.mode;
    const rejection = (item.hard_rejections || []).join("；");
    return `<article data-selected="${selected}" data-feasible="${Boolean(item.feasible)}">
      <div><strong>${escapeHtml(modeLabels[item.mode] || item.mode)}</strong><span>${item.feasible ? "可行" : "淘汰"}</span></div>
      <p>成功 ${Math.round(Number(item.expected_success || 0) * 100)}% · 下界 ${Math.round(Number(item.success_lower_bound || 0) * 100)}%</p>
      <small>总损失 ${Number(item.expected_loss?.total || 0).toFixed(4)} · 规划 $${Number(item.planning_cost || 0).toFixed(4)}</small>
      ${rejection ? `<em>${escapeHtml(rejection)}</em>` : ""}
    </article>`;
  }).join("");
  const assignments = Array.isArray(panel.selected_assignments) ? panel.selected_assignments : [];
  $("#planning-roles").innerHTML = assignments.length
    ? assignments.map((item) => `<article>
        <strong>${escapeHtml(humanize(item.role_id || "planner"))}</strong>
        <span>${escapeHtml(item.model_id || item.worker_id || "未绑定候选")}</span>
        <small>${escapeHtml(item.provider || "provider-neutral")} · ${escapeHtml(item.deployment || "未部署")} · 匹配 ${Math.round(Number(item.score || 0) * 100)}%</small>
      </article>`).join("")
    : '<p>本模式不创建额外 Planner，避免无意义管理开销。</p>';
  const deliberationPanel = $("#planning-deliberation");
  if (deliberation) {
    const scorecards = Array.isArray(deliberation.scorecards) ? deliberation.scorecards : [];
    const anonymous = Array.isArray(deliberation.anonymous_proposals) ? deliberation.anonymous_proposals : [];
    const conflicts = Array.isArray(deliberation.conflicts) ? deliberation.conflicts : [];
    const adjudication = deliberation.adjudication || {};
    const proposalsByAlias = new Map(anonymous.map((item) => [item.alias, item]));
    $("#planning-proposals").innerHTML = scorecards.length
      ? scorecards.map((score) => {
          const proposal = proposalsByAlias.get(score.alias) || {};
          const selected = (adjudication.selected_aliases || []).includes(score.alias);
          return `<article data-selected="${selected}" data-hard-pass="${Boolean(score.hard_constraints_passed)}">
            <div><strong>Proposal ${escapeHtml(score.alias || "?")}</strong><span>${Math.round(Number(score.total || 0) * 100)}%</span></div>
            <p>${escapeHtml((proposal.decomposition_outline || []).slice(0, 2).join(" · "))}</p>
            <small>${score.hard_constraints_passed ? "Hard constraints passed" : `Failed: ${(score.failed_constraints || []).join(" · ")}`}</small>
          </article>`;
        }).join("")
      : "<p>No planner call was permitted by the planning budget.</p>";
    $("#planning-conflicts").innerHTML = conflicts.length
      ? conflicts.map((item) => `<article data-severity="${escapeHtml(item.severity || "low")}">
          <strong>${escapeHtml(humanize(item.conflict_type || "conflict"))}</strong>
          <span>${escapeHtml((item.aliases || []).join(" ↔ "))} · ${escapeHtml(item.severity || "")}</span>
          <small>${escapeHtml((item.evidence || []).join(" · "))}</small>
        </article>`).join("")
      : "<p>No material proposal conflict detected.</p>";
    $("#planning-adjudication").textContent = `${humanize(adjudication.outcome || "not required")} · ${Number(adjudication.rounds_used || 0)}/1 round · ${(adjudication.selected_aliases || []).join(" + ") || "none"}`;
    $("#planning-adjudication").dataset.outcome = adjudication.outcome || "not_required";
    deliberationPanel.dataset.parallel = deliberation.parallel ? "true" : "false";
    deliberationPanel.classList.remove("hidden");
  } else {
    deliberationPanel.classList.add("hidden");
    $("#planning-proposals").innerHTML = "";
    $("#planning-conflicts").innerHTML = "";
    $("#planning-adjudication").textContent = "";
  }
  const rejected = Object.entries(decision.rejected_modes || {})
    .map(([mode, reason]) => `<li><b>${escapeHtml(modeLabels[mode] || mode)}</b>：${escapeHtml(reason)}</li>`)
    .join("");
  $("#planning-details").innerHTML = `
    <ul>${rejected}</ul>
    <p>最多 ${Number(limits.max_planners_per_round || 0)} 个 Planner、${Number(limits.max_adjudication_rounds || 0)} 次裁决、${Number(limits.max_full_replans || 0)} 次全局重规划；Planner 不得再创建 Planner。</p>
    <small>规则优先、置信度感知 · ${escapeHtml(decision.policy_version || "")} · 指纹 ${escapeHtml(String(decision.fingerprint || "").slice(0, 16))}</small>`;
  card.classList.remove("hidden");
}

function renderForecast(forecast) {
  const card = $("#forecast-card");
  if (!forecast) {
    card.classList.add("hidden");
    return;
  }
  const labels = {
    first_pass_success: "首次成功",
    success_after_repair: "修复后成功",
    success_after_upgrade: "升级后成功",
    human_intervention: "人工介入",
    final_failure: "最终失败",
  };
  $("#forecast-probabilities").innerHTML = Object.entries(forecast.probabilities || {})
    .map(([key, value]) => `<article data-outcome="${escapeHtml(key)}"><span>${escapeHtml(labels[key] || key)}</span><strong>${Math.round(Number(value) * 100)}%</strong></article>`)
    .join("");
  $("#forecast-intervals").innerHTML = [
    ["质量", `${Math.round(Number(forecast.quality?.p50 || 0) * 100)}% / ${Math.round(Number(forecast.quality?.p95 || 0) * 100)}%`],
    ["成本 P50 / P95", `$${Number(forecast.cost?.p50 || 0).toFixed(4)} / $${Number(forecast.cost?.p95 || 0).toFixed(4)}`],
    ["时间 P50 / P95", `${Number(forecast.completion_seconds?.p50 || 0).toFixed(2)}s / ${Number(forecast.completion_seconds?.p95 || 0).toFixed(2)}s`],
    ["关键误放", `${(Number(forecast.critical_false_pass_probability || 0) * 100).toFixed(2)}%`],
  ].map(([label, value]) => `<span><b>${escapeHtml(label)}</b>${escapeHtml(value)}</span>`).join("");
  $("#forecast-confidence").textContent = `预测置信 ${Math.round(Number(forecast.confidence || 0) * 100)}% · ${Number(forecast.sample_size || 0)} 次仿真`;
  const ood = forecast.out_of_distribution
    ? `分布外提醒：${(forecast.ood_reasons || []).join("、")}`
    : "当前任务位于已知策略范围内";
  $("#forecast-note").textContent = `${ood} · 固定种子 ${forecast.seed} · ${forecast.policy_revision}`;
  card.classList.remove("hidden");
}

function renderRecovery(control) {
  const card = $("#recovery-card");
  const checkpoint = control?.latest_checkpoint;
  if (!checkpoint) {
    card.classList.add("hidden");
    return;
  }
  const probability = Number(checkpoint.pcomplete || 0);
  const threshold = Number(checkpoint.threshold || 0);
  const pollution = control.latest_pollution_report || {};
  const invalidated = pollution.invalidated_task_ids || [];
  const preserved = pollution.preserved_task_ids || [];
  const state = $("#recovery-state");
  state.textContent = checkpoint.recovery_recommended ? "低于目标门槛 · 监控中" : "当前门禁通过";
  state.dataset.recommended = checkpoint.recovery_recommended ? "true" : "false";
  $("#recovery-meter").style.width = `${Math.max(0, Math.min(100, probability * 100))}%`;
  $("#recovery-metrics").innerHTML = [
    ["完成概率 / 门槛", `${Math.round(probability * 100)}% / ${Math.round(threshold * 100)}%`],
    ["交接预检", `${Number(control.handoffs_admitted || 0)} 通过 · ${Number(control.handoffs_rejected || 0)} 拒绝`],
    ["污染隔离", pollution.failed_task_id ? `${invalidated.length} 受影响 · ${preserved.length} 保留` : "尚无失败传播"],
    ["全局重规划", `${Number(control.global_replans_remaining || 0)} 次可用`],
  ].map(([label, value]) => `<span><b>${escapeHtml(label)}</b>${escapeHtml(value)}</span>`).join("");
  const signals = checkpoint.signals || {};
  const evidence = [
    `检查点 ${checkpoint.stage || "unknown"}`,
    `验收覆盖 ${Math.round(Number(signals.constraint_coverage || 0) * 100)}%`,
    `证据完整 ${Math.round(Number(signals.evidence_completeness || 0) * 100)}%`,
    `预算消耗 ${Math.round(Number(signals.budget_burn || 0) * 100)}%`,
    `${Number(signals.blocked_dependencies || 0)} 个阻塞依赖`,
  ];
  $("#recovery-note").textContent = evidence.join(" · ");
  card.classList.remove("hidden");
}

function renderMediaProject(project) {
  const card = $("#media-project-card");
  if (!project) {
    card.classList.add("hidden");
    return;
  }
  const profile = project.profile || {};
  const summary = project.render_summary || {};
  const cost = project.cost || {};
  const shots = Array.isArray(project.shots) ? project.shots : [];
  const labels = {
    awaiting_preview_approval: "等待预览批准",
    completed: "媒体流程已完成",
    ready: "可生成",
  };
  $("#media-project-status").textContent = labels[project.status] || project.status;
  $("#media-project-status").dataset.status = project.status;
  $("#media-project-note").textContent = profile.requires_film_dag
    ? `已建立 ${shots.length} 镜头的受控生产 DAG；当前只生成关键帧和低清动态预览。所有内容均为元数据模拟，不包含真实图片或视频。`
    : "简单图片使用轻量流程，不创建电影级分镜 DAG。所有内容均为元数据模拟。";
  $("#media-project-metrics").innerHTML = [
    ["生产策略", humanize(profile.strategy || "supervised_production")],
    ["镜头 / 时长", `${shots.length} 镜头 · ${Number(profile.duration_seconds || 0).toFixed(1)} 秒`],
    ["预览尝试", `${Number(summary.keyframe_attempts || 0)} 关键帧 · ${Number(summary.animatic_attempts || 0)} 动态预览`],
    ["终稿状态", `${Number(summary.accepted_final_shots || 0)} 已通过 · ${Number(summary.rejected_final_attempts || 0)} 次局部驳回`],
    ["媒体成本", `$${Number(cost.total || 0).toFixed(4)}`],
    ["单位成果", cost.cost_per_accepted_second == null ? "待终稿验收" : `$${Number(cost.cost_per_accepted_second).toFixed(4)} / 有效秒`],
  ].map(([label, value]) => `<span><b>${escapeHtml(label)}</b>${escapeHtml(value)}</span>`).join("");
  $("#media-shot-list").innerHTML = shots.map((shot) => {
    const contract = shot.contract || {};
    const attempts = Array.isArray(shot.attempts) ? shot.attempts : [];
    const finalAttempts = attempts.filter((item) => item.stage === "final");
    const dependency = (contract.reference_dependencies || []).length
      ? `依赖 ${(contract.reference_dependencies || []).join("、")}`
      : "可并行起步";
    return `<article data-status="${escapeHtml(shot.status)}">
      <div><strong>镜头 ${Number(contract.sequence || shot.sequence || 0).toString().padStart(2, "0")}</strong><span>${escapeHtml(taskLabels[shot.status] || shot.status)}</span></div>
      <p>${escapeHtml(contract.shot_type || "image")} · ${Number(contract.duration_seconds || 0).toFixed(2)}s · ${escapeHtml(contract.resolution || "")}</p>
      <small>${escapeHtml(dependency)} · 终稿尝试 ${finalAttempts.length}</small>
    </article>`;
  }).join("");
  const approve = $("#approve-media-project");
  approve.classList.toggle("hidden", !profile.requires_film_dag || project.status === "completed");
  approve.disabled = false;
  $("#media-approval-feedback").textContent = project.status === "completed"
    ? `已验收 ${Number(summary.accepted_final_shots || 0)} 个镜头`
    : "终稿渲染必须经过此批准门";
  card.classList.remove("hidden");
}

async function approveMediaProject() {
  const runId = state.currentRun?.id;
  if (!runId) return;
  const button = $("#approve-media-project");
  button.disabled = true;
  $("#media-approval-feedback").textContent = "正在执行终稿模拟与连续性验收…";
  try {
    const response = await fetch(`/api/v1/media/projects/${encodeURIComponent(runId)}/approve`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": `media-approve-${runId}`,
      },
      body: "{}",
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法批准媒体项目");
    await loadRun(runId, false);
    $("#media-approval-feedback").textContent = "终稿模拟和连续性验收已完成";
  } catch (error) {
    button.disabled = false;
    $("#media-approval-feedback").textContent = error instanceof Error ? error.message : "批准失败";
  }
}

function renderArtifacts(run) {
  const artifacts = Array.isArray(run.artifacts) ? run.artifacts : [];
  $("#artifact-count").textContent = `${artifacts.length} 个不可变成果`;
  $("#artifact-graph").innerHTML = artifacts.length ? artifacts.map((artifact) => `
    <article data-status="${escapeHtml(artifact.status)}">
      <div><strong>${escapeHtml(artifact.artifact_type)}</strong><span>${escapeHtml(artifact.status)}</span></div>
      <p>${escapeHtml(artifact.producer_task_id)}</p>
      <code title="${escapeHtml(artifact.content_hash)}">${escapeHtml(artifact.content_hash.slice(0, 16))}…</code>
      <small>${escapeHtml(artifact.producer_model || "未记录模型")} · 输入 ${artifact.input_artifact_refs.length} · 计划 v${artifact.plan_revision}</small>
    </article>`).join("") : "<p>暂无 Artifact；节点只有通过结构化验收后才会解锁下游。</p>";
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
          data-queue-state="${escapeHtml(task.queue_state || "pending")}"
          aria-pressed="${task.id === state.selectedTaskId}">
          <strong>${escapeHtml(task.title)}</strong>
          <small>${escapeHtml(modelLabel(task.selected_worker))}</small>
          <span class="task-state">${escapeHtml(
            task.queue_state === "waiting_approval"
              ? "等待审批"
              : task.queue_state === "queued"
                ? "资源排队"
                : taskLabels[task.status] || task.status
          )}</span>
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
  const placement = task.placement || {};
  const profile = placement.inference_profile || {};
  const cost = placement.forecast_cost?.p50 || task.forecast_cost_breakdown || {};
  const costItems = [
    ["模型调用", cost.provider_inference],
    ["本地算力", cost.local_compute],
    ["能源", cost.energy],
    ["稀缺容量", cost.scarce_capacity],
    ["排队机会", cost.queue_opportunity],
    ["交接", cost.handoff],
    ["验收合并", cost.validation_merge],
    ["预期返工", cost.expected_rework],
  ].filter(([, value]) => Number(value || 0) > 0)
    .map(([label, value]) => `${label} $${Number(value).toFixed(5)}`)
    .join(" · ");
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
  const typedDependencies = (task.dependencies || []).map((dependency) =>
    `${dependency} [${task.dependency_types?.[dependency] || "hard"}]`);
  const artifactOutputs = (task.output_artifact_specs || []).map((spec) =>
    `${spec.artifact_type} · ${spec.schema_ref}`);
  const successCriteria = (task.success_contract?.criteria || []);
  const criticalCriteria = successCriteria
    .filter((criterion) => criterion.critical)
    .map((criterion) => criterion.text);
  const criterionResults = (task.validation?.criterion_results || []).map((criterion) =>
    `${criterion.passed ? "✓" : "✕"} ${criterion.critical ? "[关键] " : ""}${criterion.criterion}${criterion.independent_evidence ? " · 独立证据" : ""}`);
  const recoveryRecords = (task.recovery_history || []).map((record) =>
    `${record.failure?.classification || record.classification || "unknown"} → ${record.decision?.action || record.action || "stop"} · ${record.decision?.scope || record.scope || "none"}`);
  const preflight = task.handoff_receipt || {};
  const preflightFailures = (preflight.failures || []).map((item) =>
    `${item.name}: ${item.evidence}`);
  $("#contract").innerHTML = `
    <div class="contract-heading">
      <div><h4>${escapeHtml(task.title)}</h4><code>${escapeHtml(task.id)} · 计划 v${task.plan_version}</code></div>
      <span class="contract-status" data-status="${escapeHtml(task.status)}">${escapeHtml(taskLabels[task.status] || task.status)}</span>
    </div>
    <dl>
      <dt>目标</dt><dd>${escapeHtml(task.objective)}</dd>
      <dt>依赖</dt><dd>${list(typedDependencies)}</dd>
      <dt>输入 Artifact</dt><dd>${list(task.input_artifact_refs)}</dd>
      <dt>输出 Artifact</dt><dd>${list(artifactOutputs)}</dd>
      <dt>成果 ID</dt><dd>${list(task.artifact_ids)}</dd>
      <dt>交接消息</dt><dd>${escapeHtml(task.handoff_message_id || "尚未发送")}</dd>
      <dt>接收方预检</dt><dd>${preflight.admitted === true
        ? `${escapeHtml(preflight.receiver_id || "已分配")} · ${(preflight.checks || []).length} 项通过`
        : preflight.admitted === false
          ? `已拒绝 · ${list(preflightFailures)}`
          : "尚未执行"}</dd>
      <dt>所需能力</dt><dd>${list(task.required_capabilities)}</dd>
      <dt>验收标准</dt><dd>${list(task.acceptance_criteria)}</dd>
      <dt>关键成功标准</dt><dd>${list(criticalCriteria)}</dd>
      <dt>输出格式</dt><dd>${list(task.output_schema)}</dd>
      <dt>选定模型</dt><dd>${escapeHtml(modelLabel(task.selected_worker))}</dd>
      <dt>资源组合</dt><dd>${placement.placement_id
        ? `${escapeHtml(placement.provider)} / ${escapeHtml(placement.resource_pool)} · ${escapeHtml(profile.reasoning_effort || "medium")} 推理 · ${escapeHtml(profile.quantization || "provider-default")} · 备用 ${Number(task.backup_placements?.length || 0)} 个`
        : "等待资源组合决策"}</dd>
      <dt>端到端成本</dt><dd>${costItems || "暂无成本分解"}${placement.forecast_cost?.p95_total != null ? ` · P95 $${Number(placement.forecast_cost.p95_total).toFixed(5)}` : ""}</dd>
      <dt>路由理由</dt><dd>${escapeHtml(task.routing_rationale || "等待路由")}</dd>
      <dt>执行预测</dt><dd>均值 ${Math.round(task.predicted_success * 100)}% · 保守下界 ${Math.round(Number(task.predicted_success_lower_bound || 0) * 100)}% · 路由置信 ${Math.round(Number(task.route_confidence || 0) * 100)}% · $${Number(task.estimated_cost || 0).toFixed(4)} · ${task.estimated_latency_ms}ms</dd>
      <dt>任务画像</dt><dd>难度 L${task.difficulty}/10 · 风险 ${escapeHtml(task.risk_level)} · 不确定性 ${Math.round(Number(task.uncertainty || 0) * 100)}% · 关键度 ${Math.round(Number(task.criticality || 0) * 100)}% · 输入约 ${task.estimated_input_tokens} tokens · 输出约 ${task.estimated_output_tokens} tokens</dd>
      <dt>资源合同</dt><dd>${Number(task.resource_slots || 1)} 槽 · CPU ${Number(task.cpu_cores || 0)} · GPU ${Number(task.gpu_count || 0)} · VRAM ${Number(task.vram_mb || 0)} MB</dd>
      <dt>调度状态</dt><dd>${escapeHtml(task.queue_state || "pending")}${task.blocked_reason ? ` · ${escapeHtml(task.blocked_reason)}` : ""}${task.reservation_id ? ` · ${escapeHtml(task.reservation_id)}` : ""}</dd>
      <dt>审批</dt><dd>${task.requires_approval ? escapeHtml(task.approval_id || "等待生成审批单") : "不需要"}</dd>
      ${task.validation ? `<dt>最近验收</dt><dd>${escapeHtml((task.validation.findings || []).join("；"))} · 误放风险 ${Math.round(Number(task.validation.false_positive_risk || 0) * 100)}% · 误拒风险 ${Math.round(Number(task.validation.false_negative_risk || 0) * 100)}%</dd>` : ""}
      <dt>逐标准证据</dt><dd>${list(criterionResults)}</dd>
      <dt>恢复记录</dt><dd>${list(recoveryRecords)}</dd>
    </dl>
    <div class="candidate-list"><span>候选排序</span><ol>${candidates || "<li>暂无候选</li>"}</ol></div>
    <div class="candidate-list rejected-list"><span>硬约束淘汰</span><ol>${rejections || "<li>无</li>"}</ol></div>
    <details class="handoff-prompt"><summary>查看发送给执行模型的任务合同</summary><pre>${escapeHtml(task.handoff_prompt || "暂无")}</pre></details>
    ${resultSection}`;
}

async function loadCatalog(announcement = "") {
  try {
    const response = await fetch("/api/v1/catalog");
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
    const response = await fetch(editing ? `/api/v1/catalog/models/${encodeURIComponent(state.editingModelId)}` : "/api/v1/catalog/models", {
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
    const response = await fetch(`/api/v1/catalog/models/${encodeURIComponent(model.id)}?expected_revision=${encodeURIComponent(state.catalogMetadata.catalog_revision)}`, { method: "DELETE" });
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
    const response = await fetch("/api/v1/catalog/import", {
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
    const response = await fetch("/api/v1/catalog/reset", {
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

async function loadPolicyRegistry() {
  const live = $("#registry-live");
  if (!live) return;
  try {
    const response = await fetch("/api/v1/policies/registry");
    const policy = await readJson(response);
    if (!response.ok) throw new Error("无法读取策略注册表");
    const counts = policy.runtime_counts || {};
    $("#registry-boundary").textContent = policy.registry_active_is_runtime_active
      ? "警告：注册表状态已连接真实路由"
      : "注册表状态不改变真实路由";
    $("#registry-policy-count").textContent = String(counts.policy_registry_policies || 0);
    $("#registry-gate-count").textContent = String(counts.policy_registry_gate_reports || 0);
    $("#registry-recovery-count").textContent = String(
      (counts.policy_registry_frozen_transitions || 0) + (counts.policy_registry_rollbacks || 0)
    );
    $("#registry-route-change-count").textContent = String(counts.policy_registry_runtime_route_changes || 0);
    $("#registry-gate-note").textContent = `${counts.policy_registry_eligible_gates || 0} 个证据门通过`;
    live.textContent = policy.current_registry_pointer
      ? `当前注册表指针：${policy.current_registry_pointer.registry_policy_id}；真实运行路线未改变。`
      : "尚无激活的注册表指针；系统继续使用既有运行路线。";
  } catch (error) {
    live.textContent = error instanceof Error ? error.message : "策略注册表暂不可用";
  }
}

async function loadCanaryPolicy() {
  const live = $("#canary-live");
  if (!live) return;
  try {
    const response = await fetch("/api/v1/canary/policy");
    const policy = await readJson(response);
    if (!response.ok) throw new Error("无法读取 Canary 控制状态");
    const counts = policy.runtime_counts || {};
    const current = policy.current_runtime_state;
    $("#canary-stage").textContent = current ? `${Number(current.stage_pct || 0)}%` : "OFF";
    $("#canary-runtime-state").textContent = current
      ? humanize(current.event_type || "unknown")
      : "尚无运行时部署";
    $("#canary-assignment-count").textContent = String(counts.canary_assignments || 0);
    $("#canary-rollback-count").textContent = String(counts.canary_runtime_rollbacks || 0);
    $("#canary-route-change-count").textContent = String(counts.canary_runtime_policy_changes || 0);
    $("#canary-boundary").textContent = policy.runtime_enabled
      ? "已显式启用 · 仍受真实证据与阶段门约束"
      : "默认关闭 · 等待真实证据与显式启用";
    live.textContent = current
      ? `部署 ${current.deployment_id} · ${current.event_type} · 高风险候选 ${counts.canary_high_risk_candidate_assignments || 0} · 外部调用 ${counts.canary_provider_calls || 0}`
      : "没有满足启动条件的 Canary；不会改变现有任务路线，也不会产生外部模型调用。";
  } catch (error) {
    live.textContent = error instanceof Error ? error.message : "Canary 控制状态暂不可用";
  }
}

async function loadSecurityBoundary() {
  const live = $("#security-live");
  if (!live) return;
  try {
    const response = await fetch("/api/v1/security/policy");
    const policy = await readJson(response);
    if (!response.ok) throw new Error("无法读取安全边界状态");
    const project = policy.project || {};
    const counts = policy.runtime_counts || {};
    const active = project.status !== "revoked";
    $("#security-network-state").textContent = policy.external_network_default_enabled ? "ON" : "OFF";
    $("#security-project-state").textContent = active ? "已隔离" : "已撤销";
    $("#security-project-id").textContent = project.project_id || policy.default_project_id || "local-default";
    $("#security-secret-state").textContent = policy.secret_values_persisted ? "警告" : "0";
    $("#security-ambiguous-count").textContent = String(counts.ambiguous_external_side_effects || 0);
    $("#security-boundary-status").textContent = active
      ? "默认拒绝 · 当前项目可用"
      : "访问已撤销 · 历史行尚未物理擦除";
    live.textContent = `已登记 ${Array.isArray(policy.adapter_registry) ? policy.adapter_registry.length : 0} 个适配器合同；外部副作用合同 ${counts.external_side_effect_contracts || 0} 个，歧义 ${counts.ambiguous_external_side_effects || 0} 个。`;
  } catch (error) {
    live.textContent = error instanceof Error ? error.message : "安全边界状态暂不可用";
  }
}

async function loadResilienceStatus() {
  const live = $("#resilience-live");
  if (!live) return;
  try {
    const response = await fetch("/api/v1/resilience/status");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法读取韧性验收状态");
    const integrity = data.integrity || {};
    const counts = data.counts || {};
    const runtime = data.runtime || {};
    const snapshot = runtime.logical_snapshot || {};
    $("#resilience-integrity").textContent = integrity.healthy ? "通过" : "异常";
    $("#resilience-event-count").textContent = `${Number(integrity.events_checked || 0)} 个事件`;
    $("#resilience-campaign-count").textContent = String(counts.resilience_campaigns || 0);
    $("#resilience-failover").textContent = `≤${Number(data.provider_failover_deadline_seconds || 30)}s`;
    $("#resilience-recovery").textContent = data.validated_unpolluted_work_reexecuted ? "需复核" : "保留成果";
    $("#resilience-status").textContent = integrity.healthy
      ? "事件连续 · 投影可信"
      : "完整性门未通过";
    live.textContent = `migration ${Number(runtime.migrations || 0)} 项；事件哈希覆盖 ${(Number(snapshot.event_integrity_coverage ?? 0) * 100).toFixed(1)}%；备份 ${Number(runtime.backups || 0)} 份。故障活动仍是 E0 模拟或 E1 回放，不计入真实收益。`;
  } catch (error) {
    live.textContent = error instanceof Error ? error.message : "韧性状态暂不可用";
  }
}

async function loadStableReleaseStatus() {
  const live = $("#stable-release-live");
  if (!live) return;
  try {
    const response = await fetch("/api/v1/release/status");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法读取稳定版状态");
    const runtime = data.runtime || {};
    const replay = data.replay_audit || {};
    const gates = Array.isArray(data.gates) ? data.gates : [];
    const passed = gates.filter((gate) => gate.passed).length;
    const ready = Boolean(data.engineering_ready);
    $("#stable-engineering-state").textContent = ready ? "通过" : "阻断";
    $("#stable-runtime-schema").textContent = runtime.schema_version || "未知";
    $("#stable-migration-count").textContent = `${Number(runtime.migrations || 0)} 项 migration`;
    $("#stable-replay-state").textContent = replay.healthy ? "一致" : "异常";
    $("#stable-replay-count").textContent = `${Number(replay.runs_checked || 0)} 个运行`;
    $("#stable-commercial-state").textContent = data.commercial_claims_allowed ? "已支持" : "未验证";
    $("#stable-release-status").textContent = ready
      ? "v0.5 工程稳定 · 生产证据待补"
      : "稳定版硬门未通过";
    live.textContent = `${passed}/${gates.length} 个运行时硬门通过；不可变发布证明 ${Array.isArray(data.attestations) ? data.attestations.length : 0} 份。当前范围是离线单进程控制面，不代表真实 Provider 或世界领先性能。`;
  } catch (error) {
    live.textContent = error instanceof Error ? error.message : "稳定版状态暂不可用";
  }
}

const evidenceKindLabels = {
  actual: "实际记录",
  predicted: "预测值",
  simulated: "模拟值",
  missing: "缺失值",
};

function formatEvidenceValue(metric) {
  if (metric.evidence_kind === "missing" || metric.value === null || metric.value === undefined) return "缺失";
  if (typeof metric.value === "string") return humanize(metric.value);
  const value = Number(metric.value);
  if (!Number.isFinite(value)) return "不可用";
  if (metric.unit === "probability") return `${(value * 100).toFixed(1).replace(/\.0$/, "")}%`;
  if (metric.unit === "percent") return `${(value * 100).toFixed(1).replace(/\.0$/, "")}%`;
  if (metric.unit === "USD") return `$${value.toFixed(value < 1 ? 4 : 2)}`;
  if (metric.unit === "milliseconds") return formatTime(value);
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2, notation: Math.abs(value) >= 10_000 ? "compact" : "standard" }).format(value);
}

function renderEvidenceConsole() {
  const data = state.evidenceConsole;
  if (!data) return;
  const summary = data.summary || {};
  const coverage = data.coverage || {};
  const sources = data.sources || [];
  const sourceMap = Object.fromEntries(sources.map((item) => [item.source_id, item]));
  const scopeRunId = data.scope?.run_id;
  const claimable = (data.business_claims || []).filter((item) => item.status === "supported");
  $("#evidence-scope").textContent = scopeRunId ? `任务 ${scopeRunId}` : "工作区证据";
  $("#evidence-level").textContent = summary.evidence_level || "E0";
  $("#evidence-real-sample").textContent = String(summary.real_effect_sample_size || 0);
  $("#evidence-source-count").textContent = String(sources.length);
  $("#evidence-claim-count").textContent = String(claimable.length);
  ["actual", "predicted", "simulated", "missing"].forEach((kind) => {
    const target = $(`#evidence-count-${kind}`);
    if (target) target.textContent = String(coverage[kind] || 0);
  });

  const visibleMetrics = (data.metrics || []).filter((item) => state.evidenceKind === "all" || item.evidence_kind === state.evidenceKind);
  $("#evidence-metrics").innerHTML = visibleMetrics.length
    ? visibleMetrics.map((metric) => {
      const source = sourceMap[metric.source_id] || {};
      const sample = Number(metric.sample_size || 0);
      const ci = metric.confidence_interval
        ? `CI ${escapeHtml(metric.confidence_interval.lower)}–${escapeHtml(metric.confidence_interval.upper)}`
        : "CI 未提供";
      const detail = metric.evidence_kind === "missing"
        ? metric.missing_reason || "数据尚未收集"
        : `n=${sample} · ${ci}`;
      return `<article class="evidence-metric" data-evidence-kind="${escapeHtml(metric.evidence_kind)}">
        <header><span>${escapeHtml(metric.label)}</span><i class="evidence-kind-badge">${escapeHtml(evidenceKindLabels[metric.evidence_kind] || metric.evidence_kind)} · ${escapeHtml(metric.evidence_level)}</i></header>
        <strong>${escapeHtml(formatEvidenceValue(metric))}</strong>
        <small>${escapeHtml(detail)}</small>
        <small class="metric-source">${escapeHtml(source.label || metric.source_id)} · ${escapeHtml(source.data_version || "未知版本")}</small>
      </article>`;
    }).join("")
    : '<p class="history-empty">当前筛选下没有指标。</p>';

  $("#evidence-claims").innerHTML = (data.business_claims || []).map((claim) => {
    const families = (claim.applicable_task_families || []).join("、") || "尚未建立";
    const interval = claim.confidence_interval
      ? `${claim.confidence_interval.lower}–${claim.confidence_interval.upper}`
      : "未形成";
    return `<div class="evidence-claim"><div><b>${escapeHtml(claim.label)}</b><em>${claim.status === "supported" ? "已支持" : "证据不足"}</em></div>
      <p>${escapeHtml(claim.evidence_level)} · n=${Number(claim.sample_size || 0)} · CI ${escapeHtml(interval)} · 任务族 ${escapeHtml(families)}</p>
      <p>${escapeHtml(claim.reason || "")}</p></div>`;
  }).join("");

  $("#evidence-sources").innerHTML = sources.map((source) => `<div class="evidence-source">
    <b>${escapeHtml(source.label)}</b>
    <span>${escapeHtml(source.trace?.api_path || "无路径")}<br>${escapeHtml(source.data_version || "未知版本")}</span>
    <small>${Number(source.record_count || 0)} 条<br>${escapeHtml(source.evidence_level)} / ${escapeHtml(evidenceKindLabels[source.evidence_kind] || source.evidence_kind)}</small>
  </div>`).join("");
  $("#evidence-generated-at").textContent = `快照 ${formatDate(data.generated_at)}`;
  $("#evidence-live").textContent = `${summary.message || "证据快照已加载"} 外部 Provider 尝试 ${Number(summary.external_provider_attempts || 0)} 次；快照 ${String(data.snapshot_hash || "").slice(0, 12)}。`;
}

async function loadEvidenceConsole(runId = state.runId) {
  const live = $("#evidence-live");
  if (!live) return;
  const requestSeq = ++state.evidenceRequestSeq;
  live.textContent = "正在刷新证据来源与口径…";
  try {
    const query = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
    const response = await fetch(`/api/v1/evidence/console${query}`);
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法读取统一证据快照");
    if (requestSeq !== state.evidenceRequestSeq) return;
    state.evidenceConsole = data;
    renderEvidenceConsole();
  } catch (error) {
    if (requestSeq !== state.evidenceRequestSeq) return;
    live.textContent = error instanceof Error ? error.message : "统一证据控制台暂不可用";
  }
}

async function loadDecisionIntelligenceStatus() {
  const statusNode = $("#decision-intelligence-status");
  if (!statusNode) return;
  try {
    const response = await fetch("/api/v1/tournaments/status");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法读取离线竞赛状态");
    $("#decision-intelligence-cases").textContent = String(data.case_count ?? "—");
    $("#decision-intelligence-policies").textContent = "4";
    $("#decision-intelligence-claims").textContent = data.evidence_level === "E0-simulated" ? "未通过" : "待验收";
    $("#decision-intelligence-live").textContent = String(data.provider_calls ?? 0);
    statusNode.textContent = `${data.evidence_level || "E0"} · 离线就绪`;
  } catch (error) {
    statusNode.textContent = "离线竞赛暂不可用";
    $("#decision-intelligence-feedback").textContent = error instanceof Error ? error.message : "无法读取状态";
  }
}

async function runOfflineTournament() {
  const button = $("#run-offline-tournament");
  const feedback = $("#decision-intelligence-feedback");
  if (!button || !feedback) return;
  button.disabled = true;
  feedback.textContent = "正在运行本地竞赛…";
  const suffix = (window.crypto && typeof window.crypto.randomUUID === "function")
    ? window.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  try {
    const response = await fetch("/api/v1/tournaments/offline", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": `ui-tournament-${suffix}`,
      },
      body: JSON.stringify({ confirm: "RUN_OFFLINE_TOURNAMENT", seed: 600, bootstrap_samples: 200 }),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "离线竞赛未完成");
    const report = data.report || {};
    const claims = Object.values(report.claim_gates || {});
    $("#decision-intelligence-cases").textContent = String(report.policy_metrics?.["strong-only"]?.total_cases ?? "—");
    $("#decision-intelligence-policies").textContent = String(Object.keys(report.policy_metrics || {}).length);
    $("#decision-intelligence-claims").textContent = claims.some((item) => item.passed) ? "部分通过" : "未通过";
    $("#decision-intelligence-live").textContent = String(report.provider_calls ?? 0);
    $("#decision-intelligence-status").textContent = data.replayed ? "已复用相同结果" : "竞赛完成 · E0 模拟";
    feedback.textContent = `报告 ${String(report.report_hash || "").slice(0, 12)}…；不构成商业结论`;
  } catch (error) {
    feedback.textContent = error instanceof Error ? error.message : "离线竞赛未完成";
  } finally {
    button.disabled = false;
  }
}

function metaControllerDemoPayload() {
  const objectiveHash = "78279133458d95a3c05a5dd54c2aa0cb6bccca96a4e1b0c5ea18aae06df5b01b";
  return {
    confirm: "RUN_META_CONTROLLER",
    now: "2026-08-09T00:00:00Z",
    task_belief: {
      case_id: "v061-demo",
      objective_hash: objectiveHash,
      task_family_probabilities: { code: 1 },
      required_modalities: [],
      required_tools: [],
      risk_level: "low",
      reversible: true,
      external_side_effects: false,
      decomposability: { mean: 0.1, lower: 0, upper: 0.2 },
      parallelizability: { mean: 0.1, lower: 0, upper: 0.2 },
      verification_difficulty: { mean: 0.1, lower: 0, upper: 0.2 },
      epistemic_uncertainty: 0.05,
      aleatoric_uncertainty: 0.05,
      ood_score: 0.02,
      hard_constraints: { side_effects: false },
      evidence_ids: ["demo-task-evidence"],
      observed_at: "2026-08-09T00:00:00Z",
      schema_version: "v0.6.0",
    },
    system_belief: {
      case_id: "v061-demo",
      local_queue_depth: 0,
      local_available_slots: 2,
      local_gpu_memory_available_mb: null,
      budget_remaining_usd: 1,
      deadline_remaining_ms: 10000,
      provider_beliefs: [],
      telemetry_age_ms: 0,
      evidence_ids: ["demo-system-evidence"],
      observed_at: "2026-08-09T00:00:00Z",
      schema_version: "v0.6.0",
    },
    candidates: [{
      case_id: "v061-demo",
      management_mode: "no_delegation",
      nodes: [{
        node_id: "demo-node",
        objective_hash: objectiveHash,
        dependencies: [],
        executor_id: "local-demo",
        reasoning_tier: "low",
        tool_ids: [],
        parallel_group: "",
        validator_id: "demo-validator",
        predicted_success: { mean: 0.95, lower: 0.9, upper: 0.98 },
        predicted_cost_usd: { mean: 0.001, p95: 0.002 },
        predicted_latency_ms: { mean: 20, p95: 40 },
      }],
      planning_cost_usd: { mean: 0, p95: 0 },
      planning_latency_ms: { mean: 0, p95: 0 },
      predicted_success: { mean: 0.95, lower: 0.9, upper: 0.98 },
      predicted_total_cost_usd: { mean: 0.001, p95: 0.002 },
      predicted_total_latency_ms: { mean: 20, p95: 40 },
      hard_constraint_checks: { demo: true },
      recovery_policy: "none",
      evidence_ids: ["demo-strategy-evidence"],
      policy_revision: "v061-demo-policy",
      schema_version: "v0.6.0",
    }],
    planner_members: [],
    available_budget: {
      money_usd: 1,
      input_tokens: 0,
      output_tokens: 0,
      wall_clock_ms: 10000,
      planner_calls: 4,
      judge_calls: 0,
      probe_calls: 1,
    },
  };
}

async function loadMetaControllerStatus() {
  const statusNode = $("#meta-controller-status");
  if (!statusNode) return;
  try {
    const response = await fetch("/api/v1/meta-controller/status");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法读取元控制状态");
    statusNode.textContent = `${data.runtime_authority || "advisory-only"} · ${data.policy_version || "v0.6.1"}`;
    $("#meta-controller-calls").textContent = String(data.provider_calls ?? 0);
  } catch (error) {
    statusNode.textContent = "元控制状态暂不可用";
    $("#meta-controller-feedback").textContent = error instanceof Error ? error.message : "无法读取状态";
  }
}

async function runMetaControllerDemo() {
  const button = $("#run-meta-controller-demo");
  const feedback = $("#meta-controller-feedback");
  if (!button || !feedback) return;
  button.disabled = true;
  feedback.textContent = "正在执行离线元控制演示…";
  const suffix = (window.crypto && typeof window.crypto.randomUUID === "function")
    ? window.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  try {
    const response = await fetch("/api/v1/meta-controller/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": `ui-meta-${suffix}` },
      body: JSON.stringify(metaControllerDemoPayload()),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "元控制演示未完成");
    const decision = data.decision || {};
    $("#meta-controller-action").textContent = String(decision.action || "—").toUpperCase();
    $("#meta-controller-evc").textContent = Number(decision.expected_value_of_computation || 0).toFixed(4);
    $("#meta-controller-committee").textContent = String((decision.committee_member_ids || []).length);
    $("#meta-controller-calls").textContent = String(data.provider_calls ?? 0);
    $("#meta-controller-status").textContent = data.replayed ? "已复用确定性决策" : "决策完成 · 离线可审计";
    feedback.textContent = `decision ${String(decision.decision_id || "").slice(0, 12)}… · ${String((decision.reason_codes || []).join(", "))}`;
  } catch (error) {
    feedback.textContent = error instanceof Error ? error.message : "元控制演示未完成";
  } finally {
    button.disabled = false;
  }
}

function capabilityRouterDemoPayload() {
  const now = "2026-08-09T00:00:00Z";
  const candidate = (route_id, model_revision, reasoning_tier, provider_deployment, cost, latency, group) => ({
    route_id, model_revision, reasoning_tier, provider_deployment,
    validator_id: "validator:none", tool_ids: [], modality_ids: [],
    failure_correlation_group: group,
    base_cost_usd: { mean: cost, p95: cost * 2 },
    base_latency_ms: { mean: latency, p95: latency * 2 },
    schema_version: "v0.6.2",
  });
  return {
    confirm: "RUN_CAPABILITY_ROUTER",
    context: {
      project_id: "local-default", task_family: "code", risk_level: "low", ood_score: 0.02,
      required_tools: [], required_modalities: [], deadline_remaining_ms: 1000,
      budget_remaining_usd: 0.05, independent_validator_required: false,
    },
    candidates: [
      candidate("local-medium", "local-model@2026-08", "medium", "local:default", 0.001, 120, "local-model-family"),
      candidate("strong-fallback", "strong-model@2026-08", "high", "offline:strong", 0.02, 350, "strong-model-family"),
    ],
    calibration_set_id: "ui-fixture-v062",
    now,
  };
}

async function loadCapabilityRouterStatus() {
  const statusNode = $("#capability-router-status");
  if (!statusNode) return;
  try {
    const response = await fetch("/api/v1/capability-router/status");
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法读取能力图状态");
    statusNode.textContent = `${data.runtime_authority || "advisory-only"} · ${data.policy_version || "v0.6.2"}`;
    $("#capability-router-samples").textContent = String(data.evidence_sample_count ?? 0);
    $("#capability-router-posteriors").textContent = String(data.counts?.capability_calibrations_v062 ?? 0);
    $("#capability-router-calls").textContent = String(data.provider_calls ?? 0);
  } catch (error) {
    statusNode.textContent = "能力图状态暂不可用";
    $("#capability-router-feedback").textContent = error instanceof Error ? error.message : "无法读取状态";
  }
}

async function runCapabilityRouterDemo() {
  const button = $("#run-capability-router-demo");
  const feedback = $("#capability-router-feedback");
  if (!button || !feedback) return;
  button.disabled = true;
  feedback.textContent = "正在执行离线能力图演示…";
  const suffix = (window.crypto && typeof window.crypto.randomUUID === "function") ? window.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  try {
    const response = await fetch("/api/v1/capability-router/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": `ui-capability-${suffix}` },
      body: JSON.stringify(capabilityRouterDemoPayload()),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "能力图演示未完成");
    const decision = data.decision || {};
    $("#capability-router-action").textContent = String(decision.action || "—");
    $("#capability-router-calls").textContent = String(data.provider_calls ?? 0);
    $("#capability-router-status").textContent = data.replayed ? "已复用确定性建议" : "建议完成 · 离线可审计";
    feedback.textContent = `${String(decision.reason_codes?.join(", ") || "evidence-aware route")} · ${String(decision.decision_id || "").slice(0, 12)}…`;
    loadCapabilityRouterStatus();
  } catch (error) {
    feedback.textContent = error instanceof Error ? error.message : "能力图演示未完成";
  } finally {
    button.disabled = false;
  }
}

function setEvidenceFilter(kind) {
  state.evidenceKind = ["all", "actual", "predicted", "simulated", "missing"].includes(kind) ? kind : "all";
  $$(".evidence-filter").forEach((button) => {
    const selected = button.dataset.evidenceKind === state.evidenceKind;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  renderEvidenceConsole();
}

async function exportEvidencePackage() {
  const button = $("#export-evidence");
  const live = $("#evidence-live");
  if (!button || !live) return;
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = "正在冻结证据…";
  try {
    const response = await fetch("/api/v1/evidence/exports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: state.runId || null }),
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "无法生成实验包");
    const blob = new Blob([JSON.stringify(data.package, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = String(data.filename || "costweave-evidence.json").replace(/[^\w.\-]+/g, "_");
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    live.textContent = `实验包已导出；SHA-256 ${String(data.receipt?.package_hash || "").slice(0, 16)}…，收据已写入不可变记录。`;
  } catch (error) {
    live.textContent = error instanceof Error ? error.message : "实验包导出失败";
  } finally {
    button.disabled = false;
    button.textContent = previous;
  }
}

$("#catalog-search").addEventListener("input", renderCatalog);
$("#catalog-provider-filter").addEventListener("change", renderCatalog);
$("#catalog-status-filter").addEventListener("change", renderCatalog);
$("#add-model").addEventListener("click", () => openModelEditor());
$("#import-models").addEventListener("click", () => {
  $("#import-errors").textContent = "";
  $("#import-dialog").showModal();
});
$("#export-models").addEventListener("click", () => { location.href = "/api/v1/catalog/export?format=json"; });
$("#export-models-csv").addEventListener("click", () => { location.href = "/api/v1/catalog/export?format=csv"; });
$("#reset-catalog").addEventListener("click", resetCatalog);
$("#model-form").addEventListener("submit", saveModel);
$("#delete-model").addEventListener("click", deleteEditingModel);
$("#close-model-editor").addEventListener("click", () => $("#model-editor").close());
$("#cancel-model").addEventListener("click", () => $("#model-editor").close());
$("#import-form").addEventListener("submit", importCatalog);
$("#cancel-import").addEventListener("click", () => $("#import-dialog").close());
$$(".evidence-filter").forEach((button) => button.addEventListener("click", () => setEvidenceFilter(button.dataset.evidenceKind || "all")));
$("#refresh-evidence").addEventListener("click", () => loadEvidenceConsole(state.runId));
$("#export-evidence").addEventListener("click", exportEvidencePackage);
$("#run-offline-tournament").addEventListener("click", runOfflineTournament);
$("#run-meta-controller-demo").addEventListener("click", runMetaControllerDemo);
$("#run-capability-router-demo").addEventListener("click", runCapabilityRouterDemo);

updateGoalCount();
loadCatalog();
loadPolicyRegistry();
loadCanaryPolicy();
loadSecurityBoundary();
loadResilienceStatus();
loadStableReleaseStatus();
loadEvidenceConsole();
loadDecisionIntelligenceStatus();
loadMetaControllerStatus();
loadCapabilityRouterStatus();
refreshHistory({ restore: true });
