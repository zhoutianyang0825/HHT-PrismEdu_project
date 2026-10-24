/* ============================================================================
 * app.js · 路由与启动
 *   #/login                    登录页（未登录时唯一入口）
 *   #/student/<tab>            学生端   overview | assistant | quiz | mastery
 *   #/teacher/<tab>            老师端   overview | grading | homework | class
 *   #/research/<tab>           教研端   overview | compare | exam | analysis
 *   #/admin/<tab>              管理后台 overview | import | users | teaching | leads | classes | logs
 * ========================================================================== */
import { session, toast, errorBox } from "./core.js";
import { shellHTML, canEnter, defaultRoute, firstTab, NAV } from "./shell.js";
import { renderLogin } from "./views/login.js";
import * as student from "./views/student.js";
import * as teacher from "./views/teacher.js";
import * as research from "./views/research.js";
import * as admin from "./views/admin.js";

const VIEWS = { student, teacher, research, admin };
const app = document.getElementById("app");

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [role = "", tab = ""] = raw.split("/");
  return { role, tab };
}

function go(hash, replace = false) {
  if (location.hash === hash) return route();
  if (replace) location.replace(hash);
  else location.hash = hash;
}

let routing = false;
let reroute = false;

async function route() {
  /* 上一页还在拉数据时又发生了跳转：不丢弃，标记后等本轮结束立刻重跑，
     否则会出现「hash 变了、页面没反应」的假死。 */
  if (routing) { reroute = true; return; }
  routing = true;
  try {
    const me = session.me;
    const { role, tab } = parseHash();

    /* ---- 未登录：只有登录页，地址栏也收回 #/login（避免露出受保护路由）---- */
    if (!me) {
      renderLogin(app, { me: null });
      document.title = "登录 · HHT 棱镜课堂";
      if (location.hash !== "#/login") history.replaceState(null, "", "#/login");
      return;
    }

    /* ---- 已登录但路由非法：落到该账号的默认端 ---- */
    if (!role || role === "login" || !canEnter(me, role)) {
      go(defaultRoute(me), true);
      return;
    }

    const validTab = NAV[role].some((n) => n.tab === tab) ? tab : firstTab(role);
    if (validTab !== tab) { go(`#/${role}/${validTab}`, true); return; }

    app.innerHTML = shellHTML(me, role, validTab);
    document.title = `${NAV[role].find((n) => n.tab === validTab).label} · HHT 棱镜课堂`;

    app.querySelector("#logout-btn")?.addEventListener("click", () => {
      session.clear();
      toast("已退出登录");
      go("#/login");
    });

    const view = app.querySelector("#view");
    if (role === "admin" || (role === "student" && validTab === "assistant")) {
      view.classList.remove("container");
      view.classList.add("container-wide");
    }

    window.scrollTo({ top: 0 });
    try {
      await VIEWS[role].render(view, { me, role, tab: validTab });
    } catch (err) {
      if (err?.status === 401) return;
      view.innerHTML = errorBox(err?.detail || err?.message || "页面加载失败");
    }
  } finally {
    routing = false;
    if (reroute) { reroute = false; route(); }
  }
}

window.addEventListener("hashchange", route);

/* 另一个标签页退出登录时，本页同步回登录 */
window.addEventListener("storage", (e) => {
  if (e.key === "prism.token" && !e.newValue) route();
});

/* ---------------------------------------------------------------------------
 * 版本守望：标签页开着时源码被改了，页面里跑的还是旧模块。
 * 现象是「明明改了，怎么还是老样子」，很容易误判成 bug。
 * serve.py 提供 /__version（所有源文件最新的 mtime）；切回该标签页时对一次，
 * 变了就提示刷新。用 python3 -m http.server 时该端点 404，静默跳过。
 * ------------------------------------------------------------------------- */
let bootVersion = null;

async function checkVersion() {
  try {
    const res = await fetch("./__version", { cache: "no-store" });
    if (!res.ok) return;
    const { v } = await res.json();
    if (bootVersion === null) { bootVersion = v; return; }
    if (v !== bootVersion) {
      bootVersion = v;
      toast("前端文件已更新，按 ⌘⇧R 刷新才会生效", "err");
    }
  } catch { /* 没有该端点：不是 serve.py 起的，忽略 */ }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) checkVersion();
});
checkVersion();

if (!location.hash) location.replace(`#/login`);
route();
