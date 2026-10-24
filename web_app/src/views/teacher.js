/* ============================================================================
 * views/teacher.js · 老师端（对齐 app/api/teacher.py）
 *   概览     GET /classes · GET /grade/pending
 *   待批改   POST /grade/{answer_id} → 客观题直判 / 主观题 pending_confirm
 *            POST /grade/{answer_id}/confirm  {action, score, comment}
 *   布置作业 POST /homework/create · GET /homework/{id}/submissions
 *   班级学情 GET /class/{id}/error-summary · GET /class/{id}/mastery
 *
 * 越权口径：所有请求都由后端按 teaching_assignments 校验，
 * 前端只负责把「不在你任教范围」的 403 原文透出来。
 * ========================================================================== */
import {
  http, toast, esc, busy, pct, fixed, dateOf, clip,
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
async function renderGrading(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">自动批改 · T-02</div>
    <h1 class="page-title">正在取待批改列表…</h1></div>
    <div class="skeleton" style="height:160px"></div>`;

  T.pending = (await http.get("/api/teacher/grade/pending")) || [];

  const head = `
    <div class="page-head">
      <div class="eyebrow">自动批改 · T-02</div>
      <h1 class="page-title">客观题即时生效，主观题你说了算</h1>
      <p class="page-sub">客观题由规则直判；主观题走 LangGraph interrupt 挂起，AI 只给建议分与评语，采纳 / 改分 / 驳回之后才写成绩与 BKT。</p>
    </div>`;

  if (!T.pending.length) {
    view.innerHTML = `${head}<div class="card card-pad">${emptyState("没有待批改的作答", "学生提交后会自动出现在这里", "check")}</div>`;
    return;
  }

  view.innerHTML = `${head}
    <div class="muted" style="margin-bottom:14px;font-size:13px">
      共 <span id="pending-count">${T.pending.length}</span> 条 · 点击「AI 批改」开始
    </div>
    <div id="grade-list" style="display:flex;flex-direction:column;gap:14px"></div>`;

  const list = view.querySelector("#grade-list");
  T.pending.forEach((row) => list.appendChild(gradeCard(row, view, ctx)));
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
      ${state ? "" : `<button class="btn btn-sm btn-primary" data-grade="${row.answer_id}">AI 批改</button>`}
    </div>

    <div style="margin-top:14px;padding:14px 16px;background:#fafafb;border-radius:12px">
      <div class="tiny" style="margin-bottom:5px">题目</div>
      <div style="font-size:13.5px;line-height:1.65">${esc(row.question)}…</div>
      <div class="tiny" style="margin:12px 0 5px">学生作答</div>
      <div style="font-size:14px;line-height:1.7;white-space:pre-wrap">${esc(row.content)}</div>
    </div>

    <div data-result style="margin-top:14px"></div>`;

  const resultBox = card.querySelector("[data-result]");

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
    <div style="margin-top:10px;font-size:13.5px;line-height:1.7">${esc(sug.ai_comment || "（无评语）")}</div>
    ${(sug.scoring_basis || []).length ? `<ul class="basis">
      ${sug.scoring_basis.map((b) => `<li>${esc(b)}</li>`).join("")}
    </ul>` : ""}

    <div class="decision">
      <div class="segmented" data-act>
        ${[["confirm", "采纳"], ["edit", "改分"], ["reject", "驳回"]].map(([k, label]) => `
          <button data-val="${k}" class="${decision.action === k ? "active" : ""}">${label}</button>`).join("")}
      </div>
      <input class="input" data-score type="number" step="0.5" min="0"
        max="${sug.full_score ?? 100}" value="${decision.score ?? sug.ai_score ?? 0}"
        style="width:110px;${decision.action === "edit" ? "" : "display:none"}"
        aria-label="最终分数" />
      <span class="spacer" style="flex:1"></span>
      <button class="btn btn-primary" data-confirm>确认生效并回写 BKT</button>
    </div>
    <div class="tiny" style="margin-top:10px">驳回表示该次作答不计入成绩，掌握度不更新。</div>
  </div>`;
}

function bindSuggest(box, row, view, ctx, card) {
  const aid = row.answer_id;
  const decision = T.decisions[aid] || (T.decisions[aid] = { action: "confirm", score: null });

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
  if (!total) return `<div class="tiny">暂无作答记录</div>`;
  return `
    <div class="tiny" style="margin-bottom:8px">${hw ? `作业 #${hw.id} ${esc(hw.title)} · ` : ""}共 ${total} 条作答</div>
    <div class="scroll-list" style="max-height:420px">
      <div class="table-wrap"><table class="table">
        <thead><tr><th>学生</th><th>作答</th><th>得分</th><th>判定</th></tr></thead>
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
