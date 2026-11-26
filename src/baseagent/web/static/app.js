// BaseAgent Web: one EventSource for chat events, JSON POSTs for commands. No framework, no build step.
"use strict";

const token = new URLSearchParams(location.search).get("token");
const query = token ? "?token=" + encodeURIComponent(token) : "";
const $ = (id) => document.getElementById(id);
const transcript = $("transcript");
const approvals = new Map();   // key "<worker|main>:<call_id>" -> approval view
let workers = [];
let progress = {};             // worker id -> last trace line

function line(kind, text) {
  const div = document.createElement("div");
  div.className = "msg " + kind;
  div.textContent = text;     // never innerHTML: every string here comes from the model or the workspace
  const atBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 40;
  transcript.appendChild(div);
  if (atBottom) transcript.scrollTop = transcript.scrollHeight;
  return div;
}

async function post(path, body) {
  const headers = {"Content-Type": "application/json"};
  if (token) headers["Authorization"] = "Bearer " + token;
  const response = await fetch(path, {method: "POST", headers, body: JSON.stringify(body)});
  let value = {};
  try { value = await response.json(); } catch (error) { /* empty body */ }
  return {ok: response.ok, value};
}

function seconds(worker) {
  const end = worker.finished_at || Date.now() / 1000;
  return Math.max(0, end - worker.started_at).toFixed(0) + "s";
}

function renderWorkers() {
  const body = $("workers");
  body.replaceChildren();
  if (!workers.length) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 5; cell.className = "muted"; cell.textContent = "还没有 worker";
    return;
  }
  for (const worker of workers) {
    const row = body.insertRow();
    const id = row.insertCell();
    id.textContent = worker.id + (worker.allow_writes ? " ✎" : "");
    id.title = worker.task;
    const state = row.insertCell();
    state.textContent = worker.label;
    if (progress[worker.id] && (worker.status === "running")) {
      const last = document.createElement("span");
      last.className = "progress"; last.textContent = progress[worker.id];
      state.appendChild(last);
    }
    row.insertCell().textContent = seconds(worker);
    row.insertCell().textContent = `${worker.model_calls}/${worker.max_model_calls ?? "?"}`;
    const actions = row.insertCell();
    if (worker.status === "running" || worker.status === "awaiting_approval") {
      const cancel = document.createElement("button");
      cancel.className = "outline secondary small"; cancel.type = "button"; cancel.textContent = "取消";
      cancel.onclick = () => post(`/workers/${encodeURIComponent(worker.id)}/cancel${query}`, {});
      const tell = document.createElement("button");
      tell.className = "outline small"; tell.type = "button"; tell.textContent = "补充";
      tell.onclick = () => {
        const text = prompt(`给 ${worker.id} 补充要求：`);
        if (text && text.trim()) post(`/workers/${encodeURIComponent(worker.id)}/tell${query}`, {text});
      };
      actions.append(cancel, " ", tell);
    }
  }
}

function renderStatus(status) {
  $("session").textContent = "会话：" + (status.session_id || "（第一条消息创建）") + (status.busy ? " · 主 Agent 回复中…" : "");
  const b = status.budget;
  let budget = "预算：—";
  if (b) {
    budget = `本轮预算：模型 ${b.model_calls}/${b.max_model_calls} · 步数 ${b.steps}/${b.max_steps}`;
    if (b.budget_cny) budget += ` · ¥${b.cost_cny}/${b.budget_cny}`;
  }
  $("budget").textContent = budget + ` · tokens ${status.tokens}`;
  workers = status.workers || [];
  renderWorkers();
}

function approvalKey(view) { return (view.worker || "main") + ":" + view.call_id; }

function renderApprovals() {
  const box = $("approvals");
  box.replaceChildren();
  for (const view of approvals.values()) {
    const card = document.createElement("article");
    card.className = "approval";
    const title = document.createElement("strong");
    title.textContent = `需要审批：${view.name}`;
    const who = document.createElement("div");
    who.className = "muted";
    who.textContent = view.worker ? `worker ${view.worker}（会话 ${view.session_id}）` : "主 Agent";
    const details = document.createElement("pre");
    details.textContent = [view.task ? "任务：" + view.task : "", view.arguments ? "参数：" + view.arguments : "",
                           "call：" + view.call_id, "digest：" + view.digest].filter(Boolean).join("\n");
    const buttons = document.createElement("div");
    buttons.className = "buttons";
    for (const [label, decision, style] of [["批准", "allow", ""], ["拒绝", "deny", "secondary"]]) {
      const button = document.createElement("button");
      button.type = "button"; button.textContent = label; button.className = style;
      button.onclick = async () => {
        const result = await post("/approvals" + query, {call_id: view.call_id, digest: view.digest, decision,
                                                         worker: view.worker});
        if (!result.ok) line("warn", "审批没有记录：" + (result.value.message || result.value.detail || "失败"));
      };
      buttons.appendChild(button);
    }
    card.append(title, who, details, buttons);
    box.appendChild(card);
  }
}

function resync(snapshot) {
  transcript.replaceChildren();
  for (const item of snapshot.history) line(item.role, item.text);
  approvals.clear();
  for (const view of snapshot.approvals) approvals.set(approvalKey(view), view);
  renderApprovals();
  renderStatus(snapshot.status);
}

function handle(event) {
  const d = event.data;
  switch (event.kind) {
    case "resync": resync(d.snapshot); break;
    case "user_message":
      line("user", d.text + (d.queued ? (d.mode === "queue" ? "  （已排队：本轮结束后发送）" : "  （已加入本轮，等待送达）") : ""));
      break;
    case "inbox_delivered":
      if ((d.sources || []).includes("user")) line("system", d.how === "model_call" ? "（已送达主 Agent：这一步就会看到）" : "（已送达主 Agent：作为新的一轮）");
      break;
    case "assistant_message": line("assistant", d.text); break;
    case "tool_call": line("tool", `${d.worker ? "[" + d.worker + "] " : ""}→ ${d.name} ${d.arguments}`); break;
    case "tool_result": line("tool", `${d.worker ? "[" + d.worker + "] " : ""}← ${d.name} ${d.ok ? "ok" : "失败"}: ${d.preview}`); break;
    case "worker_started": line("system", `已派出 ${d.worker}${d.allow_writes ? "（可写）" : "（只读）"}：${d.task}`); break;
    case "worker_progress": progress[d.worker] = d.line; renderWorkers(); break;
    case "worker_done": line("assistant push", d.text); break;
    case "worker_failed": case "worker_cancelled": line("warn push", d.text); break;
    case "approval_requested": approvals.set(approvalKey(d), d); renderApprovals(); break;
    case "approval_decided": approvals.delete(approvalKey(d)); renderApprovals(); line("system", d.text); break;
    case "status": renderStatus(d); break;
    case "notice": line(d.stream === "err" ? "warn" : "system", d.text); break;
    case "error": line("warn", d.message); break;
  }
}

function connect() {
  const source = new EventSource("/events" + query);   // reconnects by itself and sends Last-Event-ID
  source.onopen = () => { $("connection").textContent = "已连接"; };
  source.onerror = () => { $("connection").textContent = "重连中…"; };
  source.onmessage = (message) => handle(JSON.parse(message.data));
}

// ----- side chat: its own stream, input and buttons (docs/side.md) -----------------------------------
const sideLog = $("side-log");
const sideLive = new Map();   // attempt -> element showing the answer as it arrives
const sideShown = new Map();  // attempt -> characters shown (Python counts code points, so count them the same way)

function opId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
}

function sideLine(kind, text) {
  const div = document.createElement("div");
  div.className = "msg " + kind;
  div.textContent = text;
  sideLog.appendChild(div);
  sideLog.scrollTop = sideLog.scrollHeight;
  return div;
}

function sideState(text) { $("side-state").textContent = text; }

function liveLine(attempt) {
  if (!sideLive.has(attempt)) sideLive.set(attempt, sideLine("assistant", ""));
  return sideLive.get(attempt);
}

function finishLive(attempt, text, suffix) {
  const element = liveLine(attempt);
  element.textContent = (text || "") + (suffix || "");
  sideLive.delete(attempt);
}

function sideResync(snapshot) {
  sideLog.replaceChildren(); sideLive.clear();
  const view = snapshot.side;
  if (!view) { sideState("未打开"); return; }
  for (const note of view.notes) sideLine("system", note);
  for (const item of view.attempts) {
    sideLine("user", item.question);
    const element = sideLine("assistant", item.text);
    sideShown.set(item.attempt, Array.from(item.text).length);
    if (item.status === "dispatching" || item.status === "streaming") sideLive.set(item.attempt, element);
    else if (item.status === "stopped") element.textContent += "（已停止）";
    else if (item.status === "failed") element.textContent += "（失败）";
  }
  sideState(view.ended ? "已结束" : (view.active ? "回答中…" : "打开中"));
}

function handleSide(event) {
  const d = event.data;
  switch (event.kind) {
    case "resync": sideResync(d.snapshot); break;
    case "side_opened":
      sideLine("system", `已打开：主会话第 ${d.turn} 轮的快照（${d.origin === "live" ? "主任务最近一次真实请求" : "已保存的会话，文本化"}）`);
      for (const note of d.notes || []) sideLine("system", note);
      sideState("打开中"); break;
    case "side_question": sideLine("user", d.text); liveLine(d.attempt); sideState("回答中…"); break;
    case "side_delta": {
      const element = liveLine(d.attempt);
      if ((sideShown.get(d.attempt) || 0) === d.offset) {    // a replayed piece is never added twice
        element.textContent += d.text;
        sideShown.set(d.attempt, d.offset + Array.from(d.text).length);
      }
      break;
    }
    case "side_answer": finishLive(d.attempt, d.text); sideState("打开中"); break;
    case "side_stopped": finishLive(d.attempt, d.text, "（已停止）"); sideState("打开中"); break;
    case "side_failed": finishLive(d.attempt, d.text, `（失败：${d.error}）`); sideState("打开中"); break;
    case "side_usage":
      if (d.usage === "unknown") sideLine("system", "（这次请求的用量未知，不按 0 计）");
      else if (d.late) sideLine("system", `（已停止那一问的用量后到：${d.prompt_tokens}+${d.completion_tokens} tokens，已记入）`);
      break;
    case "side_notice": sideLine("warn", d.text); break;
    case "side_ended": sideLive.clear(); sideLine("system", `侧聊已结束：${d.attempts} 次请求，未知用量 ${d.unknown} 次`); sideState("已结束"); break;
  }
}

function connectSide() {
  const source = new EventSource("/side/events" + query);   // reconnecting only replays what is shown
  source.onmessage = (message) => handleSide(JSON.parse(message.data));
}

$("side-composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("side-input");
  const text = input.value.trim();
  if (!text) return;
  const result = await post("/side/messages" + query, {text, op_id: opId()});
  if (result.ok) input.value = "";   // refused or busy: the text stays
});
$("side-stop").addEventListener("click", () => post("/side/stop" + query, {}));
$("side-retry").addEventListener("click", () => post("/side/retry" + query, {op_id: opId()}));
$("side-end").addEventListener("click", () => post("/side/end" + query, {}));

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("input");
  const text = input.value.trim();
  if (!text) return;
  const result = await post("/messages" + query, {text});
  if (result.ok) input.value = "";   // keep the text when it was rejected; while answering it is steered or queued
});
$("cancel-all").addEventListener("click", () => post("/workers/all/cancel" + query, {}));
setInterval(renderWorkers, 1000);    // elapsed time ticks locally between status events
connect();
connectSide();
