/* ============================================================================
 * views/research.js · 教研组长端（对齐 app/api/research.py）
 *   年级学情 GET /grade/{grade}/mastery?subject=math
 *   班级对比 GET /mastery/compare?grade=&subject=
 *   智能出卷 POST /exam/generate · GET /exam/{id}/quota · POST /exam/{id}/assign
 *   考后分析 POST /paper/{id}/analyze · GET /questions/abnormal
 *
 * 通道纪律（PRD 2.3）：本端只走 aggregate 聚合通道，接口层面没有学生明细。
 * ========================================================================== */
import {
  http, toast, esc, busy, pct, fixed, emptyState, errorBox,
} from "../core.js";
import { kpName } from "../kp-names.js";
import { radar, barRows, barChart } from "../charts.js";

const R = {
  grade: "初三",
  subject: "math",
  grades: ["初一", "初二", "初三"],
  mastery: {},
  compare: {},
  exam: null,
  analysis: null,
  abnormal: null,
};

const DIFF = { easy: "易", medium: "中", hard: "难" };
const QTYPE = { choice: "选择", fill: "填空", subjective: "解答" };

export async function render(view, ctx) {
  if (ctx.tab === "compare") return renderCompare(view, ctx);
  if (ctx.tab === "exam") return renderExam(view, ctx);
  if (ctx.tab === "analysis") return renderAnalysis(view, ctx);
  return renderOverview(view, ctx);
}

/* ---------------------------------------------------------------- 年级学情 */
async function renderOverview(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">教研端</div>
    <h1 class="page-title">正在取年级聚合数据…</h1></div>
    <div class="skeleton" style="height:220px"></div>`;

  const data = R.mastery[R.grade] || (R.mastery[R.grade] =
    await http.get(`/api/research/grade/${encodeURIComponent(R.grade)}/mastery`, { subject: R.subject }));
  const items = data.items || [];
  const avg = items.length ? items.reduce((a, b) => a + b.avg_p_known, 0) / items.length : 0;
  const weak = items.filter((i) => i.avg_p_known < 0.4);

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">教研端 · 年级掌握度 R-01</div>
      <h1 class="page-title">${esc(R.grade)}的整体地基，哪里最松</h1>
      <p class="page-sub">这是"${esc(R.grade)} × 数学"这个二维坐标下的聚合视图：能看到班级之间的差异，但看不到任何学生明细。</p>
    </div>

    <div style="display:flex;gap:12px;align-items:center;margin-bottom:22px;flex-wrap:wrap">
      <div class="segmented" id="grade-switch">
        ${R.grades.map((g) => `<button data-grade="${g}" class="${g === R.grade ? "active" : ""}">${g}</button>`).join("")}
      </div>
      <span class="pill">数学</span>
      <span class="pill">平均 ${pct(avg)}</span>
      <span class="pill pill-red">${weak.length} 个知识点低于 40%</span>
    </div>

    <div class="grid grid-3" style="margin-bottom:22px">
      <div class="card stat">
        <div class="stat-label">知识点覆盖</div>
        <div class="stat-value">${items.length}<small>个</small></div>
        <div class="stat-foot">有 BKT 记录的知识点</div>
      </div>
      <div class="card stat">
        <div class="stat-label">年级平均掌握度</div>
        <div class="stat-value">${pct(avg)}</div>
        <div class="stat-foot">${avg >= 0.6 ? "整体基本掌握" : avg >= 0.4 ? "整体偏薄弱" : "整体严重薄弱"}</div>
      </div>
      <div class="card stat">
        <div class="stat-label">最薄弱知识点</div>
        <div class="stat-value" style="font-size:22px;line-height:1.3">${esc(items[0]?.name || "—")}</div>
        <div class="stat-foot">${items[0] ? `平均 ${pct(items[0].avg_p_known)}` : "暂无数据"}</div>
      </div>
    </div>

    <div class="grid grid-2" style="align-items:start">
      <section class="card card-pad">
        <div class="card-title">年级掌握度雷达</div>
        <div class="card-sub">取最薄弱的 10 个知识点</div>
        <div style="margin-top:10px">
          ${items.length >= 3
            ? radar({ labels: items.slice(0, 10).map((i) => i.name), values: items.slice(0, 10).map((i) => i.avg_p_known) })
            : emptyState("数据还不够", "至少 3 个知识点有记录", "chart")}
        </div>
      </section>

      <section class="card card-pad">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px">
          <div><div class="card-title">最薄弱知识点清单</div>
            <div class="card-sub">升序，可直接作为出卷依据</div></div>
          <a class="btn btn-ghost btn-sm" href="#/research/exam">去出卷</a>
        </div>
        <div style="margin-top:12px">
          ${items.length
            ? barRows(items.map((i) => ({ ...i, p_known: i.avg_p_known })), { limit: 10 })
            : emptyState("暂无数据", "等学生作答并批改后自动聚合", "chart")}
        </div>
      </section>
    </div>`;

  view.querySelectorAll("#grade-switch button").forEach((btn) => {
    btn.onclick = () => {
      R.grade = btn.dataset.grade;
      renderOverview(view, ctx);
    };
  });
}

/* ---------------------------------------------------------------- 班级对比 */
async function renderCompare(view, ctx) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">教研端</div>
    <h1 class="page-title">正在取班级对比…</h1></div><div class="skeleton" style="height:240px"></div>`;

  const data = R.compare[R.grade] || (R.compare[R.grade] =
    await http.get("/api/research/mastery/compare", { grade: R.grade, subject: R.subject }));
  const items = data.items || [];

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">班级横向对比 · R-01</div>
      <h1 class="page-title">同一个年级，班与班的差距有多大</h1>
      <p class="page-sub">聚合口径：每个班在该学科上的平均 p_known。数据与老师端同源，只是这里不做学生明细。</p>
    </div>

    <div style="display:flex;gap:12px;align-items:center;margin-bottom:22px;flex-wrap:wrap">
      <div class="segmented" id="grade-switch">
        ${R.grades.map((g) => `<button data-grade="${g}" class="${g === R.grade ? "active" : ""}">${g}</button>`).join("")}
      </div>
      <span class="pill">${items.length} 个班级</span>
      ${items.length ? `<span class="pill pill-blue">最高 ${pct(items[items.length - 1].avg_p_known)}</span>
        <span class="pill pill-orange">最低 ${pct(items[0].avg_p_known)}</span>` : ""}
    </div>

    <section class="card card-pad" style="margin-bottom:18px">
      <div class="card-title">班级平均掌握度</div>
      <div class="card-sub">柱高即平均 p_known，越低越需要关注</div>
      <div style="margin-top:16px">
        ${items.length ? barChart({ items }) : emptyState("暂无班级数据", "需要有学生作答并完成批改", "chart")}
      </div>
    </section>

    <section class="card card-pad">
      <div class="card-title">明细</div>
      <div class="table-wrap" style="margin-top:12px">
        <table class="table">
          <thead><tr><th>班级</th><th>班级 id</th><th class="num">平均掌握度</th><th class="num">学生数</th><th>水平</th></tr></thead>
          <tbody>
            ${items.map((it) => {
              const v = it.avg_p_known;
              const pill = v >= 0.6 ? "pill-green" : v >= 0.4 ? "pill-orange" : "pill-red";
              const label = v >= 0.85 ? "掌握" : v >= 0.6 ? "基本掌握" : v >= 0.4 ? "薄弱" : "严重薄弱";
              return `<tr>
                <td>${esc(it.class_name)}</td>
                <td class="muted">#${it.class_id}</td>
                <td class="num">${pct(v)}</td>
                <td class="num">${it.student_count}</td>
                <td><span class="pill ${pill}">${label}</span></td>
              </tr>`;
            }).join("") || `<tr><td colspan="5" class="muted">暂无数据</td></tr>`}
          </tbody>
        </table>
      </div>
    </section>`;

  view.querySelectorAll("#grade-switch button").forEach((btn) => {
    btn.onclick = () => { R.grade = btn.dataset.grade; renderCompare(view, ctx); };
  });
}

/* ---------------------------------------------------------------- 智能出卷 R-08 */
async function renderExam(view, ctx) {
  const plan = R.exam;
  /* 布置要选班级，班级列表来自年级对比接口（aggregate 通道里唯一带班级名的地方） */
  if (!R.compare[R.grade]) {
    R.compare[R.grade] = await http.get("/api/research/mastery/compare", { grade: R.grade, subject: R.subject });
  }
  const classes = R.compare[R.grade]?.items || [];

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">智能出卷 · R-08</div>
      <h1 class="page-title">先出策略，再抽题，最后审核</h1>
      <p class="page-sub">命题规划师只决定「选哪些知识点、各配几道」；抽题是 Python 规则，结果可复现；质量审核用硬指标，不达标降难度重抽，最多 2 轮。</p>
    </div>

    <div class="grid grid-2" style="align-items:start">
      <section class="card card-pad">
        <div class="card-title">生成试卷</div>
        <div class="card-sub">月考偏错题巩固，期中/期末偏整体摸底</div>

        <div class="field" style="margin-top:18px">
          <label>考试类型</label>
          <div class="segmented" id="exam-type">
            <button data-val="monthly" class="active">月考（巩固）</button>
            <button data-val="midterm">期中（摸底）</button>
            <button data-val="final">期末（摸底）</button>
          </div>
        </div>

        <div class="form-row" style="margin-top:16px">
          <div class="field">
            <label for="exam-grade">年级</label>
            <select class="select" id="exam-grade">
              ${R.grades.map((g) => `<option ${g === R.grade ? "selected" : ""}>${g}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label for="exam-score">卷面总分</label>
            <input class="input" id="exam-score" type="number" min="10" max="150" value="100" />
          </div>
        </div>

        <div class="form-actions">
          <button class="btn btn-primary" id="exam-generate">开始生成</button>
          <span class="tiny" id="exam-tip">三角色协作，预计 30~90 秒</span>
        </div>
      </section>

      <section class="card card-pad">
        <div class="card-title">当前达成度</div>
        <div class="card-sub">审核指标由 Python 计算，LLM 只负责解释</div>
        <div class="list" style="margin-top:10px">
          <div class="list-row"><div class="grow"><div class="title">月考：错题命中率 ≥ 70%</div>
            <div class="meta">知识点来自年级错题 Top10 及其前置</div></div></div>
          <div class="list-row"><div class="grow"><div class="title">期中/期末：章节覆盖率 ≥ 80%</div>
            <div class="meta">难度：易 70% / 中 20% / 难 10%</div></div></div>
          <div class="list-row"><div class="grow"><div class="title">同一知识点 ≤ 2 题</div>
            <div class="meta">超配额只截断，不乱补题</div></div></div>
        </div>
      </section>
    </div>

    <div id="exam-result" style="margin-top:20px">${plan ? examResultHTML(plan, classes) : ""}</div>`;

  let type = "monthly";
  view.querySelectorAll("#exam-type button").forEach((btn) => {
    btn.onclick = () => {
      type = btn.dataset.val;
      view.querySelectorAll("#exam-type button").forEach((b) => b.classList.toggle("active", b === btn));
    };
  });

  view.querySelector("#exam-generate").onclick = async () => {
    const btn = view.querySelector("#exam-generate");
    const restore = busy(btn, "规划 → 抽题 → 审核…");
    view.querySelector("#exam-tip").textContent = "三角色 crew 正在跑，别关页面…";
    try {
      const out = await http.post("/api/research/exam/generate", {
        exam_type: type,
        grade: view.querySelector("#exam-grade").value,
        subject: R.subject,
        total_score: Number(view.querySelector("#exam-score").value) || 100,
      });
      R.exam = out;
      R.compare[out.grade || R.grade] = null;
      toast(`试卷 #${out.paper_id} 已生成`, "ok");
      renderExam(view, ctx);
    } catch (err) {
      restore();
      view.querySelector("#exam-result").innerHTML = errorBox(err.detail || "生成失败");
    }
  };

  if (plan) bindExamResult(view, ctx, plan, classes);
}

function examResultHTML(exam, classes) {
  const review = exam.review || {};
  const items = exam.plan?.items || [];
  const passed = review.passed;
  const metrics = review.metrics || {};
  const scalar = Object.entries(metrics).filter(([, v]) => typeof v !== "object" || v === null);
  const dists = Object.entries(metrics).filter(([, v]) => v && typeof v === "object");

  return `<section class="card card-pad">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <div class="card-title">试卷 #${exam.paper_id}</div>
      <span class="pill ${passed ? "pill-green" : "pill-orange"}">
        ${passed ? `审核通过 · ${review.rounds_used || 1} 轮` : "审核未通过 · 待人工复核"}
      </span>
      <span class="pill">${exam.question_ids?.length || 0} 题</span>
    </div>
    ${review.summary ? `<div class="alert alert-info" style="margin-top:14px"><span class="dot"></span>
      <div>${esc(review.summary)}</div></div>` : ""}

    <div class="grid grid-4" style="margin-top:18px">
      ${scalar.map(([k, v]) => `<div class="card stat" style="box-shadow:none;background:var(--bg-soft);border-color:transparent">
        <div class="stat-label">${esc(metricLabel(k))}</div>
        <div class="stat-value" style="font-size:26px">${esc(metricValue(v))}</div>
      </div>`).join("")}
    </div>

    <div class="tiny" style="margin:20px 0 8px">配额表 QuotaPlan（可打印给组长人工改）</div>
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>知识点</th><th>章节</th><th class="num">题数</th><th>难度</th><th>题型</th><th>入选理由</th></tr></thead>
        <tbody>
          ${items.map((it) => `<tr>
            <td>${esc(kpName(it.kp_id))}</td>
            <td class="tiny">${esc(kpName(it.chapter_id))}</td>
            <td class="num">${it.question_count}</td>
            <td>${DIFF[it.difficulty] || it.difficulty}</td>
            <td>${QTYPE[it.question_type] || it.question_type}</td>
            <td class="tiny">${esc(it.reason || "")}</td>
          </tr>`).join("") || `<tr><td colspan="6" class="muted">配额表为空</td></tr>`}
        </tbody>
      </table>
    </div>

    ${dists.length ? `<div class="tiny" style="margin:14px 0 6px">分布指标</div>
      <div class="chip-set">
        ${dists.map(([k, v]) => `<span class="chip" style="cursor:default">${esc(metricLabel(k))}
          <span class="kp-id">${esc(metricValue(v))}</span></span>`).join("")}
      </div>` : ""}

    ${(review.problems || []).length ? `<div class="alert alert-warn" style="margin-top:16px">
      <span class="dot"></span>
      <div><b>未通过项</b><br/>${review.problems.map((p) => esc(p)).join("<br/>")}</div></div>` : ""}

    ${(exam.gaps || []).length ? `<div class="alert alert-warn" style="margin-top:16px">
      <span class="dot"></span>
      <div><b>抽题缺口</b><br/>${exam.gaps.map((g) => esc(g)).join("<br/>")}</div></div>` : ""}

    <div class="divider"></div>
    <div class="card-title" style="font-size:15px">定稿并布置</div>
    <div class="card-sub">选择目标班级；布置后由演示脚本合成作答，再回到「考后分析」跑 R-04。</div>
    <div class="chip-set" id="assign-classes" style="margin-top:12px">
      ${(classes || []).map((c) => `<button type="button" class="chip" data-cid="${c.class_id}">${esc(c.class_name)}</button>`).join("")
        || '<span class="tiny">该年级暂无班级数据，请先在管理后台建班</span>'}
    </div>
    <div class="form-actions">
      <button class="btn btn-primary" id="exam-assign">定稿布置</button>
      <span class="tiny">布置后 status → published</span>
    </div>
  </section>`;
}

function bindExamResult(view, ctx, exam, classes) {
  const chosen = new Set();
  view.querySelectorAll("#assign-classes .chip").forEach((chip) => {
    chip.onclick = () => {
      const cid = Number(chip.dataset.cid);
      if (chosen.has(cid)) chosen.delete(cid); else chosen.add(cid);
      chip.classList.toggle("active", chosen.has(cid));
    };
  });

  const btn = view.querySelector("#exam-assign");
  if (!btn) return;
  btn.onclick = async () => {
    if (!chosen.size) { toast("至少选一个班级", "err"); return; }
    const restore = busy(btn, "布置中…");
    try {
      await http.post(`/api/research/exam/${exam.paper_id}/assign`, { class_ids: [...chosen] });
      toast("已定稿 published，可以安排考试了", "ok");
      btn.textContent = "已布置";
      btn.disabled = true;
    } catch (err) {
      restore();
      toast(err.detail || "布置失败", "err");
    }
  };
}

/* ---------------------------------------------------------------- 考后分析 R-04 */
async function renderAnalysis(view, ctx) {
  const ana = R.analysis;
  const abnormal = R.abnormal ?? (R.abnormal = (await http.get("/api/research/questions/abnormal", { subject: R.subject })) || []);

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">考后分析 · R-04</div>
      <h1 class="page-title">上一份卷子的分析，决定下一份卷子</h1>
      <p class="page-sub">Pandas 算得分率 / 难度系数 P / 区分度 D，异常题写回 quality_flag；下次出卷时这些题自动降权或剔除 —— 这是「考后 → 考前」的回流。</p>
    </div>

    <div class="grid grid-2" style="align-items:start;margin-bottom:18px">
      <section class="card card-pad">
        <div class="card-title">触发分析</div>
        <div class="card-sub">需要该试卷已有已判分的作答数据</div>
        <div style="display:flex;gap:8px;margin-top:16px">
          <input class="input" id="ana-paper" type="number" min="1" placeholder="试卷 id（exam_papers.id）" />
          <button class="btn btn-primary" id="ana-run">开始分析</button>
        </div>
        <div class="tiny" style="margin-top:10px">流程：Pandas 算指标 → 数据分析师 → 命题质量专家 → 教研组长汇总</div>
      </section>

      <section class="card card-pad">
        <div class="card-title">历史异常题</div>
        <div class="card-sub">R-04 写回、R-08 消费的同一批题</div>
        <div class="table-wrap" style="margin-top:12px;max-height:210px;overflow:auto">
          <table class="table">
            <thead><tr><th>题号</th><th>知识点</th><th>难度</th><th>题干</th></tr></thead>
            <tbody>
              ${abnormal.map((q) => `<tr>
                <td>#${q.id}</td>
                <td class="tiny">${esc(kpName(q.kp_id))}</td>
                <td>${DIFF[q.difficulty] || q.difficulty}</td>
                <td class="muted">${esc(q.content || "")}…</td>
              </tr>`).join("") || `<tr><td colspan="4" class="muted">暂无异常题：先对一份有作答的试卷跑 R-04</td></tr>`}
            </tbody>
          </table>
        </div>
      </section>
    </div>

    <div id="ana-result">${ana ? analysisHTML(ana) : ""}</div>`;

  view.querySelector("#ana-run").onclick = async () => {
    const pid = Number(view.querySelector("#ana-paper").value);
    if (!pid) { toast("请输入试卷 id", "err"); return; }
    const btn = view.querySelector("#ana-run");
    const restore = busy(btn, "分析中…");
    try {
      const out = await http.post(`/api/research/paper/${pid}/analyze`);
      R.analysis = out;
      view.querySelector("#ana-result").innerHTML = analysisHTML(out);
      R.abnormal = null;
      toast("分析完成，异常题已写回", "ok");
      view.querySelector("#ana-result").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      view.querySelector("#ana-result").innerHTML = errorBox(err.detail || "分析失败");
    } finally {
      restore();
    }
  };
}

function analysisHTML(ana) {
  const report = ana.report || {};
  const metrics = ana.metrics || [];
  const abnormal = metrics.filter((m) => m.is_abnormal);

  return `
    <section class="card card-pad" style="margin-bottom:16px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <div class="card-title">试卷 #${ana.paper_id} 分析报告</div>
        <span class="pill pill-blue">${metrics.length} 题</span>
        <span class="pill pill-red">${abnormal.length} 道异常题</span>
        <span class="pill pill-green">已写回 ${ana.abnormal_written ?? 0} 道</span>
      </div>
      ${report.overview ? `<div class="alert alert-info" style="margin-top:14px"><span class="dot"></span>
        <div>${esc(report.overview)}</div></div>` : ""}

      <div class="tiny" style="margin:18px 0 8px">逐题指标（P 越低越难，D 越低区分度越差）</div>
      <div class="table-wrap" style="max-height:320px;overflow:auto">
        <table class="table">
          <thead><tr><th>题号</th><th>知识点</th><th class="num">得分率</th><th class="num">难度 P</th><th class="num">区分度 D</th><th>判定</th></tr></thead>
          <tbody>
            ${metrics.map((m) => `<tr>
              <td>#${m.q_id}</td>
              <td class="tiny">${esc(kpName(m.kp_id))}</td>
              <td class="num">${pct(m.score_rate)}</td>
              <td class="num">${fixed(m.P, 2)}</td>
              <td class="num">${fixed(m.D, 2)}</td>
              <td>${m.is_abnormal
                ? `<span class="pill pill-red">${(m.issues || []).map(esc).join(" · ") || "异常"}</span>`
                : '<span class="pill pill-green">正常</span>'}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </div>
    </section>

    <div class="grid grid-2" style="align-items:start">
      <section class="card card-pad">
        <div class="card-title">异常题诊断卡</div>
        <div class="card-sub">CrewAI 三角色解读成因</div>
        <div class="list" style="margin-top:10px">
          ${(report.abnormal_items || []).map((it) => `
            <div class="list-row" style="align-items:flex-start">
              <span class="pill pill-red">#${it.q_id}</span>
              <div class="grow">
                <div class="title" style="font-size:13.5px">${esc(it.issue || "")}</div>
                <div class="meta" style="line-height:1.65">P=${fixed(it.P, 2)} · D=${fixed(it.D, 2)} · ${esc(kpName(it.kp_id))}<br/>${esc(it.cause || "")}</div>
              </div>
            </div>`).join("") || emptyState("没有异常题", "指标全部落在正常区间", "check")}
        </div>
      </section>

      <section class="card card-pad">
        <div class="card-title">教研建议</div>
        <div class="card-sub">可直接排进课表</div>
        <div class="list" style="margin-top:10px">
          ${(report.teaching_suggestions || []).map((s) => `
            <div class="list-row" style="align-items:flex-start">
              <span class="pill pill-green">建议</span>
              <div class="grow" style="font-size:13.5px;line-height:1.7">${esc(s)}</div>
            </div>`).join("") || emptyState("暂无建议", "", "sparkle")}
        </div>
        <div class="divider"></div>
        <div class="tiny" style="line-height:1.9">
          这些异常题已写回 questions.quality_flag = abnormal，<br/>
          下一次「智能出卷」抽题时会自动降权 50% 或直接剔除。
        </div>
      </section>
    </div>`;
}

/* ---------------------------------------------------------------- 小工具 */
function metricLabel(key) {
  return {
    hit_rate: "错题命中率",
    chapter_coverage: "章节覆盖率",
    per_kp_max: "单知识点最多题数",
    total_questions: "总题量",
    diff_dist: "难度分布",
    type_dist: "题型分布",
  }[key] || key;
}

function metricValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? v : fixed(v, 2);
  if (typeof v === "object") {
    return Object.entries(v)
      .map(([k, val]) => `${DIFF[k] || QTYPE[k] || k} ${typeof val === "number" ? fixed(val, 2) : val}`)
      .join(" / ");
  }
  return String(v);
}
