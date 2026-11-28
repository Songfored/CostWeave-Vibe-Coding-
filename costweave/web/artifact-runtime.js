"use strict";

const byId = id => document.getElementById(id);
const storage = window.sessionStorage;
const session = new URLSearchParams(location.hash.slice(1)).get("session");
if (session) {
  storage.setItem("costweave.localSession", session);
  history.replaceState(null, "", location.pathname + location.search);
}

const example = {
  goals: ["handoff"],
  inputs: {ticket: {preview: {owner: "林"}, approved: {owner: "林", hours: 4}}},
  budget_units: 4,
  operations: [
    {id: "preview", requires: ["ticket"], produces: "handoff", executor: "pick",
      validator: "present", cost_units: 1, config: {source: "ticket", path: ["preview"]}},
    {id: "approved", requires: ["ticket"], produces: "handoff", executor: "pick",
      validator: "present", cost_units: 2, config: {source: "ticket", path: ["approved"]}},
  ],
  acceptance: {handoff: {validator: "json_object", config: {required_keys: ["owner", "hours"]}}},
};
const form = byId("job-form");
const project = byId("project");
const runId = byId("run-id");
const editor = byId("job");
const message = byId("message");
const clarificationForm = byId("clarification-form");
const clarificationValue = byId("clarification-value");
let requestedArtifact = null;
const statusNames = {complete: "已完成", blocked: "已停止", active: "运行中", needs_reconciliation: "等待核对",
  waiting_for_input: "等待补充资料"};
const eventNames = {run_created: "创建运行", input_recorded: "记录输入", attempt_claimed: "预留预算",
  input_requested: "请求补充资料", input_supplied: "资料已验收",
  result_recorded: "已收到结果", attempt_uncertain: "等待核对", run_finished: "运行结束"};
const errorNames = {session_auth_required: "本地会话已失效，请通过启动时提供的链接重新打开调度台。",
  run_id_conflict: "该运行编号已绑定另一份任务。请查询原记录，或为新任务更换编号。",
  clarification_conflict: "该项资料已提交，内容与此次不同。请查询记录。",
  run_or_attempt_not_found: "当前项目中没有这个运行编号。",
  persistent_runtime_disabled: "当前服务未开启持久化运行。"};
const reasonNames = {complete: "目标产物均已通过验收。", over_budget: "剩余预算不足。",
  quality_threshold_not_met: "没有路线达到按时正确交付的最低要求。",
  deadline_expired: "任务时限已到。", deadline_unreachable: "剩余时间不足以完成可用路线。",
  insufficient_evidence: "现有样本不足以支持路线选择。", no_eligible_verification_route: "没有路线同时满足当前约束。",
  verification_route_failed: "本次核验路线未能交付。", unavailable: "缺少所需的路线输入。",
  unreachable: "当前没有可用路线生成所有目标。", search_limit: "已达到规划搜索上限。",
  evidence_action_rejected: "核验报告指定的操作未通过验收。", evidence_prerequisite_missing: "核验报告指定的操作缺少前置资料。",
  execution_outcome_unknown: "执行结果待核对。", validator_error: "验收器出错，已保留待验收的结果。"};
let busy = false;

project.value = storage.getItem("costweave.artifact.project") || "local-default";
runId.value = storage.getItem("costweave.artifact.run") || "run-" + crypto.randomUUID();
storage.setItem("costweave.artifact.run", runId.value);
editor.value = JSON.stringify(example, null, 2);

function clearResult() {
  requestedArtifact = null;
  clarificationForm.hidden = true;
  byId("reconcile-form").hidden = byId("reconcile-title").hidden = true;
  clarificationValue.value = "";
  byId("status").textContent = "待查询";
  for (const field of ["scope", "budget", "risk", "reason"]) byId(field).textContent = "";
  byId("outputs").textContent = "暂无产物";
  byId("audit-view").textContent = "暂无审计记录";
  byId("events").replaceChildren();
  byId("events-note").textContent = "查询或提交任务后显示。";
  message.textContent = "";
}

function showState(state) {
  byId("status").textContent = statusNames[state.status] || state.status;
  byId("scope").textContent = project.value + " / " + state.run_id;
  byId("budget").textContent = `已结算 ${state.spent_units} · 已预留 ${state.held_units} · 总预算 ${state.budget_units} 单位`;
  const riskSpent = Number(state.risk_spent || 0);
  const riskHeld = Number(state.risk_held || 0);
  const deadline = state.deadline_at_ms == null ? "" : " · 到期 " + new Date(state.deadline_at_ms).toLocaleString();
  byId("risk").textContent = `已结算风险 ${riskSpent.toFixed(4)} · 已预留风险 ${riskHeld.toFixed(4)} · 在途尝试 ${(state.active_attempts || []).length}${deadline}`;
  byId("reason").textContent = state.clarification ? "" : reasonNames[state.reason] || state.reason || "";
  byId("outputs").textContent = Object.keys(state.outputs).length ? JSON.stringify(state.outputs, null, 2) : "暂无产物";
  const clarification = state.clarification;
  if (state.status === "waiting_for_input" && clarification) {
    if (requestedArtifact !== clarification.artifact) clarificationValue.value = "";
    requestedArtifact = clarification.artifact;
    byId("clarification-label").textContent = "补充 " + requestedArtifact + "（JSON）";
    byId("clarification-note").textContent = clarification.reason;
    clarificationForm.hidden = false;
  } else {
    clarificationForm.hidden = true;
    requestedArtifact = null;
    clarificationValue.value = "";
  }
  byId("rec-attempt").value = state.active_attempt?.id || "";
  byId("reconcile-form").hidden = byId("reconcile-title").hidden = state.status !== "needs_reconciliation";
}

function showAudit(audit) {
  if (!audit || !Array.isArray(audit.decisions) || !audit.decisions.length) {
    byId("audit-view").textContent = "暂无决策记录。";
    return;
  }
  const lines = audit.decisions.map(d => {
    const plan = d.plan || {};
    const ops = (plan.operations || []).join(" -> ");
    const altCount = plan.audit && Array.isArray(plan.audit.alternatives) ? plan.audit.alternatives.length : 0;
    const parts = [`#${d.seq} 选择 ${d.op_id} (${ops})`];
    if (plan.audit) parts.push(`备选 ${altCount}`);
    if (plan.evidence) {
      const e = plan.evidence;
      parts.push(`规则 ${e.rule_index + 1} · ${e.artifact}`);
      if (e.artifact_sha256) parts.push("证据 SHA-256 " + e.artifact_sha256);
    }
    if (plan.verification) parts.push(plan.verification.selected_reason);
    if (plan.fallback) parts.push(plan.fallback.decision);
    return parts.join(" | ");
  });
  byId("audit-view").textContent = lines.join("\n");
}

function showEvents(events) {
  const rows = events.map(event => {
    const row = document.createElement("li");
    let label = eventNames[event.type] || event.type;
    if (event.type === "attempt_settled") label = event.accepted ? "验收通过" : "验收未通过";
    const detail = event.op_id || event.artifact || event.reason || "";
    row.textContent = detail ? label + " · " + detail : label;
    return row;
  });
  byId("events").replaceChildren(...rows);
  byId("events-note").textContent = rows.length ? "" : "暂无执行记录。";
}

async function request(path, options = {}) {
  const headers = new Headers({"X-CostWeave-Project": project.value});
  const token = storage.getItem("costweave.localSession");
  if (token) headers.set("X-CostWeave-Session", token);
  if (options.method === "POST") headers.set("Content-Type", "application/json");
  if (options.submit) headers.set("Idempotency-Key", runId.value);
  const response = await fetch(path, {
    method: options.method || "GET", body: options.body, headers, credentials: "same-origin", redirect: "error",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(errorNames[data.error] || data.message || data.error || `HTTP ${response.status}`);
  return data;
}

async function runAction(action) {
  if (busy) return;
  message.textContent = "";
  project.value = project.value.trim().toLowerCase();
  runId.value = runId.value.trim();
  if (!/^[a-z0-9][a-z0-9._-]{2,63}$/.test(project.value)) {
    message.textContent = "项目名须为 3 至 64 位，以字母或数字开头，可包含点号、下划线和连字符。";
    project.focus();
    return;
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$/.test(runId.value)) {
    message.textContent = "运行编号须为 1 至 96 位，以字母或数字开头，可包含点号、下划线和连字符。";
    runId.focus();
    return;
  }
  let body;
  if (action === "submit") {
    try {
      body = JSON.stringify(JSON.parse(editor.value));
    } catch (error) {
      message.textContent = "任务 JSON 无法解析。" + error.message;
      editor.focus();
      return;
    }
  }
  if (action === "provide") {
    if (!requestedArtifact) return;
    try {
      body = JSON.stringify({artifact: requestedArtifact, value: JSON.parse(clarificationValue.value)});
    } catch (error) {
      message.textContent = "补充资料的 JSON 无法解析。" + error.message;
      clarificationValue.focus();
      return;
    }
  }
  const root = "/api/v2/runs";
  const path = root + "/" + encodeURIComponent(runId.value);
  if (action !== "provide" && action !== "reconcile") clearResult();
  byId("status").textContent = "请求中";
  busy = true;
  for (const control of [...form.elements, ...clarificationForm.elements, ...byId("reconcile-form").elements]) control.disabled = true;
  byId("result-panel").setAttribute("aria-busy", "true");
  storage.setItem("costweave.artifact.project", project.value);
  storage.setItem("costweave.artifact.run", runId.value);
  try {
    let state;
    if (action === "submit") state = await request(root, {method: "POST", body, submit: true});
    else if (action === "provide") state = await request(path + "/provide", {method: "POST", body});
    else if (action === "resume") state = await request(path + "/resume", {method: "POST", body: "{}"});
    else if (action === "reconcile") state = await request(path + "/reconcile", {method: "POST", body: optionsReconcileBody()});
    else state = await request(path);
    showState(state);
    try {
      showAudit(await request(path + "/audit"));
    } catch (_) {
      byId("audit-view").textContent = "审计记录读取失败。";
    }
    byId("events").replaceChildren();
    byId("events-note").textContent = "正在读取执行记录…";
    try {
      const data = await request(path + "/events");
      showEvents(data.events);
    } catch (error) {
      byId("events-note").textContent = "执行记录读取失败。" + error.message;
    }
  } catch (error) {
    byId("status").textContent = action === "provide" ? "请查询或重新提交资料" : "请求失败";
    message.textContent = error.message;
  } finally {
    byId("result-panel").setAttribute("aria-busy", "false");
    for (const control of [...form.elements, ...clarificationForm.elements, ...byId("reconcile-form").elements]) control.disabled = false;
    busy = false;
  }
}

function optionsReconcileBody() {
  const attemptId = byId("rec-attempt").value.trim();
  const outcome = byId("rec-outcome").value;
  const unitsRaw = byId("rec-units").value.trim();
  const receipt = {outcome};
  if (outcome === "returned") {
    receipt.value = JSON.parse(byId("rec-value").value || "null");
  }
  if (unitsRaw !== "") {
    receipt.actual_units = Number(unitsRaw);
  }
  return JSON.stringify({attempt_id: attemptId, receipt});
}

form.addEventListener("submit", event => { event.preventDefault(); runAction("submit"); });
byId("reconcile-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!byId("rec-attempt").value.trim()) {
    message.textContent = "请填写需要核对的 attempt_id。";
    return;
  }
  try {
    optionsReconcileBody();
  } catch (error) {
    message.textContent = "核对 JSON 无法解析。" + error.message;
    return;
  }
  runAction("reconcile");
});
clarificationForm.addEventListener("submit", event => { event.preventDefault(); runAction("provide"); });
byId("inspect").addEventListener("click", () => runAction("inspect"));
byId("resume").addEventListener("click", () => runAction("resume"));
byId("example").addEventListener("click", () => {
  editor.value = JSON.stringify(example, null, 2);
  message.textContent = "";
  editor.focus();
});
project.addEventListener("input", clearResult);
runId.addEventListener("input", clearResult);
