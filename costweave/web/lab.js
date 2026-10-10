"use strict";
const $ = id => document.getElementById(id);
const token = new URLSearchParams(location.hash.slice(1)).get("session") || sessionStorage.getItem("costweave-lab-session");
if (token) sessionStorage.setItem("costweave-lab-session", token);
history.replaceState(null, "", location.pathname);
let state = {tasks:[],connections:[],jobs:[]};
let pending = false;
async function api(path, body) {
  const response = await fetch("/api/v1/lab" + path, {method:body ? "POST":"GET", headers:{"X-CostWeave-Session":token || "", "Content-Type":"application/json"}, ...(body ? {body:JSON.stringify(body)}:{})});
  const value = await response.json();
  if (!response.ok) throw new Error(value.message || "本地服务响应异常");
  return value;
}
function notice(text) { $("notice").textContent = text; }
function option(select, value, text) { const node = document.createElement("option"); node.value=value; node.textContent=text; select.append(node); }
function prompt() {
  const task=state.tasks.find(t=>t.id===$("task").value);
  $("manual-prompt").value=task?.prompt || "Return exactly one JSON object with only answer and checksum. Compute (17 * 23) + 11 for integer answer. For checksum, take characters at 1-based odd positions of 'CostWeave' and lowercase them. Return no Markdown or prose.";
}
function render() {
  const selected=$("selected-connection").value;
  $("selected-connection").replaceChildren(); $("connections").replaceChildren();
  if (!state.connections.length) option($("selected-connection"), "", "尚未配置");
  for (const item of state.connections) {
    option($("selected-connection"),item.id,`${item.label} · ${item.model}`);
    const row=document.createElement("div");row.className="connection";
    const name=document.createElement("span");name.textContent=`${item.label} · ${item.model} ${item.input_price===null||item.output_price===null ? "（待核实计费）":""}`;
    const edit=document.createElement("button");edit.textContent="修改";edit.className="secondary";
    edit.onclick=()=>{ $("connection-id").value=item.id; for(const key of ["label","model","endpoint","profile"]) $(key).value=item[key]; $("api-key").value="";$("input-price").value=item.input_price??"";$("output-price").value=item.output_price??"";$("connection-form").scrollIntoView({behavior:"smooth"});};
    row.append(name,edit);$("connections").append(row);
  }
  if(state.connections.some(c=>c.id===selected)) $("selected-connection").value=selected;
  if (!$("task").options.length) {for(const task of state.tasks) option($("task"),task.id,task.title);prompt();}
  $("report-directory").textContent="自动保存位置："+state.report_directory;
  $("results").replaceChildren();
  for (const job of [...state.jobs].reverse()) {
    const article=document.createElement("article"), title=document.createElement("h3"),desc=document.createElement("p"),meta=document.createElement("small");
    title.textContent=`${{real:"真实试跑",offline:"离线检查",manual:"人工评测"}[job.mode]} · ${state.tasks.find(t=>t.id===job.task)?.title||job.task} · ${{running:"运行中",completed:"已完成",failed:"未完成",interrupted:"已中断"}[job.status]||job.status}`;
    desc.textContent=job.message+(job.validation ? " / 校验："+job.validation.reason:"");meta.textContent=job.created_at+" · "+job.id;
    article.append(title,meta,desc);
    if(job.output!==undefined){const pre=document.createElement("pre");pre.textContent=job.output;article.append(pre);}
    if(job.telemetry){const usage=document.createElement("p");usage.textContent=`输入 ${job.telemetry.input_tokens??"未知"} / 输出 ${job.telemetry.output_tokens??"未知"} Token；按填写单价估算 $${job.telemetry.actual_cost_usd??"未知"}（非账单）`;article.append(usage);}
    const download=document.createElement("button");download.className="secondary";download.textContent="下载记录";download.onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(job,null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download=job.id+".json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};article.append(download);$("results").append(article);
  }
  for(const id of ["offline","real","manual","stop"]) $(id).disabled=state.busy||pending;
}
async function refresh(){state=await api("");render();}
async function action(fn){if(pending)return;pending=true;render();try{await fn();await refresh();}catch(error){notice(error.message);}finally{pending=false;render();}}
$("connection-form").onsubmit=event=>{event.preventDefault();action(async()=>{await api("/connections",{id:$("connection-id").value,label:$("label").value,endpoint:$("endpoint").value,model:$("model").value,profile:$("profile").value,api_key:$("api-key").value,input_price:$("input-price").value,output_price:$("output-price").value});$("api-key").value="";notice("接口已加密保存，未向服务商发送请求。");});};
$("new-connection").onclick=()=>{$("connection-form").reset();$("connection-id").value="";};
$("profile").onchange=()=>{if($("profile").value==="deepseek"&&!$("connection-id").value){$("endpoint").value="https://api.deepseek.com";$("model").value="deepseek-flash";$("label").value="DeepSeek 官方";}};
$("task").onchange=prompt;
$("copy-prompt").onclick=()=>action(async()=>{await navigator.clipboard.writeText($("manual-prompt").value);notice("任务已复制。");});
$("offline").onclick=()=>action(async()=>{await api("/run",{mode:"offline",task:$("task").value});notice("离线检查已启动，不会联网。");});
$("real").onclick=()=>action(async()=>{if(!$("authorize").checked)throw new Error("请先勾选本次调用授权。");await api("/run",{mode:"real",task:$("task").value,connection:$("selected-connection").value,budget_usd:Number($("budget").value),authorize:true});$("authorize").checked=false;notice("单次真实试跑已启动，请等待结果。不会自动重试。");});
$("manual").onclick=()=>action(async()=>{await api("/manual",{task:$("task").value,output:$("manual-output").value,model:$("manual-model").value||"未填写"});notice("人工评测已校验并保存。");});
$("stop").onclick=async()=>{try{await api("/stop",{});state.busy=false;notice("本机实验服务已停止。再次双击启动入口即可打开。");for(const button of document.querySelectorAll("button"))button.disabled=true;}catch(error){notice(error.message);}};
refresh().then(()=>notice("环境已就绪。保存接口不产生费用，可先点击离线检查。")).catch(error=>notice(error.message+"；请从启动入口重新打开页面。"));
setInterval(()=>{if(state.busy&&!pending)refresh().catch(error=>notice(error.message));},1500);
