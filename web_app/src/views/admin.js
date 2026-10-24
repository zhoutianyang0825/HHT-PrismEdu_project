/* ============================================================================
 * views/admin.js · 管理后台 A-01（对齐 app/api/admin.py）
 *   概览    GET /users · /classes · /teaching-assignments · /research-leads · /audit-logs
 *   导入    GET /users/import-template · POST /users/import   （逐行校验，全过才写）
 *   用户    GET /users · POST /users · PUT /users/{id}
 *   授课    GET/POST/DELETE /teaching-assignments
 *   任命    GET/POST/DELETE /research-leads
 *   班级    GET/POST/PUT/DELETE /classes
 *   日志    GET /audit-logs
 *
 * 这里的每一项都是权限模型的数据源：授课关系决定老师能看哪些班，
 * 教研任命决定组长能看哪个年级，停用而非删除保住历史作答。
 * ========================================================================== */
import {
  http, API_BASE, session, toast, esc, busy, dateOf, emptyState, errorBox, modal, closeModal, clip, icon,
} from "../core.js";

const A = {
  users: null,
  page: 1,
  keyword: "",
  roleFilter: "",
  classes: null,
  tas: null,
  leads: null,
  logs: null,
  logAction: "",
};

const ROLES = [
  ["student", "学生"],
  ["teacher", "科任老师"],
  ["researcher", "教研组长"],
  ["admin", "管理员"],
];
const GRADES = ["初一", "初二", "初三"];
const ROLE_CN = Object.fromEntries(ROLES);

/* 学科下拉（单选）。值用英文键，和库里 subject 字段一致（math/chinese/...），
   标签用中文。初中全科先给到十门，题库扩容时照这个口径加即可（PRD E-02）。 */
const SUBJECTS = [
  ["math", "数学"], ["chinese", "语文"], ["english", "英语"],
  ["physics", "物理"], ["chemistry", "化学"], ["biology", "生物"],
  ["history", "历史"], ["geography", "地理"], ["politics", "道德与法治"], ["it", "信息技术"],
];

const PAGE_SIZE = 10;   // 用户列表每页条数：请求 size 与页数计算同源，防"第二页空白"

/* 合法角色组合（5 种，与后端 VALID_ROLE_COMBOS 一一对应）：
 * {学生} {管理员} {科任老师} {教研组长} {科任老师+教研组长}。
 * 这张表用来做"提交前合法性校验"。 */
const VALID_ROLE_COMBOS = [
  ["student"], ["admin"], ["teacher"], ["researcher"], ["teacher", "researcher"],
];

/* 点选某角色时允许与它共存的角色：key=点选的角色，value=能与它同行的角色集合。
 * 这张表只管"点选时自动收敛"，不要拿它当合法组合表用（那会漏掉单角色组合）。 */
const ROLE_COMBO = {
  student: ["student"],
  admin: ["admin"],
  teacher: ["teacher", "researcher"],
  researcher: ["researcher", "teacher"],
};
function roleComboOk(roles) {
  const key = [...roles].sort().join("+");
  return VALID_ROLE_COMBOS.some((c) => [...c].sort().join("+") === key);
}

/* ---------------------------------------------------------------- 入口 */
export async function render(view, ctx) {
  if (ctx.tab === "import") return renderImport(view, ctx);
  if (ctx.tab === "users") return renderUsers(view, ctx);
  if (ctx.tab === "teaching") return renderTeaching(view, ctx);
  if (ctx.tab === "leads") return renderLeads(view, ctx);
  if (ctx.tab === "classes") return renderClasses(view, ctx);
  if (ctx.tab === "logs") return renderLogs(view, ctx);
  return renderOverview(view, ctx);
}

async function loadClasses(force = false) {
  if (!A.classes || force) A.classes = (await http.get("/api/admin/classes")) || [];
  return A.classes;
}

async function loadUsersByRole(role) {
  const data = await http.get("/api/admin/users", { role, size: 100, page: 1 });
  return data.items || [];
}

/* ---------------------------------------------------------------- 概览 */
async function renderOverview(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">管理后台</div>
    <h1 class="page-title">正在汇总组织数据…</h1></div>
    <div class="grid grid-4">${'<div class="skeleton" style="height:118px"></div>'.repeat(4)}</div>`;

  const [users, classes, tas, leads, logs] = await Promise.all([
    http.get("/api/admin/users", { page: 1, size: 1 }),
    http.get("/api/admin/classes"),
    http.get("/api/admin/teaching-assignments"),
    http.get("/api/admin/research-leads"),
    http.get("/api/admin/audit-logs", { page: 1, size: 6 }),
  ]);
  A.classes = classes || [];
  A.tas = tas || [];
  A.leads = leads || [];
  A.logs = logs || [];

  const counts = { teacher: 0, student: 0, researcher: 0 };
  await Promise.all(Object.keys(counts).map(async (role) => {
    const d = await http.get("/api/admin/users", { role, size: 1, page: 1 });
    counts[role] = d.total || 0;
  }));

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">管理后台 · A-01</div>
      <h1 class="page-title">权限的地基，都在这几页</h1>
      <p class="page-sub">账号与授课关系是全部权限隔离的数据源：改完即时生效，不需要重新登录。</p>
    </div>

    <div class="grid grid-4" style="margin-bottom:24px">
      <div class="card stat">
        <div class="stat-label">账号总数</div>
        <div class="stat-value">${users.total}<small>人</small></div>
        <div class="stat-foot">学生 ${counts.student} · 老师 ${counts.teacher} · 组长 ${counts.researcher}</div>
      </div>
      <div class="card stat">
        <div class="stat-label">班级</div>
        <div class="stat-value">${A.classes.length}<small>个</small></div>
        <div class="stat-foot">建用户前先有班级</div>
      </div>
      <div class="card stat">
        <div class="stat-label">授课关系</div>
        <div class="stat-value">${A.tas.length}<small>条</small></div>
        <div class="stat-foot">teacher ↔ 班级 ↔ 学科</div>
      </div>
      <div class="card stat">
        <div class="stat-label">教研任命</div>
        <div class="stat-value">${A.leads.length}<small>条</small></div>
        <div class="stat-foot">user ↔ 年级 ↔ 学科</div>
      </div>
    </div>

    <div class="grid grid-2" style="align-items:start">
      <section class="card card-pad">
        <div class="card-title">常用操作</div>
        <div class="card-sub">开学季最高频的四件事</div>
        <div class="list" style="margin-top:10px">
          ${[
            ["账户导入", "CSV 大宽表，逐行校验、失败行不写库", "#/admin/import", "upload"],
            ["用户管理", "单条增删改查、停用、重置密码", "#/admin/users", "users"],
            ["授课关系", "决定老师能看到哪些班的数据", "#/admin/teaching", "link"],
            ["教研任命", "勾 researcher 必须先有任命", "#/admin/leads", "medal"],
          ].map(([t, d, href]) => `
            <a class="list-row" href="${href}" style="text-decoration:none;color:inherit">
              <div class="grow"><div class="title">${t}</div><div class="meta">${d}</div></div>
              <span class="btn btn-sm btn-secondary">进入</span>
            </a>`).join("")}
        </div>
      </section>

      <section class="card card-pad">
        <div style="display:flex;align-items:baseline;justify-content:space-between">
          <div><div class="card-title">最近操作</div><div class="card-sub">审计日志最新 6 条</div></div>
          <a class="btn btn-ghost btn-sm" href="#/admin/logs">全部</a>
        </div>
        <div class="list" style="margin-top:10px">
          ${A.logs.length ? A.logs.map((l) => `
            <div class="list-row">
              <span class="pill">${esc(l.action)}</span>
              <div class="grow">
                <div class="title" style="font-size:13px">${esc(l.target || "—")}</div>
                <div class="meta">用户 #${l.user_id} · ${dateOf(l.created_at)}</div>
              </div>
            </div>`).join("") : emptyState("暂无日志", "导入、改授课关系、批改确认都会留痕", "log")}
        </div>
      </section>
    </div>`;
}

/* ---------------------------------------------------------------- CSV 导入 */
function renderImport(view, ctx) {
  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">账户导入 · A-01</div>
      <h1 class="page-title">一次导入，四张表同时就位</h1>
      <p class="page-sub">逐行校验，全部通过才写库；任何一行不合格，整批一条都不进。失败报告带行号与原因。</p>
    </div>

    <div class="card card-pad" style="margin-bottom:16px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <div class="card-title">CSV 大宽表</div>
        <span class="pill pill-blue">7 列</span>
        <span class="spacer" style="flex:1"></span>
        <button class="btn btn-sm btn-secondary" id="tpl-btn">下载模板</button>
      </div>
      <div class="table-wrap" style="margin-top:14px">
        <table class="table">
          <thead><tr><th>列</th><th>必填</th><th>说明</th></tr></thead>
          <tbody>
            <tr><td>username</td><td><span class="pill">必填</span></td><td>登录名，≥2 字符且无空格，全局唯一</td></tr>
            <tr><td>password</td><td><span class="pill">必填</span></td><td>≥8 位，含字母与数字</td></tr>
            <tr><td>name</td><td><span class="pill">必填</span></td><td>真实姓名</td></tr>
            <tr><td>roles</td><td><span class="pill">必填</span></td><td>分号分隔，多角色如 <code>teacher;researcher</code>；student / admin 只能单独成角</td></tr>
            <tr><td>classes</td><td><span class="pill">按角色</span></td><td>年级:班名，多值分号分隔；student 必填，班不存在自动建</td></tr>
            <tr><td>student_no</td><td><span class="pill">可选</span></td><td>学籍号，仅 student 使用</td></tr>
            <tr><td>lead_grades</td><td><span class="pill">按角色</span></td><td>任命年级，researcher 必填，学科固定 math</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <section class="card card-pad">
      <div class="card-title">CSV 格式文件导入</div>
      <div class="card-sub">选择 .csv 文件，首行必须是表头</div>
      <label class="file-drop" for="csv-file" id="csv-drop">
        <input type="file" id="csv-file" accept=".csv,text/csv" hidden />
        ${icon("upload", 22)}
        <div class="grow">
          <div class="title" id="csv-file-name">点击选择 CSV 文件</div>
          <div class="meta">导入在浏览器本地读取文件内容，不会上传文件本身</div>
        </div>
      </label>
      <div class="form-actions">
        <button class="btn btn-primary" id="import-btn">开始导入</button>
        <span class="tiny" id="import-tip">坏行信息会拒绝整批文件信息导入，先在小文件上验证</span>
      </div>
      <div id="import-result" style="margin-top:18px"></div>
    </section>`;

  view.querySelector("#tpl-btn").onclick = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/admin/users/import-template`, {
        headers: { Authorization: `Bearer ${session.token}` },
      });
      if (!res.ok) throw new Error("模板获取失败");
      const text = await res.text();
      const url = URL.createObjectURL(new Blob([`\ufeff${text}`], { type: "text/csv;charset=utf-8" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = "users_import_template.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      toast("模板已开始下载", "ok");
    } catch (err) {
      toast(err.message || "模板获取失败", "err");
    }
  };

  const fileInput = view.querySelector("#csv-file");
  fileInput.onchange = () => {
    const f = fileInput.files[0];
    view.querySelector("#csv-file-name").textContent = f ? f.name : "点击选择 CSV 文件";
    view.querySelector("#csv-drop").classList.toggle("has-file", Boolean(f));
  };

  view.querySelector("#import-btn").onclick = async () => {
    const btn = view.querySelector("#import-btn");
    const box = view.querySelector("#import-result");
    const f = fileInput.files[0];
    if (!f) { toast("请先选择 CSV 文件", "err"); return; }

    const restore = busy(btn, "校验并写入…");
    try {
      const text = (await f.text()).trim();
      if (!text) { toast("文件内容为空", "err"); restore(); return; }
      const out = await http.post("/api/admin/users/import", { csv_content: text });
      box.innerHTML = importResultHTML(out);
      box.scrollIntoView({ behavior: "smooth", block: "nearest" });
      if (out.ok) toast(`成功导入 ${out.imported} 人`, "ok");
      else toast("整批未导入，请看失败报告", "err");
    } catch (err) {
      box.innerHTML = errorBox(err.detail || "导入失败");
    } finally {
      restore();
    }
  };
}

function importResultHTML(out) {
  if (!out.ok) {
    return `<div class="alert alert-error"><span class="dot"></span>
        <div><b>整批未导入</b>：${esc(out.message || "")}</div></div>
      <div class="table-wrap" style="margin-top:12px">
        <table class="table">
          <thead><tr><th>行号</th><th>原因</th></tr></thead>
          <tbody>${(out.errors || []).map((e) => `<tr>
            <td>第 ${e.line} 行</td><td>${esc(e.reason)}</td></tr>`).join("")}</tbody>
        </table>
      </div>`;
  }
  return `<div class="alert alert-ok"><span class="dot"></span>
      <div><b>导入成功</b>：${out.imported} 个账号 ·
      学生档案 ${out.students || 0} · 任教关系 ${out.teaching_assignments || 0} ·
      教研任命 ${out.research_leads || 0}</div></div>
    <div class="table-wrap" style="margin-top:12px">
      <table class="table">
        <thead><tr><th>id</th><th>用户名</th><th>角色</th></tr></thead>
        <tbody>${(out.items || []).map((u) => `<tr>
          <td>#${u.seq ?? u.id}</td><td>${esc(u.username)}</td>
          <td>${u.roles.map((r) => `<span class="pill">${esc(r)}</span>`).join(" ")}</td></tr>`).join("")}</tbody>
      </table>
    </div>`;
}

/* ---------------------------------------------------------------- 用户管理 */
async function renderUsers(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">用户管理 · A-01</div>
    <h1 class="page-title">正在取用户列表…</h1></div><div class="skeleton" style="height:300px"></div>`;

  const data = await http.get("/api/admin/users", {
    keyword: A.keyword || undefined,
    role: A.roleFilter || undefined,
    page: A.page,
    size: PAGE_SIZE,
  });
  A.users = data;

  const total = data.total || 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (A.page > pages) { A.page = pages; return renderUsers(view, ctx); }  // 删完越界防停在空页

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">用户管理 · A-01</div>
      <h1 class="page-title">账号与角色</h1>
      <p class="page-sub">一人多角色用 user_roles 多对多保存；停用而非删除，保住历史作答数据。</p>
    </div>

    <div class="card">
      <div class="card-pad" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding-bottom:8px">
        <input class="input" id="u-kw" placeholder="搜索用户名 / 姓名" style="width:220px"
          value="${esc(A.keyword)}" />
        <select class="select" id="u-role" style="width:150px">
          <option value="">全部角色</option>
          ${ROLES.map(([v, l]) => `<option value="${v}" ${A.roleFilter === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
        <button class="btn btn-secondary" id="u-search">搜索</button>
        <span class="spacer" style="flex:1"></span>
        <span class="tiny">共 ${total} 人</span>
        <button class="btn btn-primary" id="u-new">新建用户</button>
      </div>

      <div class="table-wrap">
        <table class="table">
          <thead><tr><th>编号</th><th>id</th><th>用户名</th><th>姓名</th><th>角色</th><th>班级</th><th>状态</th><th>创建时间</th><th></th></tr></thead>
          <tbody>
            ${(data.items || []).map((u, i) => `
              <tr>
                <td class="muted">${(A.page - 1) * PAGE_SIZE + i + 1}</td>
                <td class="muted">#${u.id}</td>
                <td>${esc(u.username)}</td>
                <td>${esc(u.name)}</td>
                <td>${(u.roles || []).map((r) => `<span class="pill ${r === "admin" ? "pill-dark" : ""}">${esc(ROLE_CN[r] || r)}</span>`).join(" ")}</td>
                <td class="tiny">${(u.class_names || []).map((c) => esc(c)).join("、") || "—"}</td>
                <td>${u.is_active ? '<span class="pill pill-green">启用</span>' : '<span class="pill pill-red">停用</span>'}</td>
                <td class="tiny">${dateOf(u.created_at)}</td>
                <td style="text-align:right">
                  <button class="btn btn-sm btn-secondary" data-edit="${u.id}">编辑</button>
                  ${u.id === (session.me?.user_id ?? -1) ? "" : `
                    <button class="btn btn-sm ${u.is_active ? "btn-danger" : "btn-secondary"}" data-toggle="${u.id}">
                      ${u.is_active ? "停用" : "启用"}</button>`}
                  ${!(u.roles || []).includes("admin") && u.id !== (session.me?.user_id ?? -1)
                    ? `<button class="btn btn-sm btn-danger" data-del-user="${u.id}">删除</button>` : ""}
                </td>
              </tr>`).join("") || `<tr><td colspan="9" class="muted" style="padding:30px;text-align:center">没有匹配的账号</td></tr>`}
          </tbody>
        </table>
      </div>

      <div class="card-pad" style="display:flex;gap:10px;align-items:center;justify-content:flex-end">
        <button class="btn btn-sm btn-secondary" id="u-prev" ${A.page <= 1 ? "disabled" : ""}>上一页</button>
        <span class="tiny">第 ${A.page} / ${pages} 页</span>
        <button class="btn btn-sm btn-secondary" id="u-next" ${A.page >= pages ? "disabled" : ""}>下一页</button>
      </div>
    </div>`;

  view.querySelector("#u-search").onclick = () => {
    A.keyword = view.querySelector("#u-kw").value.trim();
    A.roleFilter = view.querySelector("#u-role").value;
    A.page = 1;
    renderUsers(view, ctx);
  };
  view.querySelector("#u-kw").onkeydown = (e) => {
    if (e.key === "Enter") view.querySelector("#u-search").click();
  };
  view.querySelector("#u-role").onchange = () => view.querySelector("#u-search").click();
  view.querySelector("#u-prev").onclick = () => { A.page -= 1; renderUsers(view, ctx); };
  view.querySelector("#u-next").onclick = () => { A.page += 1; renderUsers(view, ctx); };
  view.querySelector("#u-new").onclick = () => openUserModal(null, view, ctx);

  view.querySelectorAll("[data-edit]").forEach((btn) => {
    btn.onclick = () => {
      const u = (A.users.items || []).find((x) => x.id === Number(btn.dataset.edit));
      openUserModal(u, view, ctx);
    };
  });
  view.querySelectorAll("[data-toggle]").forEach((btn) => {
    btn.onclick = async () => {
      const id = Number(btn.dataset.toggle);
      const u = (A.users.items || []).find((x) => x.id === id);
      const restore = busy(btn, "…");
      try {
        await http.put(`/api/admin/users/${id}`, { is_active: !u.is_active });
        toast(u.is_active ? "账户已停用" : "账户已启用", "ok");
        renderUsers(view, ctx);
      } catch (err) {
        restore();
        toast(err.detail || "操作失败", "err");
      }
    };
  });

  view.querySelectorAll("[data-del-user]").forEach((btn) => {
    btn.onclick = () => {
      const id = Number(btn.dataset.delUser);
      const u = (A.users.items || []).find((x) => x.id === id);
      const runDelete = async (confirmFlag) => {
        const restore = busy(btn, "…");
        try {
          await http.del(`/api/admin/users/${id}${confirmFlag ? "?confirm=1" : ""}`);
          toast(`账号 ${u?.username || ""} 已删除`, "ok");
          renderUsers(view, ctx);
        } catch (err) {
          restore();
          if (err.status === 409 && !confirmFlag) {
            // 有历史数据（作答/作业）→ 弹窗确认，确定才连带删干净，取消则不动
            modal({
              title: "确认删除",
              sub: "删除后不可恢复",
              body: `<div class="alert alert-warn"><span class="dot"></span>
                <div>${esc(err.detail || "该账户下有作答数据，是否确定删除？")}</div></div>`,
              actions: [
                { label: "取消" },
                { label: "确定删除", kind: "danger", onClick: () => { runDelete(true); } },
              ],
            });
            return;
          }
          toast(err.detail || "删除失败", "err");
        }
      };
      runDelete(false);
    };
  });
}

async function openUserModal(user, view, ctx) {
  const isEdit = Boolean(user);
  const selectedRoles = new Set(user?.roles || ["student"]);
  const classes = await loadClasses();   // 班级下拉数据（带缓存；建用户前先有班级）
  const teachSet = new Set((user?.teaching || []).map((t) => t.class_id));   // 任教班级预选
  const teachSubject = user?.teaching?.[0]?.subject || "math";

  const body = document.createElement("div");
  body.className = "user-form";   // 弹窗内字段间距（apple.css 里 .user-form 规则）

  body.innerHTML = `
    ${isEdit ? "" : `
      <div class="form-row">
        <div class="field"><label for="m-username">用户名</label>
          <input class="input" id="m-username" placeholder="如 S201"
            autocomplete="off" autocapitalize="off" spellcheck="false" readonly data-unlock /></div>
        <div class="field"><label for="m-password">初始密码</label>
          <input class="input" id="m-password" type="password" placeholder="≥8 位含字母数字"
            autocomplete="new-password" readonly data-unlock /></div>
      </div>`}
    <div class="field"><label for="m-name">姓名</label>
      <input class="input" id="m-name" value="${esc(user?.name || "")}" /></div>

    <div class="field">
      <label>角色（可多选）</label>
      <div class="chip-set" id="m-roles">
        ${ROLES.map(([v, l]) => `<button type="button" class="chip ${selectedRoles.has(v) ? "active" : ""}"
          data-role="${v}">${l}</button>`).join("")}
      </div>
    </div>

    
    <div id="m-class" class="m-block${selectedRoles.has("student") ? " on" : ""}">
      <div class="field"><label for="m-class-sel">就读班级</label>
        <select class="select" id="m-class-sel">
          ${classes.map((c) => `<option value="${c.id}" ${c.id === user?.class_id ? "selected" : ""}>${esc(c.grade)}${esc(c.name)}</option>`).join("")
            || '<option value="">还没有班级，先去「班级管理」建班</option>'}
        </select>
        <div class="hint" style="margin-top:6px">学生必须有班（与 CSV 导入同规则）；换班即迁移。</div>
      </div>
    </div>

    <div id="m-teach" class="m-block${selectedRoles.has("teacher") ? " on" : ""}">
      <div class="field"><label>任教班级（可多选）</label>
        <div class="chip-set" id="m-teach-classes">
          ${classes.map((c) => `<button type="button" class="chip ${teachSet.has(c.id) ? "active" : ""}"
            data-cls="${c.id}">${esc(c.grade)}${esc(c.name)}</button>`).join("")
            || '<span class="hint">还没有班级，先去「班级管理」建班</span>'}
        </div>
      </div>
      <div class="field"><label for="m-teach-subject">任教班级学科（单选）</label>
        <select class="select" id="m-teach-subject">
          ${SUBJECTS.some(([v]) => v === teachSubject) ? "" :
            `<option value="${esc(teachSubject)}" selected>${esc(teachSubject)}</option>`}
          ${SUBJECTS.map(([v, l]) => `<option value="${v}" ${v === teachSubject ? "selected" : ""}>${l}（${v}）</option>`).join("")}
        </select></div>
      <div class="hint" style="margin-top:6px">保存即整体替换该账号的授课关系；按班不同学科请到「授课关系」页逐条改。</div>
    </div>

    <div id="m-lead" class="m-block${selectedRoles.has("researcher") ? " on" : ""}">
      <div class="form-row">
        <div class="field"><label for="m-grade">任命年级</label>
          <select class="select" id="m-grade">
            ${GRADES.map((g) => `<option>${g}</option>`).join("")}
          </select></div>
        <div class="field"><label for="m-subject">任命学科</label>
          <input class="input" id="m-subject" value="math" /></div>
      </div>
      <div class="hint" style="margin-top:6px">勾选 researcher 必须给任命，否则该账号无效。</div>
    </div>

    ${isEdit ? `
      ${user.id === (session.me?.user_id ?? -1) ? `
        <div class="field"><label>账号状态</label>
          <div class="hint">当前登录账号，不能停用自己</div></div>
      ` : `
        <div class="field"><label>账号状态</label>
          <div class="segmented" id="m-active">
            <button type="button" data-v="1" class="${user.is_active ? "active" : ""}">启用</button>
            <button type="button" data-v="0" class="${user.is_active ? "" : "active"}">停用</button>
          </div>
        </div>
      `}
      <div class="field"><label for="m-reset">重置密码（留空不改）</label>
        <input class="input" id="m-reset" type="password" placeholder="≥8 位含字母数字"
          autocomplete="new-password" readonly data-unlock /></div>` : ""}
  `;

  modal({
    title: isEdit ? `编辑` : "新建用户",
    sub: isEdit ? "修改信息" : "角色在创建时指定：学生 / 管理员单独成角，科任老师与教研组长可并存",
    body,
    actions: [{ label: "取消" }, {
      label: isEdit ? "保存修改" : "创建账号",
      kind: "primary",
      onClick: async () => {
        const roles = [...body.querySelectorAll("#m-roles .chip.active")].map((c) => c.dataset.role);
        if (!roles.length) { toast("至少选择一个角色", "err"); return false; }
        if (!roleComboOk(roles)) {
          toast("角色组合不合法：学生 / 管理员只能单独成角，科任老师与教研组长可并存", "err");
          return false;
        }
        const leadBox = body.querySelector("#m-lead");
        const needLead = roles.includes("researcher");
        const payload = { name: body.querySelector("#m-name").value.trim() };

        if (!isEdit) {
          payload.username = body.querySelector("#m-username").value.trim();
          payload.password = body.querySelector("#m-password").value;
          if (payload.username.length < 2) { toast("用户名至少 2 个字符", "err"); return false; }
          if (payload.password.length < 8) { toast("密码至少 8 位", "err"); return false; }
        } else {
          const activeSeg = body.querySelector("#m-active");
          if (activeSeg) payload.is_active = activeSeg.querySelector(".active")?.dataset.v === "1";
          const reset = body.querySelector("#m-reset").value;
          if (reset) payload.password = reset;
        }

        payload.roles = roles;
        if (roles.includes("student")) {
          const clsId = Number(body.querySelector("#m-class-sel").value);
          if (!clsId) { toast("学生账号请选择就读班级", "err"); return false; }
          payload.class_id = clsId;   // 后端据此落 students 档案 / 迁移班级
        }
        if (roles.includes("teacher")) {
          // 任教班级多选 → [{class_id, subject}]，后端整体替换授课关系
          const subj = body.querySelector("#m-teach-subject").value.trim() || "math";
          payload.teaching = [...body.querySelectorAll("#m-teach-classes .chip.active")]
            .map((c) => ({ class_id: Number(c.dataset.cls), subject: subj }));
        } else if (isEdit) {
          payload.teaching = [];   // 没了 teacher 角色 → 清空授课关系
        }
        if (needLead) {
          payload.research_leads = [{
            grade: body.querySelector("#m-grade").value,
            subject: body.querySelector("#m-subject").value.trim() || "math",
          }];
        } else if (isEdit) {
          payload.research_leads = [];
        }

        try {
          if (isEdit) await http.put(`/api/admin/users/${user.id}`, payload);
          else await http.post("/api/admin/users", payload);
          toast(isEdit ? "已保存" : "账号已创建", "ok");
          closeModal();
          renderUsers(view, ctx);
        } catch (err) {
          toast(err.detail || "保存失败", "err");
          return false;
        }
        return true;
      },
    }],
    onMount: (overlay) => {
      // 浏览器密码管理器防自动填充：渲染时 readonly 挡 autofill，用户点进输入框即解锁
      overlay.querySelectorAll("input[data-unlock]").forEach((inp) => {
        inp.onfocus = () => inp.removeAttribute("readonly");
      });
      overlay.querySelectorAll("#m-roles .chip").forEach((chip) => {
        chip.onclick = () => {
          // 角色互斥：点选后把已选集合收敛进 ROLE_COMBO[clicked]，出界的自动取消
          const clicked = chip.dataset.role;
          const chips = [...overlay.querySelectorAll("#m-roles .chip")];
          const cur = new Set(chips.filter((c) => c.classList.contains("active")).map((c) => c.dataset.role));
          if (cur.has(clicked)) {
            cur.delete(clicked);
          } else {
            cur.add(clicked);
            [...cur].forEach((r) => { if (!ROLE_COMBO[clicked].includes(r)) cur.delete(r); });
          }
          chips.forEach((c) => c.classList.toggle("active", cur.has(c.dataset.role)));
          overlay.querySelector("#m-lead").classList.toggle("on", cur.has("researcher"));
          overlay.querySelector("#m-class").classList.toggle("on", cur.has("student"));
          overlay.querySelector("#m-teach").classList.toggle("on", cur.has("teacher"));
        };
      });
      // 任教班级多选：各自独立勾选，不受角色互斥矩阵影响
      overlay.querySelectorAll("#m-teach-classes .chip").forEach((chip) => {
        chip.onclick = () => chip.classList.toggle("active");
      });
      overlay.querySelectorAll("#m-active button").forEach((btn) => {
        btn.onclick = () => {
          overlay.querySelectorAll("#m-active button").forEach((b) => b.classList.toggle("active", b === btn));
        };
      });
    },
  });
}

/* ---------------------------------------------------------------- 授课关系 */
async function renderTeaching(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">授课关系 · A-01</div>
    <h1 class="page-title">正在取授课关系…</h1></div><div class="skeleton" style="height:280px"></div>`;

  const [rows, teachers, researchers, classes] = await Promise.all([
    http.get("/api/admin/teaching-assignments"),
    loadUsersByRole("teacher"),
    loadUsersByRole("researcher"),
    loadClasses(true),
  ]);
  A.tas = rows || [];
  // 教师下拉 = 全部 teacher + researcher 账号（双角色去重）；重复不用前端防，
  // 后端按 (教师, 班级, 学科) 查现有授课关系判重，加重复会提示"该授课关系已存在"
  const byId = new Map();
  [...teachers, ...researchers].forEach((u) => byId.set(u.id, u));
  const candidates = [...byId.values()];

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">授课关系 · A-01</div>
      <h1 class="page-title">老师的可见范围由这张表决定</h1>
      <p class="page-sub">teacher ↔ 班级 ↔ 学科。改完即时生效，不用重新登录。</p>
    </div>

    <div class="grid grid-2" style="align-items:start;margin-bottom:18px">
      <section class="card card-pad">
        <div class="card-title">新增授课关系</div>
        <div style="margin-top:16px">
          <div class="field"><label for="ta-teacher">教师</label>
            <select class="select" id="ta-teacher">
              ${candidates.map((t) => `<option value="${t.id}">${esc(t.name)}（${esc(t.username)} · #${t.seq ?? t.id}）</option>`).join("")
                || '<option value="">没有教师/教研组长账号</option>'}
            </select></div>
        </div>
        <div class="form-row" style="margin-top:14px">
          <div class="field"><label for="ta-class">班级</label>
            <select class="select" id="ta-class">
              ${classes.map((c) => `<option value="${c.id}">${esc(c.grade)}${esc(c.name)}（#${c.id}）</option>`).join("")
                || '<option value="">还没有班级</option>'}
            </select></div>
          <div class="field"><label for="ta-subject">学科</label>
            <input class="input" id="ta-subject" value="math" /></div>
        </div>
        <div class="form-actions">
          <button class="btn btn-primary" id="ta-add" ${candidates.length && classes.length ? "" : "disabled"}>添加</button>
        </div>
      </section>

      <section class="card card-pad">
        <div class="card-title">当前 ${A.tas.length} 条授课关系</div>
        <div class="card-sub">删除即撤销该老师对该班的数据权限</div>
        <div class="table-wrap" style="margin-top:12px;max-height:290px;overflow:auto">
          <table class="table">
            <thead><tr><th>id</th><th>教师</th><th>班级</th><th>学科</th><th></th></tr></thead>
            <tbody>
              ${A.tas.map((r) => `<tr>
                <td class="muted">#${r.id}</td>
                <td>${esc(r.teacher)}</td>
                <td>${esc(r.class_name)}</td>
                <td>${esc(r.subject)}</td>
                <td style="text-align:right"><button class="btn btn-sm btn-danger" data-del-ta="${r.id}">删除</button></td>
              </tr>`).join("") || `<tr><td colspan="5" class="muted" style="padding:24px;text-align:center">还没有授课关系</td></tr>`}
            </tbody>
          </table>
        </div>
      </section>
    </div>`;

  const add = async () => {
    const teacherId = Number(view.querySelector("#ta-teacher").value);
    const classId = Number(view.querySelector("#ta-class").value);
    if (!teacherId || !classId) { toast("请选择教师与班级", "err"); return; }
    const btn = view.querySelector("#ta-add");
    const restore = busy(btn, "添加中…");
    try {
      await http.post("/api/admin/teaching-assignments", {
        teacher_id: teacherId, class_id: classId,
        subject: view.querySelector("#ta-subject").value.trim() || "math",
      });
      toast("已添加，老师端立即生效", "ok");
      renderTeaching(view, ctx);
    } catch (err) {
      restore();
      toast(err.detail || "添加失败", "err");
    }
  };
  view.querySelector("#ta-add").onclick = add;

  view.querySelectorAll("[data-del-ta]").forEach((btn) => {
    btn.onclick = async () => {
      const restore = busy(btn, "…");
      try {
        await http.del(`/api/admin/teaching-assignments/${btn.dataset.delTa}`);
        toast("已删除", "ok");
        renderTeaching(view, ctx);
      } catch (err) {
        restore();
        toast(err.detail || "删除失败", "err");
      }
    };
  });
}

/* ---------------------------------------------------------------- 教研任命 */
async function renderLeads(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">教研任命 · A-01</div>
    <h1 class="page-title">正在取任命…</h1></div><div class="skeleton" style="height:260px"></div>`;

  const [rows, researchers] = await Promise.all([
    http.get("/api/admin/research-leads"),
    loadUsersByRole("researcher"),
  ]);
  A.leads = rows || [];
  // 已有任命的账号从候选里过滤掉（一人一条任命；要换人/换年级先撤销原任命）
  const appointed = new Set(A.leads.map((l) => l.user_id));
  const candidates = researchers.filter((u) => !appointed.has(u.id));

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">教研任命 · A-01</div>
      <h1 class="page-title">组长的范围：年级 × 学科</h1>
      <p class="page-sub">教研组不是实体表，它是"年级 × 学科"这个二维坐标；任命记录就是聚合通道的数据源。</p>
    </div>

    <div class="grid grid-2" style="align-items:start">
      <section class="card card-pad">
        <div class="card-title">新增任命</div>
        <div class="card-sub">此操作：同时给该账号 researcher 角色</div>
        <div class="field" style="margin-top:16px"><label for="rl-user">用户</label>
          <select class="select" id="rl-user">
            ${candidates.map((u) => `<option value="${u.id}">${esc(u.name)}（${esc(u.username)} · #${u.seq ?? u.id}）</option>`).join("")
              || '<option value="">没有可任命的账号</option>'}
          </select></div>
        <div class="form-row" style="margin-top:14px">
          <div class="field"><label for="rl-grade">年级</label>
            <select class="select" id="rl-grade">${GRADES.map((g) => `<option>${g}</option>`).join("")}</select></div>
          <div class="field"><label for="rl-subject">学科</label>
            <input class="input" id="rl-subject" value="math" /></div>
        </div>
        <div class="form-actions">
          <button class="btn btn-primary" id="rl-add" ${candidates.length ? "" : "disabled"}>任命</button>
        </div>
        ${candidates.length ? "" : `<div class="alert alert-warn" style="margin-top:12px"><span class="dot"></span><div>${
          researchers.length ? "可选账号都已有任命；如需调整，先撤销原任命。" : "先给账号加上 researcher 角色，再来任命。"
        }</div></div>`}
      </section>

      <section class="card card-pad">
        <div class="card-title">当前 ${A.leads.length} 条任命</div>
        <div class="card-sub">撤销后该账号的聚合 scope 立即收缩</div>
        <div class="table-wrap" style="margin-top:12px">
          <table class="table">
            <thead><tr><th>id</th><th>用户</th><th>年级</th><th>学科</th><th></th></tr></thead>
            <tbody>
              ${A.leads.map((r) => `<tr>
                <td class="muted">#${r.id}</td>
                <td>${esc(r.username)}</td>
                <td>${esc(r.grade)}</td>
                <td>${esc(r.subject)}</td>
                <td style="text-align:right"><button class="btn btn-sm btn-danger" data-del-rl="${r.id}">撤销</button></td>
              </tr>`).join("") || `<tr><td colspan="5" class="muted" style="padding:24px;text-align:center">还没有任命</td></tr>`}
            </tbody>
          </table>
        </div>
      </section>
    </div>`;

  view.querySelector("#rl-add").onclick = async () => {
    const btn = view.querySelector("#rl-add");
    const restore = busy(btn, "任命中…");
    try {
      await http.post("/api/admin/research-leads", {
        user_id: Number(view.querySelector("#rl-user").value),
        grade: view.querySelector("#rl-grade").value,
        subject: view.querySelector("#rl-subject").value.trim() || "math",
      });
      toast("已任命", "ok");
      renderLeads(view, ctx);
    } catch (err) {
      restore();
      toast(err.detail || "任命失败", "err");
    }
  };

  view.querySelectorAll("[data-del-rl]").forEach((btn) => {
    btn.onclick = async () => {
      const restore = busy(btn, "…");
      try {
        await http.del(`/api/admin/research-leads/${btn.dataset.delRl}`);
        toast("已撤销", "ok");
        renderLeads(view, ctx);
      } catch (err) {
        restore();
        toast(err.detail || "撤销失败", "err");
      }
    };
  });
}

/* ---------------------------------------------------------------- 班级管理 */
async function renderClasses(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">班级管理 · A-01</div>
    <h1 class="page-title">正在取班级…</h1></div><div class="skeleton" style="height:240px"></div>`;

  const rows = await loadClasses(true);
  const grouped = rows.reduce((acc, c) => {
    (acc[c.grade] = acc[c.grade] || []).push(c);
    return acc;
  }, {});

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">班级管理 · A-01</div>
      <h1 class="page-title">建用户之前，先有班级</h1>
      <p class="page-sub">(年级, 班名) 全校唯一。班里还有学生或授课关系时不能删，先迁移。</p>
    </div>

    <div class="grid grid-2" style="align-items:start">
      <section class="card card-pad">
        <div class="card-title">新建班级</div>
        <div class="form-row" style="margin-top:16px">
          <div class="field"><label for="c-grade">年级</label>
            <select class="select" id="c-grade">${GRADES.map((g) => `<option>${g}</option>`).join("")}</select></div>
          <div class="field"><label for="c-name">班名</label>
            <input class="input" id="c-name" placeholder="如 3班" /></div>
        </div>
        <div class="form-actions"><button class="btn btn-primary" id="c-add">建班</button></div>
      </section>

      <section class="card card-pad">
        <div class="card-title">当前 ${rows.length} 个班级</div>
        <div class="card-sub">${Object.keys(grouped).map((g) => `${g} ${grouped[g].length} 个`).join(" · ") || "暂无"}</div>
        <div class="table-wrap" style="margin-top:12px;max-height:360px;overflow:auto">
          <table class="table">
            <thead><tr><th>id</th><th>年级</th><th>班名</th><th></th></tr></thead>
            <tbody>
              ${rows.map((c) => `<tr>
                <td class="muted">#${c.id}</td>
                <td>${esc(c.grade)}</td>
                <td>${esc(c.name)}</td>
                <td style="text-align:right"><button class="btn btn-sm btn-danger" data-del-c="${c.id}">删除</button></td>
              </tr>`).join("") || `<tr><td colspan="4" class="muted" style="padding:24px;text-align:center">还没有班级</td></tr>`}
            </tbody>
          </table>
        </div>
      </section>
    </div>`;

  view.querySelector("#c-add").onclick = async () => {
    const name = view.querySelector("#c-name").value.trim();
    if (!name) { toast("请填班名", "err"); return; }
    const btn = view.querySelector("#c-add");
    const restore = busy(btn, "建班中…");
    try {
      await http.post("/api/admin/classes", { grade: view.querySelector("#c-grade").value, name });
      toast("已建班", "ok");
      renderClasses(view, ctx);
    } catch (err) {
      restore();
      toast(err.detail || "建班失败", "err");
    }
  };

  view.querySelectorAll("[data-del-c]").forEach((btn) => {
    btn.onclick = async () => {
      const restore = busy(btn, "…");
      try {
        await http.del(`/api/admin/classes/${btn.dataset.delC}`);
        toast("已删除", "ok");
        renderClasses(view, ctx);
      } catch (err) {
        restore();
        if (err.status === 409) {
          // 班里还有学生/授课关系 → 弹窗拦截，不删
          modal({
            title: "无法删除",
            sub: "先迁移再删",
            body: `<div class="alert alert-warn"><span class="dot"></span>
              <div>${esc(err.detail || "班级存在学生或授课关系，请先迁移")}</div></div>`,
            actions: [{ label: "知道了", kind: "primary" }],
          });
          return;
        }
        toast(err.detail || "删除失败", "err");
      }
    };
  });
}

/* ---------------------------------------------------------------- 审计日志 */
async function renderLogs(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">审计日志 · A-01</div>
    <h1 class="page-title">正在取日志…</h1></div><div class="skeleton" style="height:280px"></div>`;

  const rows = (await http.get("/api/admin/audit-logs", {
    action: A.logAction || undefined, page: 1, size: 50,
  })) || [];

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">审计日志 · A-01</div>
      <h1 class="page-title">谁在什么时候改了什么</h1>
      <p class="page-sub">导入、授课关系变更、批改确认、任命都会留痕。只读，不可改。</p>
    </div>

    <div class="card">
      <div class="card-pad" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding-bottom:8px">
        <input class="input" id="log-action" placeholder="按 action 过滤，如 user.import" style="width:260px"
          value="${esc(A.logAction)}" />
        <button class="btn btn-secondary" id="log-search">查询</button>
        <span class="spacer" style="flex:1"></span>
        <span class="tiny">最近 ${rows.length} 条</span>
      </div>
      <div class="table-wrap">
        <table class="table">
          <thead><tr><th>时间</th><th>操作</th><th>对象</th><th>用户</th><th>详情</th></tr></thead>
          <tbody>
            ${rows.map((l) => `<tr>
              <td class="tiny nowrap">${dateOf(l.created_at)}</td>
              <td><span class="pill">${esc(l.action)}</span></td>
              <td class="tiny">${esc(l.target || "—")}</td>
              <td class="muted">#${l.user_id}</td>
              <td class="tiny">${esc(clip(l.detail || "", 70))}</td>
            </tr>`).join("") || `<tr><td colspan="5" class="muted" style="padding:30px;text-align:center">暂无日志</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>`;

  view.querySelector("#log-search").onclick = () => {
    A.logAction = view.querySelector("#log-action").value.trim();
    renderLogs(view, ctx);
  };
  view.querySelector("#log-action").onkeydown = (e) => {
    if (e.key === "Enter") view.querySelector("#log-search").click();
  };
}
