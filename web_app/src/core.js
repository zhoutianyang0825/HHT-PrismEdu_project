/* ============================================================================
 * core.js · 前端基础设施
 *   ① 后端契约收口（唯一改动点：API_BASE）
 *   ② JWT 会话存取（localStorage，刷新不掉登录）
 *   ③ REST + SSE 两种调用方式
 *   ④ 视图公用小件：toast / modal / 转义 / 数值格式 / 图标
 *
 * 后端契约来源：app/api/{auth,student,teacher,research,admin}.py
 * 跨源说明：后端 main.py 已放开 CORS（allow_origins=["*"]），
 *          本前端由静态服务器另起端口，直连即可。
 * ========================================================================== */

/* 后端地址。默认「当前页面的主机名 + 8100」，所以直接打开 http://localhost:5173/
 * 就能连上后端，不需要带任何参数：
 *   本机        localhost:5173   → localhost:8100
 *   127.0.0.1   127.0.0.1:5173   → 127.0.0.1:8100
 *   局域网演示  192.168.1.20:5173 → 192.168.1.20:8100（后端需 --host 0.0.0.0）
 * 特殊部署（反代、异地后端）才用 ?api=http://host:port 覆盖，界面上不暴露。 */
const API_PORT = 8100;
const API_HOST = location.hostname || "localhost";   // file:// 下为空串，兜底 localhost
const QUERY_API = new URLSearchParams(location.search).get("api");
export const API_BASE = (QUERY_API || `http://${API_HOST}:${API_PORT}`).replace(/\/+$/, "");

/* 后端地址和当前页面同源 = 请求会打到前端自己身上（静态服务器对 POST 回 501）。
 * 这是最容易误判成「后端坏了」的配置错误，单独给一条人话报错。 */
function pointsAtPageItself() {
  try { return new URL(API_BASE).origin === location.origin; }
  catch { return false; }
}

function misconfigHint(status) {
  if ((status === 501 || status === 404) && pointsAtPageItself()) {
    return `后端地址指到了前端自己（${API_BASE}）。请用 python3 serve.py 启动（默认 5173，自动指向 8100）；`
         + `或在网址后加 ?api=http://localhost:8100。`;
  }
  return "";
}

/* fetch 直接抛异常时，需要区分两种完全不同的情况：
 *   ① 后端没起            → TCP 连不上
 *   ② 后端起了但接口 500  → 5xx 由 Starlette 最外层直接返回，不带 CORS 头，
 *                          浏览器一律报 MissingAllowOriginHeader，fetch 直接 reject
 * 不加区分就会把「数据库没起」误报成「uvicorn 没起」，排查方向直接跑偏。
 * 这里用 /health 探一下：健康就说明是②。 */
async function offlineHint() {
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 2500);
    const res = await fetch(`${API_BASE}/health`, { signal: ctl.signal, cache: "no-store" });
    clearTimeout(timer);
    if (res.ok) {
      return `后端 ${API_BASE} 在跑，但接口返回了 5xx（浏览器会把它报成跨源失败）。`
           + `常见原因：PostgreSQL 未启动，请看后端终端日志。`;
    }
    return `后端 ${API_BASE} 响应异常（HTTP ${res.status}），请查看后端终端日志。`;
  } catch {
    return `连不上后端 ${API_BASE}：确认 uvicorn 已启动，且端口与你打开的前端地址一致。`;
  }
}

/* 404/501 且响应体不是 JSON（detailOf 会退化成 "HTTP 404"）= 那个端口上不是 FastAPI。
 * 最常见的原因是端口指错，或者页面还是旧版本（旧版本默认连 8000）。 */
function notApiHint(status, detail) {
  if ((status === 404 || status === 501) && /^HTTP \d+$/.test(detail)) {
    return `后端 ${API_BASE} 上没有这个接口（HTTP ${status}，返回的不是 JSON）。`
         + `多半是端口指错或页面是旧版本：后端应在 ${API_PORT} 端口，按 ⌘⇧R 强制刷新本页再试。`;
  }
  return "";
}

/* ---------------------------------------------------------------- 会话 */
const TOKEN_KEY = "prism.token";
const ME_KEY = "prism.me";

export const session = {
  get token() { return localStorage.getItem(TOKEN_KEY) || ""; },
  get me() {
    try { return JSON.parse(localStorage.getItem(ME_KEY) || "null"); }
    catch { return null; }
  },
  save(data) {
    localStorage.setItem(TOKEN_KEY, data.access_token);
    localStorage.setItem(ME_KEY, JSON.stringify({
      user_id: data.user_id,
      username: data.username,
      name: data.name,
      roles: data.roles || [],
      student_id: data.student_id ?? null,
    }));
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(ME_KEY);
  },
};

/* ---------------------------------------------------------------- 错误 */
export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function detailOf(res) {
  try {
    const data = await res.json();
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("；");
    }
    return JSON.stringify(data).slice(0, 200);
  } catch {
    return (await res.text().catch(() => "")).slice(0, 200) || `HTTP ${res.status}`;
  }
}

function authExpired() {
  session.clear();
  if (!location.hash.startsWith("#/login")) {
    location.hash = "#/login";
    toast("登录已失效，请重新登录", "err");
  }
}

/* ---------------------------------------------------------------- REST */
export async function api(method, path, { body, params } = {}) {
  let url = API_BASE + path;
  if (params) {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") qs.append(k, v);
    });
    const s = qs.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }

  let res;
  try {
    res = await fetch(url, {
      method,
      headers: {
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(session.token ? { Authorization: `Bearer ${session.token}` } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, await offlineHint());
  }

  if (res.status === 401) { authExpired(); throw new ApiError(401, "登录已失效"); }
  if (!res.ok) {
    const detail = await detailOf(res);
    throw new ApiError(res.status,
      misconfigHint(res.status) || notApiHint(res.status, detail) || detail);
  }
  if (res.status === 204) return null;
  return res.json().catch(() => null);
}

export const http = {
  get: (p, params) => api("GET", p, { params }),
  post: (p, body) => api("POST", p, { body }),
  put: (p, body) => api("PUT", p, { body }),
  del: (p) => api("DELETE", p),
};

/* ---------------------------------------------------------------- 登录 */
export async function login(username, password) {
  let res;
  try {
    res = await fetch(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
  } catch {
    throw new ApiError(0, await offlineHint());
  }
  if (!res.ok) {
    const detail = await detailOf(res);
    throw new ApiError(res.status,
      misconfigHint(res.status) || notApiHint(res.status, detail) || detail);
  }
  const data = await res.json();
  session.save(data);
  return data;
}

/* ---------------------------------------------------------------- SSE */
/**
 * 学生端对话流（契约见 app/api/student.py 顶部协议注释）
 * 帧格式：`data: {json}\n\n`，json.type ∈ node | interrupt | message | error | end
 * @param {string} path  /api/student/answer/stream 或 /api/student/resume
 * @param {object} body  {student_id, question_id, message}
 * @param {(type:string, payload:any)=>void} onEvent
 */
export async function streamChat(path, body, onEvent) {
  let res;
  try {
    res = await fetch(API_BASE + path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(session.token ? { Authorization: `Bearer ${session.token}` } : {}),
      },
      body: JSON.stringify(body),
    });
  } catch {
    onEvent("error", { message: await offlineHint() });
    return;
  }

  if (res.status === 401) { authExpired(); onEvent("error", { message: "登录已失效" }); return; }
  if (!res.ok || !res.body) {
    onEvent("error", { message: await detailOf(res) });
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";               // 末段可能是半帧，留到下一轮
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;                      // SSE 注释行/心跳
        let evt;
        try { evt = JSON.parse(line.slice(6)); } catch { continue; }
        onEvent(evt.type, evt.payload ?? evt);
        if (evt.type === "end") return;
      }
    }
  } catch (err) {
    onEvent("error", { message: `连接中断：${err.message}` });
  }
}

/* ---------------------------------------------------------------- 文案与数值 */
export const ROLE_LABEL = {
  student: "学生端",
  teacher: "老师端",
  researcher: "教研端",
  admin: "管理后台",
};

export function levelOf(p) {
  if (p >= 0.85) return { key: "good", text: "掌握", cls: "bar-good" };
  if (p >= 0.6) return { key: "ok", text: "基本掌握", cls: "" };
  if (p >= 0.4) return { key: "mid", text: "薄弱", cls: "bar-mid" };
  return { key: "weak", text: "严重薄弱", cls: "bar-weak" };
}

export function pct(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return `${Math.round(Number(v) * 100)}%`;
}

export function fixed(v, n = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(n);
}

export function dateOf(str) {
  if (!str) return "—";
  const d = new Date(String(str).replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return String(str).slice(0, 16);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function greeting() {
  const h = new Date().getHours();
  if (h < 6) return "夜深了";
  if (h < 11) return "早上好";
  if (h < 14) return "中午好";
  if (h < 18) return "下午好";
  return "晚上好";
}

export function esc(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/* ---------------------------------------------------------------- 轻量 Markdown */
/**
 * 把 LLM 输出的 Markdown 渲染成 HTML（学生端讲解、AI 评语共用）。
 *
 * 只支持最常用的一小撮语法，够用且不会引入外部库（离线环境拉不到 CDN）：
 *   **加粗**   *斜体*   `行内代码`   ``` 代码块```   1. 有序列表   - 无序列表
 *   > 引用行   #### 小标题   段落（空行分段）
 *
 * ★ 安全顺序：先 esc 转义，再做标签替换——绝不能反过来，
 *   否则用户输入的 <script> 会被当成标签插进 DOM。
 */
export function md(text) {
  const raw = String(text ?? "").replace(/\r\n/g, "\n");
  const lines = raw.split("\n");

  const out = [];
  let inCode = false;
  let codeBuf = [];
  let listTag = "";      // 当前列表类型："ul" | "ol"
  let listItems = [];    // [{parts: [html]}]，一个 item 可有多段（缩进续行并进来）
  let pendingGap = 0;    // 攒着的空行数，落成一条间距分隔

  const pushBlock = (html) => {
    if (pendingGap > 0) { out.push(`<div class="md-gap"></div>`); pendingGap = 0; }
    out.push(html);
  };

  const flushList = () => {
    if (!listItems.length) { listTag = ""; return; }
    const items = listItems.map((it) => `<li>${it.parts.join("")}</li>`);
    pushBlock(`<${listTag}>${items.join("")}</${listTag}>`);
    listItems = [];
    listTag = "";
  };

  for (const line of lines) {
    // ---- 代码块：原样保留，不解析任何标记 ----
    if (/^\s*```/.test(line)) {
      if (!inCode) { flushList(); inCode = true; codeBuf = []; }
      else { pushBlock(`<pre class="md-code"><code>${esc(codeBuf.join("\n"))}</code></pre>`); inCode = false; codeBuf = []; }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }

    // ---- 空行：记一次间距（连续空行只算一次） ----
    if (!line.trim()) { flushList(); pendingGap++; continue; }

    // ---- 列表：连续同类行合并成一个 <ul>/<ol> ----
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ol || ul) {
      const t = ol ? "ol" : "ul";
      if (listTag && listTag !== t) flushList();
      listTag = t;
      listItems.push({ parts: [inlineMd((ol || ul)[1])] });
      continue;
    }

    // ---- 缩进续行：并进上一个列表项（LLM 常把公式/说明缩进写在列表项下面） ----
    if (/^\s+\S/.test(line) && listItems.length) {
      listItems[listItems.length - 1].parts.push(`<div class="md-li-cont">${inlineMd(line.trim())}</div>`);
      continue;
    }
    flushList();

    // ---- 整行加粗：当成小标题（LLM 用 **加粗** 标小节，比 # 标题更常见） ----
    const bold = line.match(/^\s*\*\*(.+?)\*\*\s*$/);
    if (bold) {
      pushBlock(`<div class="md-sub">${inlineMd(bold[1])}</div>`);
      continue;
    }

    // ---- 标题 ----
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const lv = Math.min(h[1].length + 2, 6);   // h1~h4 → h3~h6，避免撑破气泡排版
      pushBlock(`<h${lv} class="md-h">${inlineMd(h[2])}</h${lv}>`);
      continue;
    }

    // ---- 引用 ----
    const q = line.match(/^>\s?(.*)$/);
    if (q) { pushBlock(`<div class="md-quote">${inlineMd(q[1])}</div>`); continue; }

    // ---- 普通行 ----
    pushBlock(`<div class="md-p">${inlineMd(line)}</div>`);
  }
  if (inCode && codeBuf.length) pushBlock(`<pre class="md-code"><code>${esc(codeBuf.join("\n"))}</code></pre>`);
  flushList();

  return out.join("");
}

/** 行内标记：代码 → 加粗 → 斜体（顺序有讲究：先吃反引号，避免把 ** 当成代码） */
export function mdInline(s) {
  let t = esc(s);
  t = t.replace(/`([^`]+)`/g, "<code class=\"md-icode\">$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  return t;
}

/** md() 内部用（保持行内标记逻辑只有一份） */
function inlineMd(s) {
  return mdInline(s);
}

export function clip(str, n = 40) {
  const s = String(str ?? "");
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

export function shortName(name, n = 8) {
  const s = String(name ?? "");
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

/* ---------------------------------------------------------------- toast */
export function toast(message, kind = "") {
  const root = document.getElementById("toast-root");
  if (!root) return;
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  root.appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity .25s ease, transform .25s ease";
    node.style.opacity = "0";
    node.style.transform = "translateY(8px)";
    setTimeout(() => node.remove(), 260);
  }, kind === "err" ? 4200 : 2600);
}

/* ---------------------------------------------------------------- modal */
export function modal({ title, sub, body, actions = [], onMount }) {
  const root = document.getElementById("modal-root");
  root.innerHTML = "";
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="modal-hd">
        <div>
          <h3>${esc(title)}</h3>
          ${sub ? `<div class="card-sub">${esc(sub)}</div>` : ""}
        </div>
        <button class="close" aria-label="关闭">✕</button>
      </div>
      <div class="modal-bd"></div>
      <div class="modal-ft"></div>
    </div>`;
  const bd = overlay.querySelector(".modal-bd");
  if (typeof body === "string") bd.innerHTML = body;
  else if (body) bd.appendChild(body);

  const ft = overlay.querySelector(".modal-ft");
  actions.forEach((a, i) => {
    const btn = document.createElement("button");
    btn.className = `btn ${a.kind === "primary" ? "btn-primary" : a.kind === "danger" ? "btn-danger" : "btn-secondary"}`;
    btn.textContent = a.label;
    btn.onclick = async () => {
      if (a.onClick) {
        // 把弹窗按钮自己传给 onClick：调用方可以做忙碌态/禁点，
        // 否则慢请求期间弹窗按钮看起来"没反应"，用户会连点（考试交卷踩过）
        const keep = await a.onClick({ close: closeModal, btn });
        if (keep === false) return;
      }
      if (a.keepOpen !== true) closeModal();
    };
    if (i === 0 && actions.length > 1) btn.dataset.secondary = "1";
    ft.appendChild(btn);
  });
  if (!actions.length) ft.remove();

  const close = () => closeModal();
  overlay.querySelector(".close").onclick = close;
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", function onEsc(e) {
    if (e.key === "Escape") { document.removeEventListener("keydown", onEsc); close(); }
  });

  root.appendChild(overlay);
  if (onMount) onMount(overlay);
  return overlay;
}

export function closeModal() {
  const root = document.getElementById("modal-root");
  if (root) root.innerHTML = "";
}

/* ---------------------------------------------------------------- 按钮忙碌态 */
export function busy(btn, text = "处理中…") {
  if (!btn) return () => {};
  const html = btn.innerHTML;
  btn.classList.add("is-busy");
  btn.disabled = true;                       // 忙碌期禁点：慢请求时用户连点会触发重复提交
  btn.innerHTML = `<span class="spin"></span>${esc(text)}`;
  return () => {
    btn.classList.remove("is-busy");
    btn.disabled = false;
    btn.innerHTML = html;
  };
}

/* ---------------------------------------------------------------- 图标（线性 stroke，1.7px） */
const ICONS = {
  home: '<path d="M3 9.5 10 3l7 6.5V17a1 1 0 0 1-1 1h-3.5v-5h-5v5H4a1 1 0 0 1-1-1z"/>',
  chat: '<path d="M17 11a6.5 6.5 0 0 1-6.5 6.5H6l-3 2v-4.2A6.5 6.5 0 0 1 10.5 4.5 6.5 6.5 0 0 1 17 11z"/>',
  target: '<circle cx="10" cy="10" r="7"/><circle cx="10" cy="10" r="3"/><path d="M10 3v2M10 15v2M3 10h2M15 10h2"/>',
  chart: '<path d="M3 17V9M8 17V4M13 17v-6M18 17V7"/>',
  users: '<circle cx="8" cy="7.5" r="2.8"/><path d="M2.5 17c0-3 2.5-4.8 5.5-4.8S13.5 14 13.5 17"/><path d="M14 5.2a2.7 2.7 0 0 1 0 5.2M15 12.6c1.7.4 2.8 1.8 2.8 4.4"/>',
  upload: '<path d="M10 13V3M6.5 6.5 10 3l3.5 3.5"/><path d="M3.5 13v3A1.2 1.2 0 0 0 4.7 17h10.6a1.2 1.2 0 0 0 1.2-1v-3"/>',
  link: '<path d="M8.4 11.6 11.6 8.4"/><path d="M9.2 6.4 10.6 5a3.5 3.5 0 0 1 5 5l-1.4 1.4"/><path d="M10.8 13.6 9.4 15a3.5 3.5 0 0 1-5-5l1.4-1.4"/>',
  medal: '<circle cx="10" cy="7.5" r="4"/><path d="M6.5 10.6 5 17l5-2.4L15 17l-1.5-6.4"/>',
  building: '<path d="M3.5 17V4.5h8V17"/><path d="M11.5 8H17v9"/><path d="M2 17h16"/><path d="M6 7.5h2M6 10.5h2M6 13.5h2"/>',
  log: '<path d="M5 3.5h7l3.5 3.5v10H5z"/><path d="M11.8 3.6V7h3.4"/><path d="M7.5 11h6M7.5 13.8h4"/>',
  sparkle: '<path d="M10 3.2 11.7 8 16.5 9.7 11.7 11.4 10 16.2 8.3 11.4 3.5 9.7 8.3 8z"/>',
  arrow: '<path d="M4 10h11M11 6l4 4-4 4"/>',
  plus: '<path d="M10 4v12M4 10h12"/>',
  trash: '<path d="M4 6h12M8 6V4h4v2M6 6l.8 11h6.4L14 6"/>',
  check: '<path d="M4 10.5 8 14.5 16 5.5"/>',
  refresh: '<path d="M16.5 8A7 7 0 0 0 4 6.5"/><path d="M16.5 3.5V8h-4.2"/><path d="M3.5 12A7 7 0 0 0 16 13.5"/><path d="M3.5 16.5V12h4.2"/>',
  search: '<circle cx="9" cy="9" r="5.5"/><path d="M13.2 13.2 17 17"/>',
  book: '<path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H16v14H5.5A1.5 1.5 0 0 0 4 18.5z"/><path d="M4 4.5v14"/>',
  lock: '<rect x="4.5" y="8.5" width="11" height="8" rx="2"/><path d="M7.2 8.5V6.8a2.8 2.8 0 0 1 5.6 0v1.7"/>',
  clock: '<circle cx="10" cy="10" r="7"/><path d="M10 6v4.3l3 1.7"/>',
  warn: '<path d="M10 3.5 17.5 17h-15z"/><path d="M10 8v4M10 14.4v.2"/>',
  layers: '<path d="M10 3 17 7l-7 4-7-4z"/><path d="m3 11 7 4 7-4M3 14.5l7 4 7-4"/>',
  file: '<path d="M5 3.5h7L15.5 7v9.5H5z"/><path d="M11.8 3.6V7h3.4"/>',
  logout: '<path d="M7.5 17H4.5A1.5 1.5 0 0 1 3 15.5v-11A1.5 1.5 0 0 1 4.5 3h3"/><path d="M12 6.5 15.5 10 12 13.5"/><path d="M15.5 10H7.5"/>',
};

export function icon(name, size = 17) {
  const path = ICONS[name] || ICONS.sparkle;
  return `<svg viewBox="0 0 20 20" width="${size}" height="${size}" fill="none"
    stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true">${path}</svg>`;
}

/* ---------------------------------------------------------------- 空态 */
export function emptyState(title, desc, iconName = "sparkle") {
  return `<div class="empty">
    <div class="glyph">${icon(iconName, 21)}</div>
    <h4>${esc(title)}</h4>
    ${desc ? `<p>${esc(desc)}</p>` : ""}
  </div>`;
}

export function errorBox(message) {
  return `<div class="alert alert-error"><span class="dot"></span><div>${esc(message)}</div></div>`;
}

/* 视图渲染失败时统一兜底，避免白屏 */
export function guard(root, fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return;
      root.innerHTML = `<div class="container">${errorBox(err.detail || err.message || "页面加载失败")}</div>`;
    }
  };
}
