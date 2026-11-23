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
const statusNames = {complete: "已完成", blocked: "已停止", active: "运行中", needs_reconciliation: "等待核对"};
const eventNames = {run_created: "创建运行", input_recorded: "记录输入", attempt_claimed: "预留预算",
  result_recorded: "已收到结果", attempt_uncertain: "等待核对", run_finished: "运行结束"};
const errorNames = {session_auth_required: "本地会话已失效，请通过启动时提供的链接重新打开调度台。",
  run_id_conflict: "该运行编号已绑定另一份任务。请查询原记录，或为新任务更换编号。",
  run_or_attempt_not_found: "当前项目中没有这个运行编号。",
  persistent_runtime_disabled: "当前服务未开启持久化运行。"};
const reasonNames = {complete: "目标产物均已通过验收。", over_budget: "剩余预算不足。",
  unreachable: "当前没有可用路线生成所有目标。", search_limit: "已达到规划搜索上限。",
  execution_outcome_unknown: "执行结果待核对。", validator_error: "验收器出错，已保留待验收的结果。"};
let busy = false;

project.value = storage.getItem("costweave.artifact.project") || "local-default";
runId.value = storage.getItem("costweave.artifact.run") || "run-" + crypto.randomUUID();
storage.setItem("costweave.artifact.run", runId.value);
editor.value = JSON.stringify(example, null, 2);

function clearResult() {
  byId("status").textContent = "待查询";
  for (const field of ["scope", "budget", "reason"]) byId(field).textContent = "";
  byId("outputs").textContent = "暂无产物";
  byId("events").replaceChildren();
  byId("events-note").textContent = "查询或提交任务后显示。";
  message.textContent = "";
}

function showState(state) {
  byId("status").textContent = statusNames[state.status] || state.status;
  byId("scope").textContent = project.value + " / " + state.run_id;
  byId("budget").textContent = `已结算 ${state.spent_units} · 已预留 ${state.held_units} · 总预算 ${state.budget_units} 单位`;
  byId("reason").textContent = reasonNames[state.reason] || state.reason || "";
  byId("outputs").textContent = Object.keys(state.outputs).length ? JSON.stringify(state.outputs, null, 2) : "暂无产物";
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
  const root = "/api/v2/runs";
  const path = root + "/" + encodeURIComponent(runId.value);
  clearResult();
  byId("status").textContent = "请求中";
  busy = true;
  for (const control of form.elements) control.disabled = true;
  byId("result-panel").setAttribute("aria-busy", "true");
  storage.setItem("costweave.artifact.project", project.value);
  storage.setItem("costweave.artifact.run", runId.value);
  try {
    let state;
    if (action === "submit") state = await request(root, {method: "POST", body, submit: true});
    else if (action === "resume") state = await request(path + "/resume", {method: "POST", body: "{}"});
    else state = await request(path);
    showState(state);
    byId("events").replaceChildren();
    byId("events-note").textContent = "正在读取执行记录…";
    try {
      const data = await request(path + "/events");
      showEvents(data.events);
    } catch (error) {
      byId("events-note").textContent = "执行记录读取失败。" + error.message;
    }
  } catch (error) {
    byId("status").textContent = "请求失败";
    message.textContent = error.message;
  } finally {
    byId("result-panel").setAttribute("aria-busy", "false");
    for (const control of form.elements) control.disabled = false;
    busy = false;
  }
}

form.addEventListener("submit", event => { event.preventDefault(); runAction("submit"); });
byId("inspect").addEventListener("click", () => runAction("inspect"));
byId("resume").addEventListener("click", () => runAction("resume"));
byId("example").addEventListener("click", () => {
  editor.value = JSON.stringify(example, null, 2);
  message.textContent = "";
  editor.focus();
});
project.addEventListener("input", clearResult);
runId.addEventListener("input", clearResult);
