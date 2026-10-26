/* ============================================================================
 * shell.js · 应用外壳（顶栏 + 左侧导航）+ 角色路由规则
 *
 * 角色规则（对齐 PRD 二、用户角色与权限）：
 *   student  → 学生端
 *   teacher  → 老师端（默认）
 *   researcher → 教研端
 *   admin    → 管理后台（最高优先）
 *   一人多角色：默认落在「老师端」，左侧「工作台」列出全部可进入的端，
 *              教研端入口始终可见，不必退出重登。
 * ========================================================================== */
import { icon, esc, ROLE_LABEL } from "./core.js";

export const NAV = {
  student: [
    { tab: "overview", label: "概览", ico: "home" },
    { tab: "exam", label: "考试", ico: "file" },
    { tab: "mastery", label: "掌握度", ico: "chart" },
    { tab: "assistant", label: "学习助手", ico: "chat" },
    { tab: "quiz", label: "诊断测验", ico: "target" },
  ],
  teacher: [
    { tab: "overview", label: "概览", ico: "home" },
    { tab: "grading", label: "待批改", ico: "file" },
    { tab: "homework", label: "布置作业", ico: "book" },
    { tab: "class", label: "班级学情", ico: "chart" },
  ],
  research: [
    { tab: "overview", label: "年级学情", ico: "chart" },
    { tab: "compare", label: "班级对比", ico: "layers" },
    { tab: "exam", label: "智能出卷", ico: "file" },
    { tab: "analysis", label: "考后分析", ico: "sparkle" },
  ],
  admin: [
    { tab: "overview", label: "概览", ico: "home" },
    { tab: "import", label: "账户导入", ico: "upload" },
    { tab: "users", label: "用户管理", ico: "users" },
    { tab: "teaching", label: "授课关系", ico: "link" },
    { tab: "leads", label: "教研任命", ico: "medal" },
    { tab: "classes", label: "班级管理", ico: "building" },
    { tab: "logs", label: "审计日志", ico: "log" },
  ],
};

const ROLE_OF = { student: "student", teacher: "teacher", research: "researcher", admin: "admin" };
const ROLE_CN = { student: "学生", teacher: "老师", researcher: "教研组长", admin: "管理员" };
const BENCH_SHORT = { admin: "管理后台", teacher: "老师端", research: "教研端", student: "学生端" };

/** 工作台展示顺序：老师端始终在前，双角色账号一眼看到主战场 */
const BENCH_ORDER = ["teacher", "research", "admin", "student"];

/** 当前账号可以进入哪些工作台（顺序即左侧展示顺序） */
export function workbenches(me) {
  const roles = me?.roles || [];
  const list = [];
  if (roles.includes("teacher")) list.push("teacher");
  if (roles.includes("researcher")) list.push("research");
  if (roles.includes("admin")) list.push("admin");
  if (roles.includes("student")) list.push("student");
  return list;
}

/**
 * 登录后默认落地页（对齐需求）：
 *   单角色 → 该角色自己的端（admin 即管理后台）
 *   双/多角色 → 一律默认老师端（没有 teacher 才顺延 教研 → 管理 → 学生）
 */
export function defaultRoute(me) {
  const list = workbenches(me);
  if (!list.length) return "#/login";
  const role = list.length > 1
    ? BENCH_ORDER.find((r) => list.includes(r))
    : list[0];
  return `#/${role}/overview`;
}

export function canEnter(me, role) {
  const need = ROLE_OF[role];
  return Boolean(need && (me?.roles || []).includes(need));
}

export function firstTab(role) {
  return NAV[role][0].tab;
}

export function navLabel(role, tab) {
  return NAV[role]?.find((n) => n.tab === tab)?.label || "";
}

/* ---------------------------------------------------------------- 外壳 HTML */
export function shellHTML(me, role, tab) {
  const benches = workbenches(me);
  const nav = NAV[role] || [];
  const initials = String(me?.name || me?.username || "?").slice(0, 1);

  const benchGroup = benches.length > 1
    ? `<div>
        <div class="side-label">工作台</div>
        <div class="workbench-switch">
          ${benches.map((r) => `
            <a class="${r === role ? "active" : ""}" href="#/${r}/${firstTab(r)}"
               ${r === role ? 'aria-current="true"' : ""}>${esc(BENCH_SHORT[r])}</a>`).join("")}
        </div>
      </div>`
    : "";

  return `
    <header class="topbar">
      <a class="brand" href="#/${role}/${firstTab(role)}">
        <span class="brand-mark">
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M10 3.2 16.4 16H3.6Z" fill="rgba(255,255,255,.95)"/>
            <path d="M10 5.6 13.7 13H6.3Z" fill="rgba(255,255,255,.45)"/>
          </svg>
        </span>
        <span class="brand-name">HHT 棱镜课堂</span>
        <span class="brand-sep"></span>
        <span class="brand-role">${esc(ROLE_LABEL[ROLE_OF[role]])}</span>
      </a>
      <div class="topbar-right">
        <div class="user-chip">
          <span class="avatar">${esc(initials)}</span>
          <span>${esc(me?.name || me?.username)}</span>
        </div>
        <button class="btn btn-sm btn-secondary" id="logout-btn" title="退出登录">退出</button>
      </div>
    </header>
    <div class="shell">
      <nav class="sidebar" aria-label="主导航">
        ${benchGroup}
        <div class="side-group">
          <div class="side-label">导航</div>
          ${nav.map((n) => `
            <a class="side-item ${n.tab === tab ? "active" : ""}" href="#/${role}/${n.tab}"
               ${n.tab === tab ? 'aria-current="page"' : ""}>
              <span class="ico">${icon(n.ico, 16)}</span>
              <span>${esc(n.label)}</span>
            </a>`).join("")}
        </div>
        <div class="sidebar-foot">
          <div class="who">
            ${esc(me?.username)}<br/>
            <span style="color:#a1a1a6">${(me?.roles || []).map((r) => esc(ROLE_CN[r] || r)).join(" · ")}</span>
          </div>
        </div>
      </nav>
      <main class="main"><div class="container" id="view"></div></main>
    </div>`;
}
