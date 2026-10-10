"use strict";
const $=id=>document.getElementById(id);
const session=new URLSearchParams(location.hash.slice(1)).get("session")||sessionStorage.getItem("costweave-lab-session");
if(session)sessionStorage.setItem("costweave-lab-session",session);
history.replaceState(null,"",location.pathname);
let state={rounds:[],connections:[]},pending=false,stopped=false,lastRender="";
const units={CNY:"元",USD:"美元",POINTS:"点"};
const states={queued:"排队中",running:"运行中",completed:"已完成",failed:"已停止（失败）",cancelled:"已取消",interrupted:"中断待核对"};
function notice(message){$("notice").textContent=message;}
async function api(path,body){const response=await fetch("/api/v1/lab"+path,{method:body?"POST":"GET",headers:{"X-CostWeave-Session":session||"","Content-Type":"application/json"},...(body?{body:JSON.stringify(body)}:{})});const data=await response.json();if(!response.ok)throw new Error(data.message||"本机服务响应异常");return data;}
function node(tag,text,className){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;}
function button(text,action){const b=node("button",text,"secondary");b.type="button";b.onclick=()=>perform(action);return b;}
async function perform(action){if(pending)return;pending=true;try{await action();await refresh();}catch(error){notice(error.message);}finally{pending=false;}}
function download(round){const blob=new Blob([JSON.stringify(round,null,2)],{type:"application/json"});const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download=round.id+".json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function modelName(c){return c.provider_default?"接口默认模型（身份未知）":c.model;}
function render(){
  $("connections").replaceChildren();
  for(const c of state.connections){const row=node("div",undefined,"connection"),text=node("span",`${c.label} / ${modelName(c)}`),actions=node("div",undefined,"actions");actions.append(button("修改接口",async()=>{$("connection-id").value=c.id;for(const k of ["label","model","endpoint","profile"])$(k).value=c[k];$("provider-default").checked=!!c.provider_default;$("model").disabled=!!c.provider_default;if(c.provider_default)$("model").value="";$("api-key").value="";$("connect").scrollIntoView({behavior:"smooth"});}),button("再运行一轮（8 次）",async()=>{await api("/automation/run",{connection:c.id,mode:"real",authorize:true});notice("已安排新一轮；不会自动重试。");}));row.append(text,actions);$("connections").append(row);}
  $("overview").replaceChildren();
  for(const r of state.rounds){const sum=r.summary||{},tr=node("tr"),cost=r.cost.actual_amount!==null?`${r.cost.actual_amount} ${units[r.cost.currency]}`:r.mode==="offline"?"离线演示":"待反馈账单";for(const text of [r.connection.label+" / "+modelName(r.connection)+(r.mode==="offline"?"（替身）":""),states[r.status]+` · ${r.calls.length}/8`,`${sum.passed??r.calls.filter(c=>c.validation?.accepted).length}/8`,`${sum.input_tokens??"—"} / ${sum.output_tokens??"—"}`,sum.median_latency_ms==null?"—":(sum.median_latency_ms/1000).toFixed(2)+" 秒",cost])tr.append(node("td",text));$("overview").append(tr);}
  if(!state.rounds.length){const tr=node("tr");const td=node("td","保存接口后，实验将自动出现在这里。");td.colSpan=6;tr.append(td);$("overview").append(tr);}
  // Preserve invoice form input and expanded details while polling other jobs.
  const signature=JSON.stringify(state.rounds.map(r=>[r.id,r.status,r.calls.length,r.calls.map(c=>c.status),r.cost.invoice?.id]));
  if(signature!==lastRender&&!document.activeElement?.closest(".bill")){
    lastRender=signature;$("rounds").replaceChildren();
    for(const r of state.rounds){const article=node("article");article.append(node("h3",r.connection.label+" · "+states[r.status]),node("small",r.id+" · "+r.created_at),node("p",r.cost.actual_amount!==null?"已登记你反馈的本轮账单。":r.message));
      if(r.estimated_cost)article.append(node("p",`按历史账单估算：${Number(r.estimated_cost.amount).toPrecision(5)} ${units[r.estimated_cost.currency]}；待本轮账单核对。`));
      if(r.calibration)article.append(node("p",`已有 ${r.calibration.sample_rounds} 轮账单校准：约 ${Number(r.calibration.rate).toPrecision(5)} ${units[r.calibration.currency]} / 百万总 Token。仅为相似任务的综合经验单价。`));
      if(r.cost.actual_amount!==null)article.append(node("p",`本轮实际成本（你反馈的提供商账单）：${r.cost.actual_amount} ${units[r.cost.currency]}。`));
      if(r.cost.residual!==null&&r.cost.residual!==undefined)article.append(node("p",`实际账单 − 原估算：${r.cost.residual} ${units[r.cost.currency]}。`));
      const actions=node("div",undefined,"actions");actions.append(button("下载完整记录",async()=>download(r)));if(["queued","running"].includes(r.status))actions.append(button("停止后续任务",async()=>{await api("/automation/cancel",{id:r.id});notice("已请求停止；正在发送的调用不会自动重发，仍可能计费。");}));article.append(actions);
      const details=node("details"),summary=node("summary","逐项结果与原始输出");details.append(summary);
      for(const c of r.calls){details.append(node("p",`${c.category} / ${c.task} · ${c.validation?.reason||states[c.status]||c.status}`));details.append(node("p","响应声明型号："+((c.telemetry?.reported_models||[]).join("、")||"接口未报告")+"（未经身份验证）"));if(c.output!==undefined)details.append(node("pre",c.output));}article.append(details);
      if(r.mode==="real"&&!["queued","running"].includes(r.status))article.append(invoiceForm(r));$("rounds").append(article);
    }
  }
  $("directory").textContent="自动保存目录："+state.report_directory;
  $("stop").disabled=state.busy;
}
function invoiceForm(r){const form=node("form",undefined,"bill"),title=node("h3",r.cost.invoice?"修正本轮账单（保留原版本）":"反馈本轮真实账单"),grid=node("div",undefined,"grid");const amount=node("input");amount.type="number";amount.min="0";amount.step="any";amount.required=true;amount.value=r.cost.invoice?.amount||"";const amountLabel=node("label","账单金额");amountLabel.append(amount);const currency=node("select");for(const code of ["CNY","USD","POINTS"]){const op=node("option",units[code]);op.value=code;currency.append(op);}currency.value=r.cost.invoice?.currency||"CNY";const currencyLabel=node("label","账单单位");currencyLabel.append(currency);grid.append(amountLabel,currencyLabel);const source=node("input");source.required=true;source.maxLength=500;source.placeholder="例如：服务商账单编号 / 对应时间段";source.value=r.cost.invoice?.source_reference||"";const sourceLabel=node("label","来源凭据");sourceLabel.append(source);const scope=node("input");scope.type="checkbox";scope.required=true;const scopeLabel=node("label",undefined,"check");scopeLabel.append(scope,node("span","确认这个金额只包含本轮该接口的调用，未混入其他应用用量、充值或退款。"));const save=node("button",r.cost.invoice?"保存修正并重新校准":"登记账单并自动校准");save.type="submit";form.append(title,node("p",`核对窗口：${r.started_at||r.created_at} 至 ${r.finished_at||"—"}；来源 ${r.connection.label}，模型 ${modelName(r.connection)}。`),grid,sourceLabel,scopeLabel,save);form.onsubmit=event=>{event.preventDefault();perform(async()=>{await api("/automation/invoice",{round_id:r.id,amount:amount.value,currency:currency.value,source_reference:source.value,scope_confirmed:scope.checked,supersedes:r.cost.invoice?.id});save.blur();lastRender="";notice("账单已登记，历史预测保留；后续相同接口实验将使用更新后的成本校准。");});};return form;}
async function refresh(){state=await api("/automation");render();}
$("connect").onsubmit=event=>{event.preventDefault();perform(async()=>{if(!$("provider-default").checked&&!$("model").value.trim()){notice("请输入调用别名，或勾选“使用接口默认模型”。自动读取不是必需步骤。");return;}const result=await api("/automation/connect",{id:$("connection-id").value,label:$("label").value.trim()||($("provider-default").checked?"默认模型接口":$("model").value.trim()),provider_default:$("provider-default").checked,endpoint:$("endpoint").value,model:$("model").value,profile:$("profile").value,api_key:$("api-key").value,auto_run:true});$("api-key").value="";$("connection-id").value=result.connection_id;notice(result.deduplicated?"接口已保存；相同配置已有实验，未重复扣费。需要更多样本时点击“再运行一轮”。":"接口已加密保存，已自动安排一轮实验。你可以继续添加其他接口。");});};
$("new").onclick=()=>{$("connect").reset();$("model").disabled=false;$("connection-id").value="";};
$("profile").onchange=()=>{if($("profile").value==="deepseek"&&!$("connection-id").value){$("endpoint").value="https://api.deepseek.com";$("model").value="deepseek-flash";$("label").value="DeepSeek 官方";}};
$("offline").onclick=()=>perform(async()=>{await api("/automation/run",{mode:"offline"});notice("离线全流程已启动，零外部模型请求。");});
$("stop").onclick=async()=>{try{await api("/stop",{});stopped=true;notice("本机服务已停止。双击启动入口可再次打开。");for(const b of document.querySelectorAll("button"))b.disabled=true;}catch(error){notice(error.message);}};
refresh().then(()=>notice("自动实验环境已就绪。填写接口后将自动运行；真实成本等待你反馈账单。")).catch(error=>notice(error.message+"；请从启动入口重新打开。"));
setInterval(()=>{if(!pending&&!stopped&&state.busy)refresh().catch(error=>notice(error.message));},1500);

async function discoverModels(){
  $("model-discovery").textContent="正在读取服务商模型列表…";
  const result=await api("/automation/models",{id:$("connection-id").value,endpoint:$("endpoint").value,profile:$("profile").value,api_key:$("api-key").value});
  $("model-options").replaceChildren();
  for(const id of result.models){const option=node("option");option.value=id;$("model-options").append(option);}
  if(result.models.length===1&&!$("provider-default").checked&&!$("model").value.trim())$("model").value=result.models[0];
  $("model-discovery").textContent=result.message+` 共 ${result.models.length} 个。`+(result.models.length>1?"点击型号输入框选择或输入；不会替你任意选择付费模型。":"");
}
$("discover").onclick=()=>perform(async()=>{try{await discoverModels();}catch(error){$("model-discovery").textContent=error.message;throw error;}});
for(const id of ["endpoint","api-key"]){$(id).addEventListener("input",()=>{$("model-options").replaceChildren();$("model-discovery").textContent="接口已修改；可选读取型号，也可手动填写或使用默认模型。";});}

$("provider-default").onchange=()=>{$("model").disabled=$("provider-default").checked;};
