// ===== TOP-LEVEL TAB SWITCHING =====
const navButtons = [...document.querySelectorAll("#main-nav .nav-btn")];
const panels = {
  api: document.getElementById("panel-api"),
  prompt: document.getElementById("panel-prompt"),
  notifications: document.getElementById("panel-notifications"),
  chat: document.getElementById("panel-chat"),
  memory: document.getElementById("panel-memory")
};
const notifFooter = document.getElementById("notif-footer");

function switchTab(tab) {
  for (const button of navButtons) {
    const active = button.dataset.tab === tab;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  }
  for (const [key, panel] of Object.entries(panels)) panel.hidden = key !== tab;
  notifFooter.hidden = tab !== "notifications";
  window.scrollTo(0, 0);
}
for (const button of navButtons) {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
}

const mainNav = document.getElementById("main-nav");
const narrowLayout = window.matchMedia("(max-width: 680px)");
function updateNavOrientation() {
  mainNav.setAttribute("aria-orientation", narrowLayout.matches ? "horizontal" : "vertical");
}
narrowLayout.addEventListener("change", updateNavOrientation);
updateNavOrientation();

mainNav.addEventListener("keydown", event => {
  const index = navButtons.indexOf(event.target);
  if (index < 0) return;

  const previousKey = narrowLayout.matches ? "ArrowLeft" : "ArrowUp";
  const nextKey = narrowLayout.matches ? "ArrowRight" : "ArrowDown";
  let next;
  switch (event.key) {
    case "Home": next = 0; break;
    case "End": next = navButtons.length - 1; break;
    case previousKey: next = (index - 1 + navButtons.length) % navButtons.length; break;
    case nextKey: next = (index + 1) % navButtons.length; break;
    default: return;
  }
  event.preventDefault();
  const button = navButtons[next];
  switchTab(button.dataset.tab);
  button.focus();
});

// ===== API & PROMPT CONFIG =====
const apiEls = {
  path: document.getElementById("cfg-path"),
  endpoint: document.getElementById("cfg-endpoint"),
  model: document.getElementById("cfg-model"),
  keyEnv: document.getElementById("cfg-key-env"),
  keyBadge: document.getElementById("key-env-badge"),
  format: document.getElementById("cfg-format"),
  modalGate: document.getElementById("cfg-modal-gate"),
  fold: document.getElementById("cfg-fold"),
  context: document.getElementById("cfg-context"),
  query: document.getElementById("cfg-query"),
  idle: document.getElementById("cfg-idle"),
  timeout: document.getElementById("cfg-timeout"),
  options: document.getElementById("cfg-options"),
  instructions: document.getElementById("cfg-instructions"),
  promptChars: document.getElementById("prompt-chars"),
  builtinPrompt: document.getElementById("builtin-system-prompt"),
  apiStatus: document.getElementById("api-status"),
  promptStatus: document.getElementById("prompt-status"),
  launchCmd: document.getElementById("launch-cmd"),
  testKey: document.getElementById("test-key"),
  testStatus: document.getElementById("test-status"),
  testOutput: document.getElementById("test-output"),
  copyStatus: document.getElementById("copy-status"),
};

let latestProfile = "captures\\local-game-本次目录\\session_profile.json";

const API_PRESETS = {
  deepseek: { endpoint: "https://api.deepseek.com/v1/chat/completions", model: "deepseek-chat", api_key_env: "DEEPSEEK_API_KEY" },
  openai: { endpoint: "https://api.openai.com/v1/chat/completions", model: "gpt-4o-mini", api_key_env: "OPENAI_API_KEY" },
  gemini: { endpoint: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", model: "gemini-2.5-flash", api_key_env: "GEMINI_API_KEY" },
  local: { endpoint: "http://127.0.0.1:8000/v1/chat/completions", model: "local-model", api_key_env: "" }
};

const PROMPT_PRESETS = {
  ally: "你与房主是长期同盟。开局后先通过 observe 查看自身处境与邻国实力；积极响应房主的联姻、结盟与召集参战请求，优先配合房主作战并及时发兵支援。平时稳步发展直辖经济、妥善处理事件与封臣关系，避免无意义的频繁查询。",
  ireland: "你的战略目标是以当前爱尔兰领主身份逐步统一爱尔兰（先派宫廷牧师对弱小邻国伪造宣称，积攒军费与常备军后再逐个吞并）。注意：严格保留都夫林（Dublin）留给房主，绝不对都夫林宣战。积极与房主维持同盟并互相支援作战。",
  tall: "采用稳健种田与外交发展路线：优先把直辖领地控制在上限内，资金优先投入经济建筑与提升发展度，通过联姻建立强力同盟自保，尽量避免主动挑起高风险战争；遇到事件优先选择降低压力、提升管理/外交与增加收入的选项。",
  conqueror: "采用积极扩张路线：保持高额常备军与军事内阁任务，持续制造宣称并寻找兵力弱于自己的邻国宣战扩张；战时优先使用 war_theater 掌握全局态势，集中兵力歼灭敌军主力后再快速围城议和。"
};

function updateLaunchCmd() {
  const fmt = apiEls.format.value || "compact_text";
  const gate = apiEls.modalGate.value || "strict";
  apiEls.launchCmd.textContent =
    `py ck3_client.py connect-local \`\n` +
    `  --session-profile "${latestProfile}" \`\n` +
    `  --live-memory-pid 当前ck3进程PID \`\n` +
    `  --model-config model_config.json \`\n` +
    `  --llm-format ${fmt} \`\n` +
    `  --modal-gate ${gate} \`\n` +
    `  --model-transcript \`\n` +
    `  --notification-cooldown 60 \`\n` +
    `  --duration 0 --quiet --stdin --ack-received-turn`;
}

function updatePromptCount() {
  apiEls.promptChars.textContent = `${apiEls.instructions.value.length} 字符`;
}

function collectModelConfig() {
  let reqOpts = {};
  const rawOpts = apiEls.options.value.trim();
  if (rawOpts) {
    reqOpts = JSON.parse(rawOpts);
    if (typeof reqOpts !== "object" || Array.isArray(reqOpts) || reqOpts === null) {
      throw new Error("request_options 必须是 JSON 对象");
    }
  }
  return {
    endpoint: apiEls.endpoint.value.trim(),
    model: apiEls.model.value.trim(),
    api_key_env: apiEls.keyEnv.value.trim() || null,
    instructions: apiEls.instructions.value,
    request_timeout: Number(apiEls.timeout.value),
    idle_seconds: Number(apiEls.idle.value),
    max_query_chars: parseInt(apiEls.query.value, 10),
    context_chars: parseInt(apiEls.context.value, 10),
    response_format: apiEls.format.value,
    modal_gate: apiEls.modalGate.value,
    fold_old_tool_results: apiEls.fold.value === "true",
    request_options: reqOpts
  };
}

function applyModelState(state) {
  apiEls.path.textContent = state.path?.split(/[\\/]/).pop() || "model_config.json";
  apiEls.path.title = state.path || "";
  if (state.latest_session_profile) latestProfile = state.latest_session_profile;
  const cfg = state.config || {};
  apiEls.endpoint.value = cfg.endpoint || "";
  apiEls.model.value = cfg.model || "";
  apiEls.keyEnv.value = cfg.api_key_env || "";
  apiEls.format.value = cfg.response_format || "compact_text";
  apiEls.modalGate.value = cfg.modal_gate || "strict";
  apiEls.fold.value = String(cfg.fold_old_tool_results !== false);
  apiEls.context.value = cfg.context_chars ?? 32000;
  apiEls.query.value = cfg.max_query_chars ?? 6000;
  apiEls.idle.value = cfg.idle_seconds ?? 60;
  apiEls.timeout.value = cfg.request_timeout ?? 45;
  apiEls.options.value = JSON.stringify(cfg.request_options || {}, null, 2);
  apiEls.instructions.value = cfg.instructions || "";
  apiEls.builtinPrompt.textContent = state.system_prompt || "";
  if (!cfg.api_key_env) {
    apiEls.keyBadge.className = "pill";
    apiEls.keyBadge.textContent = "无需 Key";
  } else if (state.api_key_present) {
    apiEls.keyBadge.className = "pill ok";
    apiEls.keyBadge.textContent = "环境变量已就绪";
  } else {
    apiEls.keyBadge.className = "pill warn";
    apiEls.keyBadge.textContent = "未检测到环境变量";
  }
  updatePromptCount();
  updateLaunchCmd();
  if (state.error) {
    apiEls.apiStatus.textContent = "配置读取异常：" + state.error;
    apiEls.apiStatus.className = "status-line error";
  } else {
    apiEls.apiStatus.textContent = state.exists ? "已读取配置" : "保存后生成配置文件";
    apiEls.apiStatus.className = "status-line";
  }
}

async function loadModelConfig() {
  try {
    const resp = await fetch("/api/model-config");
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "读取模型配置失败");
    applyModelState(data);
  } catch (err) {
    apiEls.apiStatus.textContent = err.message;
    apiEls.apiStatus.className = "status-line error";
  }
}

async function saveModelConfig(targetStatusEl) {
  targetStatusEl.textContent = "正在校验并保存…";
  targetStatusEl.className = "status-line";
  try {
    const config = collectModelConfig();
    const resp = await fetch("/api/model-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config })
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "保存失败");
    applyModelState(data);
    targetStatusEl.textContent = "已保存到 model_config.json";
    targetStatusEl.className = "status-line";
  } catch (err) {
    targetStatusEl.textContent = "保存失败：" + err.message;
    targetStatusEl.className = "status-line error";
  }
}

document.getElementById("api-presets").addEventListener("click", e => {
  const btn = e.target.closest("[data-preset]");
  if (!btn) return;
  const p = API_PRESETS[btn.dataset.preset];
  apiEls.endpoint.value = p.endpoint;
  apiEls.model.value = p.model;
  apiEls.keyEnv.value = p.api_key_env;
  updateLaunchCmd();
});
document.getElementById("prompt-presets").addEventListener("click", e => {
  const btn = e.target.closest("[data-prompt]");
  if (!btn) return;
  apiEls.instructions.value = PROMPT_PRESETS[btn.dataset.prompt];
  updatePromptCount();
});
apiEls.instructions.addEventListener("input", updatePromptCount);
apiEls.format.addEventListener("change", updateLaunchCmd);
apiEls.modalGate.addEventListener("change", updateLaunchCmd);
document.getElementById("save-api").addEventListener("click", () => saveModelConfig(apiEls.apiStatus));
document.getElementById("save-prompt").addEventListener("click", () => saveModelConfig(apiEls.promptStatus));
document.getElementById("reload-api").addEventListener("click", loadModelConfig);
document.getElementById("copy-cmd").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(apiEls.launchCmd.textContent);
    apiEls.copyStatus.textContent = "已复制到剪贴板";
    setTimeout(() => { apiEls.copyStatus.textContent = ""; }, 2000);
  } catch {
    apiEls.copyStatus.textContent = "复制失败，请手动选中复制";
  }
});
document.getElementById("run-api-test").addEventListener("click", async () => {
  apiEls.testStatus.textContent = "正在发送测试请求…";
  apiEls.testStatus.className = "status-line";
  apiEls.testOutput.hidden = true;
  try {
    const config = collectModelConfig();
    const resp = await fetch("/api/model-test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config, api_key: apiEls.testKey.value.trim() || null })
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "测试请求失败");
    if (data.ok) {
      apiEls.testStatus.textContent = `连通成功！HTTP ${data.status}，耗时 ${data.elapsed_ms} ms`;
      apiEls.testStatus.className = "status-line";
    } else {
      apiEls.testStatus.textContent = `连接失败：${data.error}（耗时 ${data.elapsed_ms} ms）`;
      apiEls.testStatus.className = "status-line error";
    }
    apiEls.testOutput.hidden = false;
    apiEls.testOutput.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    apiEls.testStatus.textContent = "测试异常：" + err.message;
    apiEls.testStatus.className = "status-line error";
  }
});

// ===== INTEGRATED NOTIFICATION MANAGER =====
const nEls = {
  search: document.getElementById("search"),
  stateFilter: document.getElementById("state-filter"),
  sectionTabs: document.getElementById("section-tabs"),
  counts: document.getElementById("counts"),
  groups: document.getElementById("groups"),
  empty: document.getElementById("empty"),
  status: document.getElementById("notif-status"),
  save: document.getElementById("save-notif"),
  reload: document.getElementById("reload-notif"),
  enableAll: document.getElementById("enable-all"),
};

let catalog = null;
let revision = null;
let disabledGroups = new Set();
let disabledTypes = new Set();
let expandedItems = new Set();
let activeSection = "all";
let dirty = false;
let saving = false;
let editVersion = 0;

const SECTIONS = [
  { id: "core", title: "一、必响应弹窗 & 内存状态监测", desc: "居中事件弹窗、人物信件请求、活动邀请，以及军事/角色内存状态直采。", cssClass: "" },
  { id: "top_bar", title: "二、顶部栏通知（顶部警报图标 & 局势下拉列表）", desc: "游戏顶部独立警报图标与局势下拉列表，可按子组或单项精细开关。", cssClass: "top-bar" },
  { id: "side_bar", title: "三、侧栏消息流 & 顶部横幅（游戏消息 player_messages）", desc: "游戏右下角侧栏消息流（Feed）与顶部弹出横幅（Toast），按主题分组。", cssClass: "side-bar" },
  { id: "static", title: "四、仅静态目录（暂未接入运行时）", desc: "主动建议静态目录，当前不会触发也不会消耗 Token。", cssClass: "static-only" }
];

function isClosed(item) { return disabledGroups.has(item.group) || disabledTypes.has(item.id); }
function cleanTemplate(text) { return text ? text.replace(/@[A-Za-z0-9_]+!/g, "").replace(/#(?:[A-Za-z0-9_=:.]+|!)\s*/g, "").trim() : ""; }
function locationTag(item) {
  if (item.namespace === "action") {
    if (item.action_type === "alert") return { text: "顶部警报图标", cls: "loc-top" };
    if (item.action_type === "tutorial") return { text: "顶部新手教程", cls: "loc-top" };
    return { text: "顶部局势下拉", cls: "loc-top" };
  }
  if (item.namespace === "message") {
    if (item.display === "toast") return { text: "顶部横幅 Toast", cls: "loc-side" };
    if (item.display === "popup") return { text: "消息弹窗 Popup", cls: "loc-side" };
    return { text: "侧栏消息流", cls: "loc-side" };
  }
  if (item.namespace === "suggestion") return { text: "仅静态目录", cls: "inactive" };
  if (["pending_events", "pending_interactions", "activity_invitations"].includes(item.group)) return { text: "必响应弹窗", cls: "loc-popup" };
  return { text: "内存状态直采", cls: "loc-mem" };
}

function describeItem(item) {
  if (item.namespace === "message") {
    const slot = item.display === "toast" ? "顶部横幅 Toast" : item.display === "popup" ? "消息弹窗" : "右下角侧栏消息流";
    return {
      producer: `PlayerMessageNotifications (${slot})`,
      wake: "会立即唤醒 wait_for_update",
      brief: item.human_note || cleanTemplate(item.description_template) || `游戏原生消息「${item.label}」`,
      coalesce: "同日同类消息自动合并为 {count, items}",
      data: { id: 9021, type: item.key, filter: item.filter || "default", arrival_date: "1066.9.15", title: item.title_template || item.label }
    };
  }
  if (item.namespace === "action") {
    const slot = item.action_type === "alert" ? "顶部独立警报图标" : item.action_type === "tutorial" ? "顶部新手教程" : "顶部局势下拉列表";
    return {
      producer: `ImportantActionNotifications (${slot})`,
      wake: "会唤醒模型（受 --notification-cooldown 冷却保护）",
      brief: cleanTemplate(item.description_template) || `游戏${slot}条目「${item.label}」`,
      coalesce: "单轮同类型合并为 {count, items}，并受冷却间隔限制",
      data: { count: 1, items: [{ id: 501, type: item.key, character_id: 65238 }] }
    };
  }
  if (item.namespace === "military") {
    return {
      producer: item.key.startsWith("siege_") ? "SiegeNotifications" : "DirectNotifications",
      wake: "会立即唤醒 wait_for_update",
      brief: `实时监控军事状态变化「${item.label}」，在紧凑模式下自动渲染为 1~2 行中文战报。`,
      coalesce: item.key === "movement_stopped" ? "同单位连续停驻自动去重" : "按军事事件触发",
      data: item.key.startsWith("siege_")
        ? { id: 12, province_id: 2214, side: "attacker", progress: { percent: 46.2 }, effects: { breach: 1 }, projection: { estimated_days_remaining: 24 } }
        : { id: 88, province_id: 2215 }
    };
  }
  if (item.namespace === "state") {
    return {
      producer: "角色状态/待处理交互采集器",
      wake: "会立即唤醒 wait_for_update",
      brief: `角色关键交互或状态变化「${item.label}」。`,
      coalesce: ["pending_event", "pending_interaction", "activity_invitation"].includes(item.key) ? "一次性严格去重" : "未消费前连续变化合并为 before→after",
      data: { id: 771, character_id: 65238 }
    };
  }
  return { producer: "静态目录（未接入）", wake: "不会触发", brief: "仅静态目录展示。", coalesce: "无", data: { suggestion: item.key } };
}

function buildVisualGroups() {
  const byGroup = new Map();
  for (const item of catalog.items) {
    if (!byGroup.has(item.group)) byGroup.set(item.group, []);
    byGroup.get(item.group).push(item);
  }
  const visual = [];
  for (const group of catalog.groups) {
    const items = byGroup.get(group.id) || [];
    if (!items.length) continue;
    if (group.id === "important_actions") {
      const buckets = [
        { key: "top_alerts", label: "顶部独立警报图标（Alert）", items: items.filter(i => i.action_type === "alert") },
        { key: "top_actions_danger", label: "顶部局势下拉列表 · 高危警告", items: items.filter(i => i.action_type === "action" && i.dangerous) },
        { key: "top_actions_normal", label: "顶部局势下拉列表 · 常规提示", items: items.filter(i => i.action_type === "action" && !i.dangerous) },
        { key: "top_tutorial", label: "顶部新手教程提示", items: items.filter(i => i.action_type === "tutorial") }
      ];
      for (const b of buckets) {
        if (b.items.length) visual.push({ key: b.key, section: "top_bar", policyGroup: "important_actions", label: b.label, items: b.items, policyGroupItems: items });
      }
      continue;
    }
    const first = items[0];
    const section = (first.namespace === "military" || first.namespace === "state") ? "core" : (first.namespace === "suggestion" ? "static" : "side_bar");
    visual.push({ key: group.id, section, policyGroup: group.id, label: group.label, items, policyGroupItems: items });
  }
  return visual;
}

function setItemsClosed(targetItems, policyGroup, policyGroupItems, closed) {
  if (disabledGroups.has(policyGroup)) {
    disabledGroups.delete(policyGroup);
    for (const entry of policyGroupItems) disabledTypes.add(entry.id);
  }
  for (const entry of targetItems) {
    if (closed) disabledTypes.add(entry.id);
    else disabledTypes.delete(entry.id);
  }
  if (policyGroupItems.every(entry => disabledTypes.has(entry.id))) {
    disabledGroups.add(policyGroup);
    for (const entry of policyGroupItems) disabledTypes.delete(entry.id);
  }
}

function notifStatus(msg, err = false) {
  nEls.status.textContent = msg;
  nEls.status.classList.toggle("error", err);
}
function notifChanged() {
  dirty = true;
  editVersion += 1;
  nEls.save.disabled = saving;
  notifStatus("通知设置有未保存的更改");
}
function notifCounts() {
  const closed = catalog.items.filter(isClosed).length;
  nEls.counts.innerHTML = `<span>共 <strong>${catalog.items.length}</strong> 种通知</span><span>已开启 <strong>${catalog.items.length - closed}</strong> 种</span><span>已关闭 <strong>${closed}</strong> 种</span>`;
}

function createInspector(item) {
  const info = describeItem(item);
  const box = document.createElement("div");
  box.className = "inspector";
  const grid = document.createElement("div");
  grid.className = "inspector-grid";
  for (const [k, v] of [["来源", info.producer], ["唤醒", info.wake], ["合并/去重", info.coalesce]]) {
    const cell = document.createElement("div");
    cell.className = "meta-box";
    cell.innerHTML = `<div class="meta-label"></div><div class="meta-value"></div>`;
    cell.querySelector(".meta-label").textContent = k;
    cell.querySelector(".meta-value").textContent = v;
    grid.append(cell);
  }
  box.append(grid);
  const h = document.createElement("h4");
  h.textContent = "发给 LLM 的事件载荷示例（compact_text 模式下会进一步压缩为 1~2 行中文简报）";
  const pre = document.createElement("pre");
  pre.className = "code-box";
  pre.textContent = JSON.stringify({ event: "game_notification", notification_id: item.id, summary: item.label, date: "1066.9.15", data: info.data }, null, 2);
  box.append(h, pre);
  return box;
}

function renderNotifs() {
  if (!catalog) return;
  const search = nEls.search.value.trim().toLocaleLowerCase();
  const stateFilter = nEls.stateFilter.value;
  const openGroups = new Set([...nEls.groups.querySelectorAll("details[open]")].map(g => g.dataset.group));
  const visualGroups = buildVisualGroups();
  const frag = document.createDocumentFragment();
  let visibleCount = 0;

  for (const section of SECTIONS) {
    if (activeSection !== "all" && activeSection !== section.id) continue;
    const sectionGroups = visualGroups.filter(g => g.section === section.id);
    const rendered = [];
    let secTotal = 0, secEnabled = 0;
    for (const group of sectionGroups) {
      secTotal += group.items.length;
      const enabledInGroup = group.items.filter(i => !isClosed(i)).length;
      secEnabled += enabledInGroup;
      const matched = group.items.filter(i => {
        const closed = isClosed(i);
        if (stateFilter === "enabled" && closed) return false;
        if (stateFilter === "disabled" && !closed) return false;
        if (!search) return true;
        return `${i.label} ${i.id} ${i.human_note || ""} ${i.filter_label || ""}`.toLocaleLowerCase().includes(search);
      });
      if (!matched.length) continue;
      visibleCount += matched.length;
      const closedInGroup = group.items.length - enabledInGroup;
      const details = document.createElement("details");
      details.className = "group";
      details.dataset.group = group.key;
      details.open = Boolean(search) || stateFilter === "enabled" || openGroups.has(group.key);

      const summary = document.createElement("summary");
      const title = document.createElement("span");
      title.className = "group-name";
      title.textContent = group.label;
      const badge = document.createElement("span");
      badge.className = "group-badge" + (enabledInGroup > 0 ? " active" : "");
      badge.textContent = `已开启 ${enabledInGroup} / ${group.items.length}`;
      const gLabel = document.createElement("label");
      gLabel.className = "group-toggle";
      gLabel.addEventListener("click", e => e.stopPropagation());
      const gCb = document.createElement("input");
      gCb.type = "checkbox";
      gCb.checked = closedInGroup === group.items.length;
      gCb.indeterminate = closedInGroup > 0 && closedInGroup < group.items.length;
      gCb.addEventListener("change", () => {
        setItemsClosed(group.items, group.policyGroup, group.policyGroupItems, gCb.checked);
        notifChanged();
        renderNotifs();
      });
      gLabel.append(gCb, "全选关闭");
      summary.append(title, badge, gLabel);

      const list = document.createElement("div");
      list.className = "type-list";
      for (const item of matched) {
        const row = document.createElement("div");
        row.className = "type-row";
        const head = document.createElement("div");
        head.className = "type-head";
        head.classList.toggle("muted", isClosed(item));

        const cLabel = document.createElement("label");
        cLabel.className = "type-check";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = isClosed(item);
        cb.addEventListener("change", () => {
          setItemsClosed([item], group.policyGroup, group.policyGroupItems, cb.checked);
          notifChanged();
          notifCounts();
          head.classList.toggle("muted", isClosed(item));
          const ue = group.items.filter(x => !isClosed(x)).length;
          const uc = group.items.length - ue;
          badge.textContent = `已开启 ${ue} / ${group.items.length}`;
          badge.className = "group-badge" + (ue > 0 ? " active" : "");
          gCb.checked = uc === group.items.length;
          gCb.indeterminate = uc > 0 && uc < group.items.length;
        });

        const text = document.createElement("div");
        text.className = "type-text";
        const tLine = document.createElement("div");
        tLine.className = "type-title-line";
        const name = document.createElement("span");
        name.className = "type-title";
        name.textContent = item.label;
        tLine.append(name);
        const loc = locationTag(item);
        const locSpan = document.createElement("span");
        locSpan.className = "tag " + loc.cls;
        locSpan.textContent = loc.text;
        tLine.append(locSpan);
        if (item.dangerous) {
          const dTag = document.createElement("span");
          dTag.className = "tag danger";
          dTag.textContent = "高危提示";
          tLine.append(dTag);
        }
        const key = document.createElement("div");
        key.className = "type-key";
        key.textContent = item.id;
        const brief = document.createElement("div");
        brief.className = "type-brief";
        brief.textContent = describeItem(item).brief;
        text.append(tLine, key, brief);
        cLabel.append(cb, text);

        const iBtn = document.createElement("button");
        iBtn.type = "button";
        iBtn.className = "inspect-btn";
        const exp = expandedItems.has(item.id);
        iBtn.setAttribute("aria-expanded", String(exp));
        iBtn.textContent = exp ? "收起内容" : "查看发给 LLM 的内容";
        iBtn.addEventListener("click", () => {
          if (expandedItems.has(item.id)) {
            expandedItems.delete(item.id);
            iBtn.setAttribute("aria-expanded", "false");
            iBtn.textContent = "查看发给 LLM 的内容";
            const ex = row.querySelector(".inspector");
            if (ex) ex.remove();
          } else {
            expandedItems.add(item.id);
            iBtn.setAttribute("aria-expanded", "true");
            iBtn.textContent = "收起内容";
            row.append(createInspector(item));
          }
        });
        head.append(cLabel, iBtn);
        row.append(head);
        if (exp) row.append(createInspector(item));
        list.append(row);
      }
      details.append(summary, list);
      rendered.push(details);
    }
    if (!rendered.length) continue;
    const secEl = document.createElement("section");
    secEl.className = "section-block";
    const header = document.createElement("div");
    header.className = "section-header " + section.cssClass;
    header.innerHTML = `<span class="section-title"></span><span class="section-stat"></span><span class="section-desc"></span>`;
    header.querySelector(".section-title").textContent = section.title;
    header.querySelector(".section-stat").textContent = `已开启 ${secEnabled} / ${secTotal}`;
    header.querySelector(".section-desc").textContent = section.desc;
    secEl.append(header, ...rendered);
    frag.append(secEl);
  }
  nEls.groups.replaceChildren(frag);
  nEls.empty.hidden = visibleCount > 0;
  notifCounts();
}

async function loadNotifs() {
  nEls.reload.disabled = true;
  nEls.save.disabled = true;
  try {
    const resp = await fetch("/api/settings");
    if (!resp.ok) throw new Error("读取通知设置失败");
    const data = await resp.json();
    catalog = data;
    revision = data.revision;
    disabledGroups = new Set(data.policy.disabled_groups);
    disabledTypes = new Set(data.policy.disabled_types);
    dirty = false;
    nEls.enableAll.disabled = false;
    notifStatus(data.error ? `配置错误：${data.error}` : "已读取保存的通知设置", Boolean(data.error));
    renderNotifs();
  } catch (err) {
    notifStatus(err.message, true);
  } finally {
    nEls.reload.disabled = false;
  }
}

nEls.sectionTabs.addEventListener("click", e => {
  const btn = e.target.closest(".tab-btn");
  if (!btn) return;
  activeSection = btn.dataset.section;
  for (const t of nEls.sectionTabs.querySelectorAll(".tab-btn")) t.classList.toggle("active", t === btn);
  renderNotifs();
});
nEls.search.addEventListener("input", renderNotifs);
nEls.stateFilter.addEventListener("change", renderNotifs);
nEls.reload.addEventListener("click", loadNotifs);
nEls.enableAll.addEventListener("click", () => {
  disabledGroups.clear();
  disabledTypes.clear();
  notifChanged();
  renderNotifs();
});
nEls.save.addEventListener("click", async () => {
  saving = true;
  nEls.save.disabled = true;
  nEls.reload.disabled = true;
  const sv = editVersion;
  try {
    const resp = await fetch("/api/policy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision, policy: { version: 1, disabled_groups: [...disabledGroups], disabled_types: [...disabledTypes] } })
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(resp.status === 409 ? "设置已被其他页面修改，请重新读取" : data.error);
    revision = data.revision;
    dirty = editVersion !== sv;
    notifStatus(dirty ? "已保存先前更改，仍有未保存项" : "通知设置已保存，运行中的虚拟机会自动热重载");
  } catch (err) {
    notifStatus(err.message, true);
  } finally {
    saving = false;
    nEls.reload.disabled = false;
    nEls.save.disabled = !dirty;
  }
});

// ===== LIVE PLAYER <-> LLM CHAT =====
const chatEls = {
  log: document.getElementById("chat-log"),
  input: document.getElementById("chat-input"),
  send: document.getElementById("chat-send"),
  clear: document.getElementById("chat-clear"),
  status: document.getElementById("chat-status"),
  quick: document.getElementById("chat-quick"),
};
let chatMessages = [];

function renderChat() {
  if (!chatMessages.length) {
    chatEls.log.innerHTML = `<div class="chat-empty">暂无对话记录。启动模型客户端后，可在这里与它交流。</div>`;
    return;
  }
  const atBottom = chatEls.log.scrollHeight - chatEls.log.scrollTop - chatEls.log.clientHeight < 60;
  const frag = document.createDocumentFragment();
  for (const m of chatMessages) {
    const box = document.createElement("div");
    box.className = "msg " + (m.role === "player" ? "player" : "assistant");
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const who = m.role === "player" ? "你（房主）" : "LLM（联机角色）";
    meta.textContent = `${who} · ${m.time || ""}`;
    if (m.meta && m.meta.tool) {
      const pill = document.createElement("span");
      pill.className = "pill";
      pill.textContent = `工具: ${m.meta.tool}`;
      meta.append(pill);
    }
    const body = document.createElement("div");
    body.textContent = m.text || "";
    box.append(meta, body);
    frag.append(box);
  }
  chatEls.log.replaceChildren(frag);
  if (atBottom) chatEls.log.scrollTop = chatEls.log.scrollHeight;
}

async function pollChat() {
  try {
    const resp = await fetch("/api/chat");
    if (!resp.ok) return;
    const data = await resp.json();
    const incoming = data.messages || [];
    if (incoming.length !== chatMessages.length
        || (incoming.length && incoming[incoming.length - 1].id !== (chatMessages[chatMessages.length - 1] || {}).id)) {
      chatMessages = incoming;
      renderChat();
    }
  } catch {}
}

async function sendChat() {
  const text = chatEls.input.value.trim();
  if (!text) return;
  chatEls.send.disabled = true;
  chatEls.status.textContent = "正在发送…";
  chatEls.status.className = "status-line";
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text })
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "发送失败");
    chatEls.input.value = "";
    chatEls.status.textContent = "已发送，等待运行中的 LLM 响应…";
    await pollChat();
    chatEls.log.scrollTop = chatEls.log.scrollHeight;
  } catch (err) {
    chatEls.status.textContent = "发送失败：" + err.message;
    chatEls.status.className = "status-line error";
  } finally {
    chatEls.send.disabled = false;
    chatEls.input.focus();
  }
}

chatEls.send.addEventListener("click", sendChat);
chatEls.input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
});
chatEls.quick.addEventListener("click", e => {
  const btn = e.target.closest("[data-quick]");
  if (!btn) return;
  chatEls.input.value = btn.dataset.quick;
  chatEls.input.focus();
});
chatEls.clear.addEventListener("click", async () => {
  await fetch("/api/chat", { method: "DELETE" });
  chatMessages = [];
  renderChat();
  chatEls.status.textContent = "已清空对话记录";
});
setInterval(pollChat, 1500);

// ===== LLM MEMORY & EXPERIENCE MANAGER =====
const memEls = {
  scope: document.getElementById("mem-scope"),
  category: document.getElementById("mem-category"),
  content: document.getElementById("mem-content"),
  add: document.getElementById("mem-add"),
  reload: document.getElementById("mem-reload"),
  clearCampaign: document.getElementById("mem-clear-campaign"),
  clearAll: document.getElementById("mem-clear-all"),
  status: document.getElementById("mem-status"),
  filterTabs: document.getElementById("mem-filter-tabs"),
  counts: document.getElementById("mem-counts"),
  list: document.getElementById("mem-list"),
  preview: document.getElementById("mem-preview"),
};
let memState = null;
let memFilter = "all";

function renderMemory() {
  if (!memState) return;
  const c = memState.counts || {};
  const previewText = memState.prompt_preview || "（当前无启用的记忆，不会占用系统提示词字符）";
  memEls.counts.innerHTML =
    `<span>共 <strong>${c.total || 0}</strong> 条</span>` +
    `<span>本局记忆 <strong>${c.campaign || 0}</strong> / 30</span>` +
    `<span>跨局经验 <strong>${c.experience || 0}</strong> / 30</span>` +
    `<span>已启用 <strong>${c.enabled || 0}</strong> 条（注入前缀约 ${memState.prompt_preview ? memState.prompt_preview.length : 0} 字符）</span>`;
  memEls.preview.textContent = previewText;

  const items = (memState.items || []).filter(i => memFilter === "all" || i.scope === memFilter);
  if (!items.length) {
    memEls.list.innerHTML = `<div class="empty-state">当前分类暂无记忆。</div>`;
    return;
  }
  const cats = memState.categories || {};
  const frag = document.createDocumentFragment();
  for (const item of items) {
    const card = document.createElement("div");
    card.className = "mem-item" + (item.enabled ? "" : " disabled");

    const body = document.createElement("div");
    body.className = "mem-body";
    const badges = document.createElement("div");
    badges.className = "mem-badges";
    const sTag = document.createElement("span");
    sTag.className = "tag " + (item.scope === "campaign" ? "loc-top" : "loc-side");
    sTag.textContent = item.scope === "campaign" ? "本局记忆" : "跨局经验";
    const cTag = document.createElement("span");
    cTag.className = "tag loc-popup";
    cTag.textContent = cats[item.category] || item.category;
    const srcTag = document.createElement("span");
    srcTag.className = "tag";
    srcTag.textContent = item.source === "llm" ? "LLM 自动记录" : "玩家手动添加";
    const timeTag = document.createElement("span");
    timeTag.className = "hint";
    timeTag.textContent = `${item.updated_at || item.created_at || ""} · id=${item.id}`;
    badges.append(sTag, cTag, srcTag, timeTag);

    const text = document.createElement("div");
    text.className = "mem-text";
    text.textContent = item.content;
    body.append(badges, text);

    const acts = document.createElement("div");
    acts.className = "mem-actions";
    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.textContent = item.enabled ? "停用" : "启用";
    toggleBtn.addEventListener("click", async () => {
      const resp = await fetch("/api/memory", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: item.id, enabled: !item.enabled })
      });
      if (resp.ok) { memState = await resp.json(); renderMemory(); }
    });
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.textContent = "编辑";
    editBtn.addEventListener("click", async () => {
      const next = prompt("编辑记忆内容（≤200字）：", item.content);
      if (next === null || !next.trim()) return;
      const resp = await fetch("/api/memory", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: item.id, content: next.trim() })
      });
      if (resp.ok) { memState = await resp.json(); renderMemory(); memEls.status.textContent = "已更新记忆"; }
    });
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "danger";
    delBtn.textContent = "删除";
    delBtn.addEventListener("click", async () => {
      const resp = await fetch(`/api/memory?id=${encodeURIComponent(item.id)}`, { method: "DELETE" });
      if (resp.ok) { memState = await resp.json(); renderMemory(); memEls.status.textContent = "已删除该条记忆"; }
    });
    acts.append(toggleBtn, editBtn, delBtn);
    card.append(body, acts);
    frag.append(card);
  }
  memEls.list.replaceChildren(frag);
}

async function loadMemory() {
  try {
    const resp = await fetch("/api/memory");
    if (!resp.ok) return;
    memState = await resp.json();
    renderMemory();
  } catch {}
}

memEls.add.addEventListener("click", async () => {
  const content = memEls.content.value.trim();
  if (!content) return;
  const resp = await fetch("/api/memory", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope: memEls.scope.value, category: memEls.category.value, content })
  });
  const data = await resp.json();
  if (!resp.ok) { memEls.status.textContent = "添加失败：" + data.error; memEls.status.className = "status-line error"; return; }
  memEls.content.value = "";
  memState = data;
  renderMemory();
  memEls.status.textContent = "已添加，运行中的 LLM 下一轮即生效";
  memEls.status.className = "status-line";
});
memEls.reload.addEventListener("click", loadMemory);
memEls.clearCampaign.addEventListener("click", async () => {
  if (!confirm("确定清空所有「本局记忆」吗？（跨局经验会保留）")) return;
  const resp = await fetch("/api/memory?scope=campaign", { method: "DELETE" });
  if (resp.ok) { memState = await resp.json(); renderMemory(); memEls.status.textContent = "已清空本局记忆"; }
});
memEls.clearAll.addEventListener("click", async () => {
  if (!confirm("确定清空全部记忆（含本局记忆和跨局经验）吗？")) return;
  const resp = await fetch("/api/memory?scope=all", { method: "DELETE" });
  if (resp.ok) { memState = await resp.json(); renderMemory(); memEls.status.textContent = "已清空全部记忆"; }
});
memEls.filterTabs.addEventListener("click", e => {
  const btn = e.target.closest("[data-mscope]");
  if (!btn) return;
  memFilter = btn.dataset.mscope;
  for (const b of memEls.filterTabs.querySelectorAll("[data-mscope]")) b.classList.toggle("active", b === btn);
  renderMemory();
});

loadModelConfig();
loadNotifs();
pollChat();
loadMemory();
