/* ============================================================================
 * views/teacher.js · 老师端（对齐 app/api/teacher.py）
 *   概览     GET /classes · GET /grade/pending
 *   待批改   POST /grade/{answer_id} → 客观题直判 / 主观题 pending_confirm
 *            POST /grade/{answer_id}/confirm  {action, score, comment}
 *   考试批改 GET /exam/papers · GET /exam/{id}/grading
 *            POST /exam/{id}/question/{qid}/pre-grade（按题批量 AI 批改）
 *   布置作业 POST /homework/create · GET /homework/{id}/submissions
 *   班级学情 GET /class/{id}/error-summary · GET /class/{id}/mastery
 *
 * 越权口径：所有请求都由后端按 teaching_assignments 校验，
 * 前端只负责把「不在你任教范围」的 403 原文透出来。
 * ========================================================================== */
import {
  API_BASE, http, toast, esc, md, mdInline, busy, pct, fixed, dateOf, clip,
  emptyState, errorBox,
} from "../core.js";
import { kpName } from "../kp-names.js";
import { barRows } from "../charts.js";

const T = {
  classes: null,
  pending: null,
  cards: {},        // answer_id -> {kind:'suggest'|'final', data}
  decisions: {},    // answer_id -> {action, score}
  classId: null,
  summary: null,
  mastery: null,
  submissions: null,
  submissionsKeyword: null, // 作业提交情况：上次搜的 id/作业名（翻页要带着它再查）
  lastHomework: null,
  homeworkList: null,   // 已布置作业列表（后端分页：时间倒序，每页 10 条）
  homeworkPage: 1,
  submissionHwId: null, // 从作业列表跳到「班级学情」时带过去的作业 id
  // ---- 考试批改 M7 ----
  gradeMode: "paper",   // paper=按试卷/题目分组批改；all=原作答流水
  papers: null,         // 我教的班里已推送的试卷
  paperId: null,        // 当前选中的卷
  gradeData: null,      // 当前卷的分组批改数据
  gradeBusy: {},        // answer_id -> true（AI 批改中）
};

const QTYPE = { choice: "选择题", fill: "填空题", subjective: "解答题" };

/* ---------------------------------------------------------------- 入口 */
export async function render(view, ctx) {
  if (ctx.tab === "grading") return renderGrading(view, ctx);
  if (ctx.tab === "homework") return renderHomework(view, ctx);
  if (ctx.tab === "class") return renderClass(view, ctx);
  return renderOverview(view, ctx);
}

async function loadClasses() {
  if (!T.classes) T.classes = (await http.get("/api/teacher/classes")) || [];
  return T.classes;
}

/* ---------------------------------------------------------------- 概览 */
async function renderOverview(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">老师端</div>
    <h1 class="page-title">正在取你的班级…</h1></div>
    <div class="grid grid-3">${'<div class="skeleton" style="height:118px"></div>'.repeat(3)}</div>`;

  const [classes, pending] = await Promise.all([
    loadClasses(),
    http.get("/api/teacher/grade/pending"),
  ]);
  T.pending = pending || [];

  const subjective = T.pending.filter((p) => p.question_type === "subjective").length;

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">老师端 · 教学工作台</div>
      <h1 class="page-title">${greeting()}，${esc(ctx.me.name)}</h1>
      <p class="page-sub">你的数据范围来自授课关系（teacher ↔ 班级 ↔ 学科），教案之外的事交给系统：抽题、判分、汇总。</p>
    </div>

    <div class="grid grid-3" style="margin-bottom:26px">
      <div class="card stat">
        <div class="stat-label">任教班级</div>
        <div class="stat-value">${classes.length}<small>个</small></div>
        <div class="stat-foot">${classes.length ? classes.map((c) => esc(`${c.grade}${c.name}`)).join(" · ") : "尚未分配班级"}</div>
      </div>
      <div class="card stat">
        <div class="stat-label">待批改作答</div>
        <div class="stat-value">${T.pending.length}<small>份</small></div>
        <div class="stat-foot">客观题规则直判，主观题 AI 建议 + 你确认</div>
      </div>
      <div class="card stat">
        <div class="stat-label">主观题待裁决</div>
        <div class="stat-value">${subjective}<small>份</small></div>
        <div class="stat-foot">只有你确认后才写入成绩与掌握度</div>
      </div>
    </div>

    <div class="grid grid-2" style="align-items:start">
      <section class="card card-pad">
        <div class="card-title">我的班级</div>
        <div class="card-sub">点进去看错题汇总与掌握度</div>
        <div class="list" style="margin-top:10px">
          ${classes.length ? classes.map((c) => `
            <div class="list-row">
              <span class="pill pill-blue">${esc(c.grade)}</span>
              <div class="grow">
                <div class="title">${esc(c.name)}</div>
                <div class="meta">班级 #${c.id}</div>
              </div>
              <button class="btn btn-sm btn-secondary" data-goto-class="${c.id}">学情</button>
            </div>`).join("") : emptyState("还没有任教班级", "请管理员在管理后台 → 授课关系里添加", "users")}
        </div>
      </section>

      <section class="card card-pad">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px">
          <div><div class="card-title">待批改</div>
            <div class="card-sub">按提交时间倒序，最多 50 条</div></div>
          <a class="btn btn-ghost btn-sm" href="#/teacher/grading">全部</a>
        </div>
        <div class="list" style="margin-top:10px">
          ${T.pending.length ? T.pending.slice(0, 4).map((p) => `
            <div class="list-row">
              <div class="grow">
                <div class="title">${esc(p.student)}</div>
                <div class="meta">${esc(QTYPE[p.question_type] || p.question_type)} · ${esc(clip(p.question, 26))}</div>
              </div>
              <a class="btn btn-sm btn-secondary" href="#/teacher/grading">去批改</a>
            </div>`).join("") : emptyState("全部处理完了", "没有待批改的作答", "check")}
        </div>
      </section>
    </div>`;

  view.querySelectorAll("[data-goto-class]").forEach((btn) => {
    btn.onclick = () => {
      T.classId = Number(btn.dataset.gotoClass);
      T.summary = null; T.mastery = null; T.submissions = null;
      location.hash = "#/teacher/class";
    };
  });
}

/* ---------------------------------------------------------------- 待批改 */
/* 两种看法：
 *   paper —— 按试卷 → 按题目分组：一题下面挂全班作答（考试批改主视图）
 *   all   —— 原来的作答流水：谁、哪题、什么时候交（作业/对话也走这里）
 *
 * 为什么默认 paper：老师手上的活 90% 是考试卷的主观题，
 * 一条题一个评分标准连着批 30 份，尺度才稳；流水视图适合零散作业。
 */
async function renderGrading(view, ctx) {
  const mode = T.gradeMode || "paper";
  const head = `
    <div class="page-head">
      <div class="eyebrow">自动批改 · T-02</div>
      <h1 class="page-title">客观题对错判断即时生效，主观题你说了算</h1>
      <p class="page-sub">客观题由规则直判；主观题 AI 只给建议分与评语，决定：采纳 / 改分 / 驳回之后才写成绩与 BKT掌握度。</p>
    </div>
    <div class="segmented" style="margin-bottom:18px">
      <button data-grade-mode="paper" class="${mode === "paper" ? "active" : ""}">按试卷/题目批改</button>
      <button data-grade-mode="all" class="${mode === "all" ? "active" : ""}">全部待批改</button>
    </div>`;

  const wrap = (inner) => `${head}${inner}`;

  if (mode === "all") await renderPendingList(view, ctx, wrap);
  else await renderExamGrading(view, ctx, wrap);

  // 绑定必须放在子视图渲染完之后：wrap 里的按钮是随 innerHTML 一起进 DOM 的，
  // 提前 querySelectorAll 拿到的是空集合（这是最容易写错的一处）
  view.querySelectorAll("[data-grade-mode]").forEach((b) => {
    b.onclick = () => {
      if (T.gradeMode === b.dataset.gradeMode) return;
      T.gradeMode = b.dataset.gradeMode;
      renderGrading(view, ctx);
    };
  });
}

/* ---- 模式二：原作答流水 ---- */
async function renderPendingList(view, ctx, wrap) {
  view.innerHTML = wrap(`<div class="skeleton" style="height:160px"></div>`);

  T.pending = (await http.get("/api/teacher/grade/pending")) || [];

  if (!T.pending.length) {
    view.innerHTML = wrap(`<div class="card card-pad">${emptyState("没有待批改的作答", "学生提交后会自动出现在这里", "check")}</div>`);
    return;
  }

  view.innerHTML = wrap(`
    <div class="muted" style="margin-bottom:14px;font-size:13px">
      共 <span id="pending-count">${T.pending.length}</span> 条 · 点击「AI 批改」开始
    </div>
    <div id="grade-list" style="display:flex;flex-direction:column;gap:14px"></div>`);

  const list = view.querySelector("#grade-list");
  T.pending.forEach((row) => list.appendChild(gradeCard(row, view, ctx)));
}

/* ---- 模式一：按试卷 → 按题目分组（考试批改主视图） ---- */
const EXAM_TYPE_CN = { monthly: "月考", midterm: "期中", final: "期末" };

async function renderExamGrading(view, ctx, wrap) {
  view.innerHTML = wrap(`<div class="skeleton" style="height:220px"></div>`);

  if (T.papers === null) {
    T.papers = (await http.get("/api/teacher/exam/papers")) || [];
  }

  if (!T.papers.length) {
    // 没卷不代表没活干：作业和对话的作答仍在「全部待批改」里，
    // 所以空态必须给一条出口，否则老师会以为批改页空了
    view.innerHTML = wrap(`<div class="card card-pad">
      ${emptyState("还没有推送给你的班级的试卷",
                   "教研组长定稿后，卷子会按年级推送到你任教的班级", "file")}
      <div class="form-actions" style="justify-content:center">
        <button class="btn btn-secondary" id="to-all-grading">去看全部待批改（作业/对话）</button>
      </div>
    </div>`);
    view.querySelector("#to-all-grading").onclick = () => {
      T.gradeMode = "all";
      renderGrading(view, ctx);
    };
    return;
  }

  const activeId = (T.paperId && T.papers.some((p) => p.paper_id === T.paperId))
    ? T.paperId
    : T.papers[0].paper_id;
  T.paperId = activeId;

  if (!T.gradeData || T.gradeData.paper_id !== activeId) {
    view.innerHTML = wrap(`<div class="skeleton" style="height:260px"></div>`);
    T.gradeData = await http.get(`/api/teacher/exam/${activeId}/grading`);
  }

  const d = T.gradeData;
  const papers = T.papers;
  const sum = d.summary || { subjective_total: 0, pending: 0, graded: 0 };

  view.innerHTML = wrap(`
    <div class="grade-layout">
      <aside>
        <div class="tiny" style="margin-bottom:8px">我任教的班里已推送的试卷</div>
        ${papers.map((p) => `
          <div class="paper-tab ${p.paper_id === activeId ? "active" : ""}" data-paper="${p.paper_id}">
            <div class="grow" style="flex:1;min-width:0">
              <div class="title" style="font-size:13.5px;font-weight:500">${esc(p.title)}</div>
              <div class="tiny">${esc(EXAM_TYPE_CN[p.exam_type] || p.exam_type)} ·
                已交 ${p.submitted_count}/${p.student_count} 人 · ${p.question_count} 题</div>
            </div>
            ${p.pending_count
              ? `<span class="pill pill-orange">待批 ${p.pending_count}</span>`
              : `<span class="pill pill-green">已批完</span>`}
          </div>`).join("")}
      </aside>

      <div>
        <div class="card card-pad" style="margin-bottom:14px">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <div class="grow" style="flex:1;min-width:200px">
              <div class="card-title">${esc(d.title)}</div>
              <div class="card-sub">
                ${esc(d.grade)} · 主观题 ${sum.subjective_total} 道 · 满分 ${fixed(d.full_score, 0)}
              </div>
            </div>
            <span class="pill pill-orange">待批 ${sum.pending}</span>
            <span class="pill pill-green">已批 ${sum.graded}</span>
          </div>
          <div class="tiny" style="margin-top:12px;line-height:1.8">
            按题目分组：一题一组，组内是本班全体学生对该题的作答。
            同一道题连着批，评分尺度最稳；「AI 批改本题全部」一次给出全组建议分，你再逐条采纳/改分/驳回。
          </div>
        </div>

        ${(d.groups || []).length
          ? d.groups.map((g) => gradeGroupHTML(g)).join("")
          : `<div class="card card-pad">${emptyState("这份卷没有主观题", "全客观题的卷子交卷即判完，不需要人工批改", "check")}</div>`}
      </div>
    </div>`);

  view.querySelectorAll("[data-paper]").forEach((el) => {
    el.onclick = () => {
      T.paperId = Number(el.dataset.paper);
      T.gradeData = null;
      renderGrading(view, ctx);
    };
  });

  view.querySelectorAll("[data-pregrade]").forEach((btn) => {
    btn.onclick = () => pregradeQuestion(btn, view, ctx);
  });

  view.querySelectorAll("[data-aigrade]").forEach((btn) => {
    btn.onclick = () => pregradeOne(btn, view, ctx);
  });

  // 自主批改：不跑 AI，老师直接判 正确/部分正确/错误
  view.querySelectorAll("[data-manual]").forEach((btn) => {
    btn.onclick = () => {
      const aid = Number(btn.dataset.manual);
      const container = view.querySelector(`[data-answer="${aid}"]`);
      const box = container?.querySelector("[data-result]");
      if (!box) return;
      box.innerHTML = manualPanelHTML({ answer_id: aid, full_score: Number(btn.dataset.full) });
      bindManualGrade(box, { answer_id: aid, full_score: Number(btn.dataset.full) },
                      view, ctx, container);
    };
  });
}

function gradeGroupHTML(g) {
  const pending = g.answers.filter((a) => a.is_correct === null).length;
  const graded = g.answers.length - pending;

  return `<section class="grade-group" data-group="${g.question_id}">
    <div class="grade-group-hd">
      <span class="pill pill-blue">第 ${g.seq} 题</span>
      <span class="pill">${fixed(g.score, 1)} 分</span>
      <span data-group-count>
        <span class="pill pill-orange">待批 ${pending}</span>
        <span class="pill pill-green">已批 ${graded}</span>
      </span>
      <div class="spacer" style="flex:1"></div>
      ${pending
        ? `<button class="btn btn-sm btn-primary" data-pregrade="${g.question_id}">AI 批改本题全部（${pending}）</button>`
        : `<span class="pill pill-green">本题已批完</span>`}
    </div>
    <div class="grade-group-bd">
      <div style="padding:12px 0 4px">
        <div class="q-text">${md(g.content)}</div>
        <details style="margin-top:10px">
          <summary class="tiny" style="cursor:pointer">查看标准答案与解析</summary>
          <div style="margin-top:8px;font-size:13px;line-height:1.8;color:var(--ink-2)">
            <div><b>标准答案：</b>${esc(g.answer || "—")}</div>
            ${g.analysis ? `<div style="margin-top:6px"><b>解析：</b>${md(g.analysis)}</div>` : ""}
          </div>
        </details>
      </div>
      ${g.answers.length
        ? g.answers.map((a) => gradeAnswerHTML(a, g.score)).join("")
        : `<div class="tiny" style="padding:14px 0">本题还没有学生提交作答。</div>`}
    </div>
  </section>`;
}

function gradeAnswerHTML(a, fullScore) {
  const pending = a.is_correct === null;
  const full = Number(fullScore ?? a.full_score ?? 100);
  const statePill = pending
    ? `<span class="pill pill-orange" data-pending="1" data-state>待批改</span>`
    : `<span class="pill ${a.is_correct ? "pill-green" : "pill-red"}" data-pending="0" data-state>
         ${a.is_correct ? "答对" : "答错"} · ${fixed(a.score ?? 0, 1)} 分</span>`;

  return `<div class="grade-answer" data-answer="${a.answer_id}">
    <div class="who">
      <span class="name">${esc(a.student)}</span>
      <span class="tiny">${esc(a.class_name || "")} · 交卷 ${dateOf(a.submitted_at)}</span>
      ${statePill}
      <span class="spacer" style="flex:1"></span>
      ${pending ? `
        <button class="btn btn-sm btn-secondary" data-manual="${a.answer_id}" data-full="${full}">自主批改</button>
        <button class="btn btn-sm btn-secondary" data-aigrade="${a.answer_id}">AI 批改</button>` : ""}
    </div>
    <div class="body-text ${a.content ? "" : "empty"}">${a.content ? esc(a.content) : "（未填写文字，见下方手写图）"}</div>
    ${(a.attachments || []).length ? `<div class="imgs">
      ${a.attachments.map((u) => `<a href="${esc(API_BASE + u)}" target="_blank" rel="noopener">
        <img src="${esc(API_BASE + u)}" alt="手写解题图片" loading="lazy" /></a>`).join("")}
    </div>` : ""}
    <div data-result></div>
  </div>`;
}

/* 老师自主批改面板：正确 / 部分正确 / 错误；选部分正确才显示给分框 */
function manualPanelHTML(item) {
  const full = Number(item.full_score ?? item.fullScore ?? 100);
  return `<div class="manual-grade">
    <div class="mg-hd">
      <span class="pill pill-dark">自主批改</span>
      <span class="tiny">本题满分 ${fixed(full, 1)} 分 · 判完直接生效并回写掌握度</span>
    </div>
    <div class="segmented" data-verdict>
      <button data-val="correct">正确（${fixed(full, 1)} 分）</button>
      <button data-val="partial">部分正确</button>
      <button data-val="wrong">错误（0 分）</button>
    </div>
    <div class="mg-score" data-score-box hidden>
      <span class="tiny">给分</span>
      <input class="input" type="number" min="0" max="${full}" step="0.5"
        data-score placeholder="0 ~ ${fixed(full, 1)}" />
      <span class="tiny">/ ${fixed(full, 1)} 分</span>
    </div>
    <div class="mg-actions">
      <input class="input" data-comment maxlength="200" placeholder="评语（可选，写进批改记录）" />
      <button class="btn btn-sm btn-primary" data-mg-submit>确认给分</button>
      <button class="btn btn-sm btn-ghost" data-mg-cancel>取消</button>
    </div>
  </div>`;
}

function bindManualGrade(box, item, view, ctx, container) {
  const full = Number(item.full_score ?? item.fullScore ?? 100);
  const seg = box.querySelector("[data-verdict]");
  const scoreBox = box.querySelector("[data-score-box]");
  const scoreInput = box.querySelector("[data-score]");
  let verdict = null;

  seg.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      verdict = btn.dataset.val;
      seg.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
      scoreBox.hidden = verdict !== "partial";
      // 再压一层 inline display：以后若有人给 .mg-score 加 display 声明，
      // [hidden] 又会被盖掉，给分框会莫名其妙地弹出来
      scoreBox.style.display = verdict === "partial" ? "" : "none";
      // 预填一半分：老师多数是"改个数"，不是从 0 开始想
      if (verdict === "partial" && !scoreInput.value) {
        scoreInput.value = String(Math.round(full / 2 * 2) / 2);
      }
    };
  });

  box.querySelector("[data-mg-cancel]").onclick = () => { box.innerHTML = ""; };

  const submit = box.querySelector("[data-mg-submit]");
  submit.onclick = async () => {
    if (!verdict) { toast("先选：正确 / 部分正确 / 错误", "err"); return; }

    let score = null;
    if (verdict === "partial") {
      score = Number(scoreInput.value);
      if (!Number.isFinite(score) || score <= 0 || score >= full) {
        toast(`部分正确的分数要在 0 ~ ${fixed(full, 1)} 之间（全对选「正确」、全错选「错误」）`, "err");
        return;
      }
    }

    const comment = (box.querySelector("[data-comment]").value || "").trim();
    const restore = busy(submit, "落库中…");
    try {
      const out = await http.post(`/api/teacher/grade/${item.answer_id}/manual`, {
        judgement: verdict,
        score: verdict === "partial" ? score : null,
        comment: comment || null,
      });
      box.innerHTML = finalHTML(out);
      markAnswerFinal(container, out, item.answer_id);
      toast(`已给 ${fixed(out.final_score, 1)} 分，掌握度同步更新`, "ok");
      restore();
    } catch (err) {
      restore();
      toast(err.detail || "给分失败", "err");
    }
  };
}

/* 单条 AI 批改：客观题终局、主观题挂起给建议 */
async function pregradeOne(btn, view, ctx) {
  const aid = Number(btn.dataset.aigrade);
  const container = view.querySelector(`[data-answer="${aid}"]`);
  const box = container?.querySelector("[data-result]");
  const restore = busy(btn, "批改中…");
  try {
    const out = await http.post(`/api/teacher/grade/${aid}`);
    if (out.status === "pending_confirm") {
      box.innerHTML = suggestHTML(out.suggestion, aid);
      bindExamSuggest(box, { answer_id: aid }, view, ctx, container);
    } else {
      box.innerHTML = finalHTML(out);
      markAnswerFinal(container, out, aid);
    }
    restore();
  } catch (err) {
    restore();
    if (box) box.innerHTML = errorBox(err.detail || "批改失败");
  }
}

/* 按题批量：一条评分标准跑全组，失败不整体回滚 */
async function pregradeQuestion(btn, view, ctx) {
  const qid = Number(btn.dataset.pregrade);
  const groupEl = view.querySelector(`[data-group="${qid}"]`);
  const restore = busy(btn, "整组批改中…");
  try {
    const out = await http.post(
      `/api/teacher/exam/${T.paperId}/question/${qid}/pre-grade`);

    const items = out.items || [];

    // 一条都没跑：库里这题已经没有 is_correct IS NULL 的作答了，
    // 说明页面上的"待批 N"是旧快照（这条刚在另一个页签/另一处批过）。
    // 不清缓存重拉，老师会对着一个永远批不动的数字反复点。
    if (!items.length) {
      T.gradeData = null;
      restore();
      toast("这道题已经没有待批的作答了（可能刚批过），已刷新", "");
      renderGrading(view, ctx);
      return;
    }

    items.forEach((it) => {
      const container = view.querySelector(`[data-answer="${it.answer_id}"]`);
      if (!container) return;
      const box = container.querySelector("[data-result]");

      if (it.status === "pending_confirm") {
        box.innerHTML = suggestHTML(it.suggestion, it.answer_id);
        bindExamSuggest(box, { answer_id: it.answer_id }, view, ctx, container);
      } else if (it.status === "error") {
        box.innerHTML = errorBox(it.message || "这条批改失败");
      } else {
        box.innerHTML = finalHTML(it);
        markAnswerFinal(container, it, it.answer_id);
      }
    });

    // 分组数据已被就地改过（终局的条数变了），缓存作废，下次切模式重拉
    T.gradeData = null;

    const asked = items.filter((it) => it.status === "pending_confirm").length;
    const settled = items.length - asked - out.failed;
    let msg = `AI 处理 ${out.graded} 条`;
    if (asked) msg += `：${asked} 条待你裁决`;
    if (settled) msg += `${asked ? "，" : "："}${settled} 条已直接判定`;
    if (out.failed) msg += `，${out.failed} 条失败（可单独重试）`;

    toast(msg, out.failed ? "" : "ok");
    if (groupEl) refreshGroupCount(groupEl);
    restore();
  } catch (err) {
    restore();
    toast(err.detail || "批量批改失败", "err");
  }
}

/* 建议卡的裁决绑定：确认后把这条作答就地改成终局态，并刷新本组待批数 */
function bindExamSuggest(box, item, view, ctx, container) {
  const aid = item.answer_id;
  const decision = T.decisions[aid] || (T.decisions[aid] = { action: "confirm", score: null });
  // 旧内存里可能残留 reject（入口已移除），兜回 confirm，否则确认时会发后端已不展示的动作
  if (decision.action === "reject") decision.action = "confirm";

  box.querySelectorAll("[data-act] button").forEach((btn) => {
    btn.onclick = () => {
      decision.action = btn.dataset.val;
      box.querySelectorAll("[data-act] button").forEach((b) => b.classList.toggle("active", b === btn));
      const scoreInput = box.querySelector("[data-score]");
      if (scoreInput) scoreInput.style.display = decision.action === "edit" ? "" : "none";
    };
  });

  const scoreInput = box.querySelector("[data-score]");
  if (scoreInput) scoreInput.oninput = () => { decision.score = Number(scoreInput.value); };

  const confirmBtn = box.querySelector("[data-confirm]");
  confirmBtn.onclick = async () => {
    if (decision.action === "edit" && !Number.isFinite(decision.score)) {
      toast("改分需要填写最终分数", "err");
      return;
    }
    const restore = busy(confirmBtn, "落库中…");
    try {
      const out = await http.post(`/api/teacher/grade/${aid}/confirm`, {
        action: decision.action,
        score: decision.action === "edit" ? decision.score : null,
      });
      box.innerHTML = finalHTML(out);
      markAnswerFinal(container, out, aid);
      toast("已生效，掌握度与考试总分同步更新", "ok");
      restore();
    } catch (err) {
      restore();
      toast(err.detail || "确认失败", "err");
    }
  };
}

/* 把一条作答的状态药丸换成终局，并刷新所在组的待批/已批计数 */
function markAnswerFinal(container, out, answerId) {
  // 任何一条批完，分组快照就过期了：不清掉，切回"按试卷批改"会拿旧数据重绘，
  // 组头仍显示"待批 1"，老师再点"AI 批改本题全部"就是 0 份。
  T.gradeData = null;

  const pill = container?.querySelector("[data-state]");
  if (pill) {
    const rejected = out.status === "rejected";
    pill.className = `pill ${rejected ? "pill-orange" : out.is_correct ? "pill-green" : "pill-red"}`;
    pill.dataset.pending = "0";
    pill.textContent = rejected
      ? "已驳回"
      : `${out.is_correct ? "答对" : "答错"} · ${fixed(out.final_score ?? out.score, 1)} 分`;
  }
  const btn = container?.querySelector("[data-aigrade]");
  if (btn) btn.remove();
  // 自主批改入口也要收起：已批过的作答再点人工给分会 409
  container?.querySelector("[data-manual]")?.remove();
  container?.querySelector("[data-manual-row]")?.remove();

  const groupEl = container?.closest(".grade-group");
  if (groupEl) refreshGroupCount(groupEl);

  // 每批完一条，侧栏那份卷的"待批 N"也要跟着减一
  const paper = (T.papers || []).find((p) => p.paper_id === T.paperId);
  if (paper && paper.pending_count > 0) paper.pending_count -= 1;
  const layout = container?.closest(".grade-layout");
  const tab = layout?.querySelector(`.paper-tab[data-paper="${T.paperId}"] .pill`);
  if (tab && paper) {
    tab.textContent = paper.pending_count ? `待批 ${paper.pending_count}` : "已批完";
    tab.className = `pill ${paper.pending_count ? "pill-orange" : "pill-green"}`;
  }
}

function refreshGroupCount(groupEl) {
  const total = groupEl.querySelectorAll("[data-answer]").length;
  const pending = groupEl.querySelectorAll('[data-pending="1"]').length;
  const box = groupEl.querySelector("[data-group-count]");
  if (box) {
    box.innerHTML = `<span class="pill pill-orange">待批 ${pending}</span>
      <span class="pill pill-green">已批 ${total - pending}</span>`;
  }
  const pre = groupEl.querySelector("[data-pregrade]");
  if (pre) {
    if (pending === 0) {
      pre.outerHTML = `<span class="pill pill-green">本题已批完</span>`;
    } else {
      pre.textContent = `AI 批改本题全部作答（${pending}）`;
    }
  }
}

function gradeCard(row, view, ctx) {
  const card = document.createElement("section");
  card.className = "card card-pad";
  const state = T.cards[row.answer_id];

  card.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span class="pill pill-blue">${esc(QTYPE[row.question_type] || row.question_type)}</span>
      <span class="title" style="font-size:14px;font-weight:500">${esc(row.student)}</span>
      <span class="tiny">作答 #${row.answer_id} · ${dateOf(row.submitted_at)}</span>
      <span class="spacer" style="flex:1"></span>
      ${state ? "" : `
        <button class="btn btn-sm btn-secondary" data-manual-row="${row.answer_id}"
          data-full="${Number(row.full_score ?? 100)}">自主批改</button>
        <button class="btn btn-sm btn-primary" data-grade="${row.answer_id}">AI 批改</button>`}
    </div>

    <div style="margin-top:14px;padding:14px 16px;background:#fafafb;border-radius:12px">
      <div class="tiny" style="margin-bottom:5px">题目</div>
      <div style="font-size:13.5px;line-height:1.65">${esc(row.question)}…</div>
      <div class="tiny" style="margin:12px 0 5px">学生作答</div>
      <div style="font-size:14px;line-height:1.7;white-space:pre-wrap">${esc(row.content)}</div>
    </div>

    <div data-result style="margin-top:14px"></div>`;

  const resultBox = card.querySelector("[data-result]");

  // 自主批改（流水视图）：不经过 AI，直接三选一给分
  const manualRow = card.querySelector("[data-manual-row]");
  if (manualRow) {
    manualRow.onclick = () => {
      const item = { answer_id: row.answer_id, full_score: Number(manualRow.dataset.full) };
      resultBox.innerHTML = manualPanelHTML(item);
      bindManualGrade(resultBox, item, view, ctx, card);
    };
  }

  const trigger = card.querySelector("[data-grade]");
  if (trigger) {
    trigger.onclick = async () => {
      const restore = busy(trigger, "批改中…");
      try {
        const out = await http.post(`/api/teacher/grade/${row.answer_id}`);
        if (out.status === "pending_confirm") {
          T.cards[row.answer_id] = { kind: "suggest", data: out };
        } else {
          T.cards[row.answer_id] = { kind: "final", data: out };
          toast("客观题已直判", "ok");
        }
        T.decisions[row.answer_id] = { action: "confirm", score: null };
        refreshCard(card, row, view, ctx);
      } catch (err) {
        restore();
        resultBox.innerHTML = errorBox(err.detail || "批改失败");
      }
    };
  }

  if (state?.kind === "suggest") {
    resultBox.innerHTML = suggestHTML(state.data.suggestion, row.answer_id);
    bindSuggest(resultBox, row, view, ctx, card);
  } else if (state?.kind === "final") {
    resultBox.innerHTML = finalHTML(state.data);
  }

  return card;
}

function suggestHTML(sug, answerId) {
  const decision = T.decisions[answerId] || { action: "confirm", score: null };
  return `<div class="suggest">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span class="pill pill-orange">AI 建议 · 待你裁决</span>
      <span class="tiny">置信度 ${pct(sug.confidence || 0)}</span>
    </div>
    <div class="score-line" style="margin-top:12px">
      <b>${fixed(sug.ai_score, 1)}</b>
      <span class="muted">/ ${fixed(sug.full_score, 0)} 分</span>
    </div>
    <div style="margin-top:10px;font-size:13.5px;line-height:1.7" class="md">${md(sug.ai_comment || "（无评语）")}</div>
    ${(sug.scoring_basis || []).length ? `<ul class="basis">
      ${sug.scoring_basis.map((b) => `<li>${mdInline(b)}</li>`).join("")}
    </ul>` : ""}

    <div class="decision">
      <div class="segmented" data-act>
        ${[["confirm", "采纳"], ["edit", "改分"]].map(([k, label]) => `
          <button data-val="${k}" class="${(decision.action === "reject" ? "confirm" : decision.action) === k ? "active" : ""}">${label}</button>`).join("")}
      </div>
      <input class="input" data-score type="number" step="0.5" min="0"
        max="${sug.full_score ?? 100}" value="${decision.score ?? sug.ai_score ?? 0}"
        style="width:110px;${decision.action === "edit" ? "" : "display:none"}"
        aria-label="最终分数" />
      <span class="spacer" style="flex:1"></span>
      <button class="btn btn-primary" data-confirm>确认生效并回写 BKT</button>
    </div>
    <div class="tiny" style="margin-top:10px">
      只有「采纳 / 改分」两条路：认为 AI 判错就用这条作答上的「自主批改」自己给分（正确 / 部分正确 / 错误）。
    </div>
  </div>`;
}

function bindSuggest(box, row, view, ctx, card) {
  const aid = row.answer_id;
  const decision = T.decisions[aid] || (T.decisions[aid] = { action: "confirm", score: null });
  if (decision.action === "reject") decision.action = "confirm";

  box.querySelectorAll("[data-act] button").forEach((btn) => {
    btn.onclick = () => {
      decision.action = btn.dataset.val;
      box.querySelectorAll("[data-act] button").forEach((b) => b.classList.toggle("active", b === btn));
      const scoreInput = box.querySelector("[data-score]");
      if (scoreInput) scoreInput.style.display = decision.action === "edit" ? "" : "none";
    };
  });

  const scoreInput = box.querySelector("[data-score]");
  if (scoreInput) scoreInput.oninput = () => { decision.score = Number(scoreInput.value); };

  const confirmBtn = box.querySelector("[data-confirm]");
  confirmBtn.onclick = async () => {
    if (decision.action === "edit" && !Number.isFinite(decision.score)) {
      toast("改分需要填写最终分数", "err");
      return;
    }
    const restore = busy(confirmBtn, "落库中…");
    try {
      const out = await http.post(`/api/teacher/grade/${aid}/confirm`, {
        action: decision.action,
        score: decision.action === "edit" ? decision.score : null,
      });
      T.cards[aid] = { kind: "final", data: out };
      T.pending = T.pending.filter((p) => p.answer_id !== aid);
      const counter = view.querySelector("#pending-count");
      if (counter) counter.textContent = String(T.pending.length);
      toast("已生效，掌握度同步更新", "ok");
      refreshCard(card, row, view, ctx);
    } catch (err) {
      restore();
      toast(err.detail || "确认失败", "err");
    }
  };
}

function finalHTML(out) {
  const ok = out.is_correct === true;
  const rejected = out.status === "rejected";
  return `<div class="alert ${rejected ? "alert-warn" : ok ? "alert-ok" : "alert-info"}">
      <span class="dot"></span>
      <div>
        <div style="font-weight:500;margin-bottom:4px">
          ${rejected ? "已驳回，本次作答不计入成绩" : `最终得分 ${fixed(out.final_score ?? out.score, 1)}`}
        </div>
        <div style="font-size:13px">
          掌握度 ${out.p_known_before != null ? `${fixed(out.p_known_before, 2)} → ` : "→ "}
          ${fixed(out.p_known_after, 2)}
        </div>
      </div>
    </div>`;
}

function refreshCard(card, row, view, ctx) {
  const next = gradeCard(row, view, ctx);
  card.replaceWith(next);
}

/* ---------------------------------------------------------------- 布置作业 T-01 */
async function renderHomework(view, ctx) {
  const classes = await loadClasses();
  // 知识点清单来自后端（knowledge_points 表 + 每个知识点的可抽题量），不再前端写死
  const kps = (await http.get("/api/teacher/knowledge-points")) || [];
  const selected = new Set(kps.filter((k) => ["kp_crossmult", "kp_peifang"].includes(k.id)).map((k) => k.id));

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">布置作业 · T-01</div>
      <h1 class="page-title">按知识点抽题，结果可复现</h1>
      <p class="page-sub">抽题由规则引擎完成（单知识点 ≤2 题、缺口不乱补），不经过 LLM ——同一个知识点、同样的配额，重跑拿到同一套题。</p>
    </div>

    <div class="grid" style="grid-template-columns: 1.2fr 0.8fr">
      <section class="card card-pad">
        <div class="card-title">新建作业</div>
        <div class="card-sub">${classes.length ? "选班级 → 勾知识点 → 定量" : "你还没有任教班级"}</div>

        <div class="field" style="margin-top:18px">
          <label for="hw-class">班级</label>
          <select class="select" id="hw-class" ${classes.length ? "" : "disabled"}>
            ${classes.map((c) => `<option value="${c.id}">${esc(c.grade)}${esc(c.name)}（#${c.id}）</option>`).join("")}
          </select>
        </div>

        <div class="field" style="margin-top:16px">
          <label for="hw-title">作业标题</label>
          <input class="input" id="hw-title" maxlength="128" value="随堂作业" />
        </div>

        <div class="field" style="margin-top:16px">
          <label>知识点范围${kps.length ? `（${kps.length}）` : ""}</label>
          <div class="scroll-list" style="max-height:240px">
            <div class="chip-set" id="kp-set">
              ${kps.map((k) => `
                <button type="button" class="chip ${selected.has(k.id) ? "active" : ""}" data-kp="${k.id}"
                  title="${esc(k.chapter_name || "")} · ${esc(k.id)} · 题库 ${k.question_count} 题">
                  ${esc(k.name)}<span class="kp-id">${k.question_count}题</span>
                </button>`).join("")}
            </div>
          </div>
          <div class="hint">可多选；右上角是该知识点的可抽题量，题量不够会自动进入「缺口」清单。</div>
        </div>

        <div class="form-row" style="margin-top:16px">
          <div class="field">
            <label for="hw-per">每知识点题数</label>
            <select class="select" id="hw-per">
              ${[1, 2, 3, 4, 5].map((n) => `<option value="${n}" ${n === 2 ? "selected" : ""}>${n} 题</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label for="hw-score">每题分值</label>
            <input class="input" id="hw-score" type="number" min="1" max="20" step="0.5" value="4" />
          </div>
        </div>

        <div class="form-actions">
          <button class="btn btn-primary" id="hw-submit" ${classes.length ? "" : "disabled"}>抽题并布置</button>
        </div>
      </section>

      <div style="display:flex;flex-direction:column;gap:16px;min-height:0">
        <section class="card card-pad" style="flex:1 1 0;display:flex;flex-direction:column">
          <div class="card-title">最近一次布置</div>
          <div class="card-sub">抽中题量与缺口会原样返回</div>
          <div id="hw-result" style="margin-top:16px">
            ${T.lastHomework ? homeworkResultHTML(T.lastHomework) : emptyState("还没有布置记录", "左侧提交后会显示结果", "book")}
          </div>
        </section>

        <section class="card card-pad" style="flex:1 1 0;display:flex;flex-direction:column">
          <div class="card-title">已布置作业</div>
          <div class="card-sub">按时间倒序 · 每页 10 条</div>
          <div id="hw-list" style="margin-top:12px"></div>
        </section>
      </div>
    </div>`;

  // 已布置作业列表：时间倒序 + 每页 10 条。翻页只重画这块，左边表单不动
  const listBox = view.querySelector("#hw-list");
  const paintList = async (page) => {
    const data = (await http.get("/api/teacher/homeworks", { page, size: 10 }))
      || { items: [], total: 0, page };
    T.homeworkList = data;
    T.homeworkPage = page;
    listBox.innerHTML = homeworkListHTML(data);
    listBox.querySelectorAll("[data-hw-page]").forEach((btn) => {
      btn.onclick = () => paintList(Number(btn.dataset.hwPage));
    });
    listBox.querySelectorAll("[data-hw-open]").forEach((btn) => {
      btn.onclick = () => {
        T.submissionHwId = Number(btn.dataset.hwOpen);
        location.hash = "#/teacher/class";   // 跳到班级学情，自动带出这次作业
      };
    });
  };
  await paintList(T.homeworkPage || 1);

  view.querySelectorAll("[data-kp]").forEach((chip) => {
    chip.onclick = () => {
      const id = chip.dataset.kp;
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      chip.classList.toggle("active", selected.has(id));
    };
  });

  const submit = view.querySelector("#hw-submit");
  submit.onclick = async () => {
    if (!selected.size) { toast("至少勾一个知识点", "err"); return; }
    const body = {
      class_id: Number(view.querySelector("#hw-class").value),
      title: view.querySelector("#hw-title").value.trim() || "随堂作业",
      kp_ids: [...selected],
      per_kp: Number(view.querySelector("#hw-per").value),
      score: Number(view.querySelector("#hw-score").value) || 4,
    };
    const restore = busy(submit, "抽题中…");
    try {
      const out = await http.post("/api/teacher/homework/create", body);
      T.lastHomework = { ...out, title: body.title, class_id: body.class_id };
      toast(`作业 #${out.homework_id} 已布置`, "ok");
      view.querySelector("#hw-result").innerHTML = homeworkResultHTML(T.lastHomework);
      await paintList(1);   // 新作业排在最前 → 列表回到第 1 页刷新
    } catch (err) {
      restore();
      view.querySelector("#hw-result").innerHTML = errorBox(err.detail || "布置失败");
    }
  };
}

function homeworkResultHTML(out) {
  return `
    <div class="alert alert-ok"><span class="dot"></span>
      <div><b>作业 #${out.homework_id}</b> 已布置 · 抽中 ${out.question_ids.length} 题</div>
    </div>
    ${(out.gaps || []).length ? `
      <div class="tiny" style="margin:18px 0 6px">配题缺口（宁可少题不错题）</div>
      <div class="alert alert-warn"><span class="dot"></span>
        <div>${out.gaps.map((g) => esc(g)).join("<br/>")}</div></div>` : `
      <div class="alert alert-info" style="margin-top:18px"><span class="dot"></span>
        <div>无缺口，全部知识点配题成功。</div></div>`}
    <div class="form-actions">
      <a class="btn btn-secondary btn-sm" href="#/teacher/class">去班级学情看提交</a>
    </div>`;
}

/** 已布置作业列表（右侧卡片）：时间倒序，每页 10 条，带上一页/下一页。 */
function homeworkListHTML(data) {
  const items = data.items || [];
  const total = data.total || 0;
  const page = data.page || 1;
  const pages = Math.max(1, Math.ceil(total / 10));
  if (!items.length) {
    return emptyState("还没有布置过作业", "左边抽题并布置后，这里按时间倒序出现", "book");
  }
  return `
    <div class="list">
      ${items.map((h) => `
        <div class="list-row">
          <div class="grow">
            <div class="title">#${h.id} ${esc(h.title)}</div>
            <div class="meta">${esc(h.class_name)} · ${h.question_count} 题 · 已交 ${h.submitted} 人 · ${dateOf(h.created_at)}</div>
          </div>
          <button class="btn btn-sm btn-secondary" data-hw-open="${h.id}">看提交</button>
        </div>`).join("")}
    </div>
    <div style="display:flex;gap:10px;align-items:center;justify-content:flex-end;margin-top:12px">
      <button class="btn btn-sm btn-secondary" data-hw-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <span class="tiny">第 ${page} / ${pages} 页 · 共 ${total} 份</span>
      <button class="btn btn-sm btn-secondary" data-hw-page="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button>
    </div>`;
}

/* ---------------------------------------------------------------- 班级学情 */
async function renderClass(view, ctx) {
  const classes = await loadClasses();
  if (!classes.length) {
    view.innerHTML = `<div class="page-head"><div class="eyebrow">班级学情 · T-03</div>
      <h1 class="page-title">还没有任教班级</h1></div>
      <div class="card card-pad">${emptyState("数据范围为空", "请管理员在管理后台 → 授课关系里为你添加班级", "users")}</div>`;
    return;
  }
  if (!T.classId || !classes.some((c) => c.id === T.classId)) T.classId = classes[0].id;

  view.innerHTML = `<div class="page-head"><div class="eyebrow">班级学情 · T-03</div>
      <h1 class="page-title">正在取班级数据…</h1></div>
      <div class="skeleton" style="height:180px"></div>`;

  const [summary, mastery] = await Promise.all([
    http.get(`/api/teacher/class/${T.classId}/error-summary`),
    http.get(`/api/teacher/class/${T.classId}/mastery`),
  ]);
  T.summary = summary;
  T.mastery = mastery || [];

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">班级学情 · T-03</div>
      <h1 class="page-title">这个班卡在哪一块</h1>
      <p class="page-sub">按错因聚类的错题分布，取 Top3 薄弱知识点；掌握度来自 BKT，与教研端同源。</p>
    </div>

    <div style="display:flex;gap:12px;align-items:center;margin-bottom:20px;flex-wrap:wrap">
      <select class="select" id="class-pick" style="width:220px">
        ${classes.map((c) => `<option value="${c.id}" ${c.id === T.classId ? "selected" : ""}>${esc(c.grade)}${esc(c.name)}（#${c.id}）</option>`).join("")}
      </select>
      <span class="pill">${T.mastery.length} 个知识点有记录</span>
      <span class="pill pill-red">${(summary.top_weak_kps || []).length} 个 Top 薄弱点</span>
    </div>

    <div class="grid grid-3" style="margin-bottom:18px">
      ${(summary.top_weak_kps || []).length ? summary.top_weak_kps.map((kp, i) => `
        <div class="card stat">
          <div class="stat-label">Top ${i + 1} 薄弱知识点</div>
          <div style="font-size:17px;font-weight:600;margin-top:8px;letter-spacing:-.01em">${esc(kp.name)}</div>
          <div class="stat-value" style="font-size:26px;margin-top:10px">${kp.wrong_count}<small>次错题</small></div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:12px">
            ${Object.entries(kp.error_types || {}).map(([k, v]) => `<span class="pill">${esc(k)} ×${v}</span>`).join("")}
          </div>
        </div>`).join("") : `<div class="card card-pad" style="grid-column:1/-1">
          ${emptyState("还没有错因数据", "学生做几道题、答错后就会有归因记录", "chart")}</div>`}
    </div>

    <div class="grid grid-2" style="align-items:start">
      <section class="card card-pad">
        <div class="card-title">作业提交情况</div>
        <div class="card-sub">输入作业 id 或作业名，查这份作业的全班作答（每页 20 条）</div>
        <div style="display:flex;gap:8px;margin-top:14px">
          <input class="input" id="sub-hw" type="text" placeholder="作业 id 或作业名" style="flex:1"
            value="${T.submissionHwId || ""}" />
          <button class="btn btn-secondary" id="sub-load">查询</button>
        </div>
        <div id="sub-result" style="margin-top:14px">
          ${T.submissions ? submissionsHTML(T.submissions) : '<div class="tiny">未查询</div>'}
        </div>
      </section>

      <section class="card card-pad">
        <div class="card-title">班级掌握度</div>
        <div class="card-sub">按知识点平均 p_known，升序</div>
        <div style="margin-top:14px">
          ${T.mastery.length
            ? `<div class="scroll-list">${barRows(T.mastery.map((m) => ({ ...m, name: kpName(m.kp_id) })), { valueKey: "avg_p_known" })}</div>`
            : emptyState("暂无掌握度数据", "", "chart")}
        </div>
      </section>
    </div>`;

  view.querySelector("#class-pick").onchange = (e) => {
    T.classId = Number(e.target.value);
    T.submissions = null;
    renderClass(view, ctx);
  };

  const loadSubs = async (ref, page = 1) => {
    const key = String(ref ?? "").trim();
    if (!key) { toast("请输入作业 id 或作业名", "err"); return; }
    const btn = view.querySelector("#sub-load");
    const restore = busy(btn, "查询中");
    try {
      // id 和作业名走同一个接口：后端按"纯数字=id、否则按名字模糊匹配"解析；分页每页 20 条
      T.submissionsKeyword = key;
      T.submissions = (await http.get(
        `/api/teacher/homework/${encodeURIComponent(key)}/submissions?page=${page}&size=20`)) || null;
      const box = view.querySelector("#sub-result");
      box.innerHTML = submissionsHTML(T.submissions);
      bindSubPager();
    } catch (err) {
      view.querySelector("#sub-result").innerHTML = errorBox(err.detail || "查询失败");
    } finally {
      restore();
    }
  };
  const bindSubPager = () => {
    view.querySelectorAll("[data-sub-page]").forEach((b) => {
      b.onclick = () => loadSubs(T.submissionsKeyword, Number(b.dataset.subPage));
    });
  };
  view.querySelector("#sub-load").onclick = () => loadSubs(view.querySelector("#sub-hw").value, 1);
  bindSubPager();               // 上次查过的结果若还挂在页面上，翻页按钮照样能用
  if (T.submissionHwId) {          // 从「已布置作业」跳过来：自动查这一次
    loadSubs(T.submissionHwId, 1);
    T.submissionHwId = null;
  }
}

function submissionsHTML(data) {
  const items = (data && data.items) || [];
  const total = (data && data.total) || 0;
  const size = (data && data.size) || 20;
  const page = (data && data.page) || 1;
  const pages = Math.max(1, Math.ceil(total / size));
  const hw = data && data.homework;
  const sum = (data && data.summary) || null;
  if (!total) return `<div class="tiny">暂无作答记录</div>`;
  return `
    <div class="tiny" style="margin-bottom:8px">${hw ? `作业 #${hw.id} ${esc(hw.title)} · ` : ""}共 ${total} 条作答</div>
    ${sum ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      <span class="pill">共 ${sum.question_total} 题</span>
      <span class="pill pill-green">做齐 ${sum.completed} 人</span>
      <span class="pill pill-orange">只做一部分 ${sum.partial} 人</span>
    </div>` : ""}
    <div class="scroll-list" style="max-height:420px">
      <div class="table-wrap"><table class="table">
        <thead><tr><th>学生</th><th>作答</th><th class="num">得分</th><th>判定</th></tr></thead>
        <tbody>
          ${items.map((r) => `
            <tr>
              <td>${esc(r.student)}</td>
              <td>${esc(clip(r.content, 22))}</td>
              <td class="num">${r.score ?? "—"}</td>
              <td>${r.is_correct === null ? '<span class="pill pill-orange">待确认</span>'
                : r.is_correct ? '<span class="pill pill-green">正确</span>'
                : '<span class="pill pill-red">错误</span>'}</td>
            </tr>`).join("")}
        </tbody></table></div>
    </div>
    <div style="display:flex;gap:10px;align-items:center;justify-content:flex-end;margin-top:12px">
      <button class="btn btn-sm btn-secondary" data-sub-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <span class="tiny">第 ${page} / ${pages} 页</span>
      <button class="btn btn-sm btn-secondary" data-sub-page="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button>
    </div>`;
}

function greeting() {
  const h = new Date().getHours();
  if (h < 6) return "夜深了";
  if (h < 11) return "早上好";
  if (h < 14) return "中午好";
  if (h < 18) return "下午好";
  return "晚上好";
}
