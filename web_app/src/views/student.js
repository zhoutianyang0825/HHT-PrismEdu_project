/* ============================================================================
 * views/student.js · 学生端（对齐 app/api/student.py）
 *   概览     GET /api/student/homework · GET /api/student/mastery/{sid}
 *   学习助手 POST /api/student/answer/stream · /resume      （SSE + interrupt）
 *   诊断测验 POST /api/student/quiz/generate · /quiz/{id}/submit
 *   掌握度   GET /api/student/mastery/{sid}?granularity=kp|chapter
 *
 * interrupt 状态机：某题收到 interrupt ⇒ 该题 awaiting=true，
 * 下一次输入走 /resume，否则走 /answer/stream（后端另有兜底，双保险）。
 * ========================================================================== */
import {
  API_BASE, http, streamChat, toast, esc, md, busy, pct, fixed, dateOf, clip, greeting,
  levelOf, emptyState, errorBox, shortName, modal,
} from "../core.js";
import { radar, barRows } from "../charts.js";

const S = {
  homework: null,
  hw: null,
  questions: {},        // homework_id -> Question[]
  progress: {},         // homework_id -> {total, answered, correct, items:[{question_id,is_correct}]}
  q: null,              // 当前题目
  chats: {},            // question_id -> [{kind, text, meta}]
  awaiting: {},         // question_id -> bool
  pendingOpen: null,    // 从概览点「去作答」带过来的 homework_id
  quiz: null,
  quizAnswers: {},
  quizResults: null,
  mastery: { kp: null, chapter: null },
  gran: "kp",
  streaming: false,
  // ---- 考试 M7 ----
  exams: null,          // 考试列表（待考 + 记录）
  exam: null,           // 当前打开的那份卷（含题目）
  examAnswers: {},      // question_id -> {content, attachments[]}（草稿，存内存+localStorage）
  examResult: null,     // 交卷接口的返回
  examUploading: {},    // question_id -> true，图片上传中
};

const DIFF = { easy: "基础", medium: "中等", hard: "较难" };
const QTYPE = { choice: "选择题", fill: "填空题", subjective: "解答题" };

/* ---------------------------------------------------------------- 入口 */
export async function render(view, ctx) {
  if (!ctx.me.student_id) {
    view.innerHTML = `<div class="page-head">
        <div class="eyebrow">学生端</div>
        <h1 class="page-title">还没有学生档案</h1>
        <p class="page-sub">当前账号带 student 角色，但 students 表里没有对应档案，请联系管理员补建。</p>
      </div>
      ${errorBox("学生档案缺失：登录返回的 student_id 为空")}`;
    return;
  }
  if (ctx.tab === "assistant") return renderAssistant(view, ctx);
  if (ctx.tab === "quiz") return renderQuiz(view, ctx);
  if (ctx.tab === "exam") return renderExam(view, ctx);
  if (ctx.tab === "mastery") return renderMastery(view, ctx);
  return renderOverview(view, ctx);
}

/* ---------------------------------------------------------------- 概览 */
async function renderOverview(view, ctx) {
  const sid = ctx.me.student_id;
  view.innerHTML = `<div class="page-head"><div class="eyebrow">学生端</div>
      <h1 class="page-title">正在整理你的学情…</h1></div>
      <div class="grid grid-3">${skelCards(3)}</div>`;

  const [homework, mastery] = await Promise.all([
    http.get("/api/student/homework"),
    http.get(`/api/student/mastery/${sid}`, { granularity: "kp" }),
  ]);
  S.homework = homework || [];
  S.mastery.kp = mastery;

  const items = mastery.items || [];
  const avg = items.length ? items.reduce((a, b) => a + b.p_known, 0) / items.length : 0;
  const weak = items.filter((i) => i.p_known < 0.4);

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">学生端 · 学情概览</div>
      <h1 class="page-title">${greeting()}，${esc(ctx.me.name)}</h1>
      <p class="page-sub">这三块数字都来自同一份 BKT 掌握度：做题 → 批改 → 贝叶斯更新 → 反向决定该练什么。</p>
    </div>

    <div class="grid grid-3" style="margin-bottom:26px">
      <div class="card stat">
        <div class="stat-label">平均掌握度</div>
        <div class="stat-value">${pct(avg)}</div>
        <div class="stat-foot">覆盖 ${items.length} 个知识点${items.length ? ` · ${levelOf(avg).text}` : ""}</div>
      </div>
      <div class="card stat">
        <div class="stat-label">严重薄弱知识点</div>
        <div class="stat-value">${weak.length}<small>个</small></div>
        <div class="stat-foot">掌握度低于 40%，会优先进入诊断测验</div>
      </div>
      <div class="card stat">
        <div class="stat-label">待作答作业</div>
        <div class="stat-value">${S.homework.length}<small>份</small></div>
        <div class="stat-foot">点进去可逐题开始引导式作答</div>
      </div>
    </div>

    <div class="grid grid-2">
      <section class="card card-pad">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px">
          <div><div class="card-title">最薄弱的六个知识点</div>
          <div class="card-sub">按掌握度升序，越低越该先补</div></div>
          <a class="btn btn-ghost btn-sm" href="#/student/mastery">全部</a>
        </div>
        <div style="margin-top:16px">
          ${items.length ? barRows(items, { limit: 6 }) : emptyState("还没有掌握度数据", "先在学习助手里做几道题", "chart")}
        </div>
      </section>

      <section class="card card-pad">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px">
          <div><div class="card-title">我的作业</div>
          <div class="card-sub">老师布置后即时可见</div></div>
          <a class="btn btn-ghost btn-sm" href="#/student/assistant">去作答</a>
        </div>
        <div class="list" style="margin-top:6px">
          ${S.homework.length ? S.homework.slice(0, 5).map((h) => `
            <div class="list-row">
              <div class="grow">
                <div class="title">${esc(h.title)}</div>
                <div class="meta">作业 #${h.id} · ${dateOf(h.created_at)} · ${kpCount(h.kp_ids)} 个知识点</div>
              </div>
              <button class="btn btn-sm btn-secondary" data-open-hw="${h.id}">开始</button>
            </div>`).join("") : emptyState("暂无作业", "老师布置后会出现在这里", "book")}
        </div>
      </section>
    </div>`;

  view.querySelectorAll("[data-open-hw]").forEach((btn) => {
    btn.onclick = () => {
      const hid = Number(btn.dataset.openHw);
      S.hw = S.homework.find((h) => h.id === hid) || null;
      S.q = null;
      S.pendingOpen = hid;
      location.hash = "#/student/assistant";
    };
  });
}

/* ---------------------------------------------------------------- 学习助手 */
async function renderAssistant(view, ctx) {
  const sid = ctx.me.student_id;

  view.innerHTML = `<div class="page-head compact"><div class="eyebrow">学习助手 · S-04</div>
      <h1 class="page-title">不给答案，带你想出来</h1>
      <p class="page-sub">AI 先给一层提示，你再试一次；连续三轮仍卡住才给完整讲解。</p>
    </div>
    <div class="assistant-grid">
      <aside class="card hw-panel"><div class="card-hd"><div class="card-title">我的作业</div></div>
        <div class="hw-list"><div class="skeleton" style="height:60px;margin:8px 4px"></div></div></aside>
      <section class="card chat-card"><div class="chat-scroll"></div></section>
    </div>`;

  if (!S.homework) S.homework = (await http.get("/api/student/homework")) || [];

  /* 从概览带过来的作业：自动展开并选第一题 */
  if (S.pendingOpen) {
    const hid = S.pendingOpen;
    S.pendingOpen = null;
    if (!S.hw) S.hw = S.homework.find((h) => h.id === hid) || null;
    if (S.hw) {
      await ensureQuestions(hid);
      if (!S.q) S.q = (S.questions[hid] || [])[0] || null;
    }
  }

  renderHwPanel(view, ctx);
  renderChat(view, ctx);
}

/** 题号右侧的作答状态标记：✓ 对 / ✗ 错 / … 待批改 / 空 = 还没做 */
function qMark(prog, qid) {
  const it = (prog?.items || []).find((x) => x.question_id === qid);
  if (!it) return `<span class="q-mark"></span>`;
  if (it.is_correct === true) return `<span class="q-mark ok">✓</span>`;
  if (it.is_correct === false) return `<span class="q-mark bad">✗</span>`;
  return `<span class="q-mark wait">…</span>`;
}

function renderHwPanel(view, ctx) {
  const panel = view.querySelector(".hw-panel");
  const hwList = S.homework || [];

  // 重写 innerHTML 会让两个滚动位置归零：左侧列表和自己 + 整个页面。
  // 先记下来，画完再放回去——不然"点一下题目，滚动条往上窜"。
  const oldList = panel.querySelector(".hw-list");
  const keepListTop = oldList ? oldList.scrollTop : 0;
  const keepPageTop = window.scrollY;

  panel.innerHTML = `
    <div class="card-hd">
      <div>
        <div class="card-title">我的作业</div>
        <div class="card-sub">选作业 → 选题目 → 开始对话</div>
      </div>
      <span class="pill" style="margin-left:auto">${hwList.length}</span>
    </div>
    <div class="hw-list">
      ${hwList.length ? hwList.map((h) => {
        const active = S.hw?.id === h.id;
        const qs = S.questions[h.id] || [];
        const prog = S.progress[h.id];
        return `
          <button class="hw-item ${active ? "active" : ""}" data-hw="${h.id}">
            <div class="title">${esc(h.title)}</div>
            <div class="meta">#${h.id} · ${kpCount(h.kp_ids)} 个知识点 · ${dateOf(h.created_at)}</div>
            ${prog && prog.total ? `<div class="meta">已完成 ${prog.answered}/${prog.total}${prog.correct ? ` · 对 ${prog.correct}` : ""}</div>` : ""}
          </button>
          ${active ? `<div class="q-list">${
            qs.length ? qs.map((q, i) => `
              <button class="q-item ${S.q?.id === q.id ? "active" : ""}" data-q="${q.id}">
                <span class="idx">${String(i + 1).padStart(2, "0")}</span>
                <span class="txt">${esc(clip(q.content, 44))}</span>
                ${qMark(prog, q.id)}
              </button>`).join("")
              : `<div class="tiny" style="padding:8px 12px">这份作业还没有题目</div>`
          }</div>` : ""}`;
      }).join("") : `<div class="tiny" style="padding:14px 12px;line-height:1.8">
          暂无作业。老师布置后这里会出现题目，<br/>也可以直接去做「诊断测验」。</div>`}
    </div>`;

  // 还原滚动位置（放在事件绑定之前：绑定不改布局，先还原更稳）
  const newList = panel.querySelector(".hw-list");
  if (newList) newList.scrollTop = keepListTop;
  if (window.scrollY !== keepPageTop) window.scrollTo({ top: keepPageTop });

  // 展开的作业：拉一次完成进度（真值在库里，刷新页面也不会丢）
  // 没缓存才拉，拉回来再重绘一次 —— 不阻塞首屏，也不会无限递归
  if (S.hw && !S.progress[S.hw.id]) {
    ensureProgress(S.hw.id)
      .then(() => renderHwPanel(view, ctx))
      .catch(() => {});
  }

  panel.querySelectorAll("[data-hw]").forEach((btn) => {
    btn.onclick = async () => {
      const hid = Number(btn.dataset.hw);
      if (S.hw?.id === hid) {
        S.hw = null; S.q = null;
        renderHwPanel(view, ctx); renderChat(view, ctx);
        return;
      }
      S.hw = S.homework.find((h) => h.id === hid) || null;
      S.q = null;
      renderHwPanel(view, ctx);
      await ensureQuestions(hid);
      S.q = (S.questions[hid] || [])[0] || null;
      renderHwPanel(view, ctx);
      renderChat(view, ctx);
    };
  });

  panel.querySelectorAll("[data-q]").forEach((btn) => {
    btn.onclick = () => {
      const qid = Number(btn.dataset.q);
      S.q = (S.questions[S.hw.id] || []).find((q) => q.id === qid) || null;

      // ★ 只切选中态，不重建面板：
      //   renderHwPanel 会重写整个 .hw-panel 的 innerHTML，
      //   每次做完浏览器都把 .hw-list 的滚动位置归零、页面滚动位置也漂一下——
      //   表现就是"点靠下的题目，右边滚动条突然往上窜"。
      //   这里 DOM 结构没变（还是同一份题目列表），只需要换 active 类。
      panel.querySelectorAll(".q-item").forEach((b) => {
        b.classList.toggle("active", Number(b.dataset.q) === qid);
      });

      renderChat(view, ctx);
    };
  });
}

function renderChat(view, ctx) {
  const card = view.querySelector(".chat-card");
  const q = S.q;

  if (!q) {
    card.innerHTML = `
      <div class="chat-scroll">
        <div class="chat-intro">
          <div class="glyph">${'<svg viewBox="0 0 20 20" width="24" height="24" fill="none" stroke="#0071e3" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M17 11a6.5 6.5 0 0 1-6.5 6.5H6l-3 2v-4.2A6.5 6.5 0 0 1 10.5 4.5 6.5 6.5 0 0 1 17 11z"/></svg>'}</div>
          <h3>从左边选一道题开始</h3>
          <p class="muted" style="font-size:13.5px">这里是引导式作答：不会直接给答案，而是先给一层提示，让你自己走下一步。</p>
          <ul>
            <li>问「这题答案是多少」→ 只给阶梯提示</li>
            <li>提交作答 → 立即判分并更新 BKT 掌握度</li>
            <li>答错 → 可以做错因归因，再推一道同类题</li>
          </ul>
        </div>
      </div>`;
    return;
  }

  const chats = S.chats[q.id] || [];
  card.innerHTML = `
    <div class="chat-hd">
      <span class="pill pill-blue">${QTYPE[q.question_type] || q.question_type}</span>
      <span class="pill">${DIFF[q.difficulty] || q.difficulty}</span>
      <div class="stem" title="${esc(q.content)}">${esc(q.content)}</div>
      <span class="pill ${S.awaiting[q.id] ? "pill-orange" : ""}" style="margin-left:auto">
        ${S.awaiting[q.id] ? "等待你的回应" : "同一题同一会话"}
      </span>
    </div>
    <div class="chat-scroll" id="chat-scroll"></div>
    <form class="composer" id="composer">
      <input class="input" id="chat-input" maxlength="2000" autocomplete="off" aria-label="给学习助手发消息"
             placeholder="${S.awaiting[q.id] ? "回应上面的提示…" : "说说你的思路，或问「这题考什么」"}" />
      <button class="send" type="submit" aria-label="发送">
        <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor"
             stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M10 16V4M5 9l5-5 5 5"/></svg>
      </button>
    </form>`;

  const scroll = card.querySelector("#chat-scroll");
  if (!chats.length) {
    scroll.innerHTML = `<div class="msg msg-ai">
        <span class="avatar">棱</span>
        <div class="bubble">这道题我陪你做。先说说你想到哪一步了？<br/>如果完全没思路，就问「这题考什么」，我给你一点提示。</div>
      </div>`;
  } else {
    chats.forEach((m) => scroll.appendChild(messageNode(m)));
  }
  scrollToBottom(scroll);

  const form = card.querySelector("#composer");
  const input = card.querySelector("#chat-input");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || S.streaming) return;
    input.value = "";
    await sendMessage(view, ctx, q, text, scroll);
  };
}

async function sendMessage(view, ctx, q, text, scroll) {
  S.streaming = true;
  const qid = q.id;
  const chats = S.chats[qid] || (S.chats[qid] = []);
  chats.push({ kind: "user", text });
  scroll.appendChild(messageNode({ kind: "user", text }));
  scrollToBottom(scroll);

  const typing = document.createElement("div");
  typing.className = "typing";
  typing.innerHTML = `<i></i><i></i><i></i>
    <span style="font-size:11.5px;color:#86868b;margin-left:5px" id="node-tip">正在思考</span>`;
  scroll.appendChild(typing);
  scrollToBottom(scroll);

  const tip = typing.querySelector("#node-tip");
  const path = S.awaiting[qid] ? "/api/student/resume" : "/api/student/answer/stream";

  // homework_id 必须带上：后端判分落库靠它决定 source_type
  //   有值 → source_type='homework'，老师端「作业提交情况」才查得到
  //   不带 → 落成 'chat'，老师端永远是"暂无作答记录"
  const payload = {
    student_id: ctx.me.student_id,
    question_id: qid,
    message: text,
    homework_id: S.hw?.id ?? null,
  };

  await streamChat(path, payload, (type, data) => {
    if (type === "node") {
      tip.textContent = NODE_TIP[data.node] || "正在思考";
    } else if (type === "interrupt") {
      typing.remove();
      S.awaiting[qid] = true;
      const msg = { kind: "hint", text: data.hint || "", meta: data.attempt_count };
      chats.push(msg);
      scroll.appendChild(messageNode(msg));
      scrollToBottom(scroll);
    } else if (type === "message") {
      typing.remove();
      S.awaiting[qid] = false;
      const msg = { kind: "ai", text: data.content || "" };
      chats.push(msg);
      scroll.appendChild(messageNode(msg));
      scrollToBottom(scroll);
    } else if (type === "error") {
      typing.remove();
      const msg = { kind: "error", text: data.message || "出错了" };
      chats.push(msg);
      scroll.appendChild(messageNode(msg));
      scrollToBottom(scroll);
    }
  });

  typing.remove();
  S.streaming = false;

  // ---- 作业场景：判完分后端已实时落库，这里把进度拉回来重绘 ✓/✗ 与完成度 ----
  // 为什么答完就刷、而不是等"统一提交"：
  //   作答一旦入库就不会因为关网页/断网丢失，进度条也才能真实反映"做到第几题"
  if (S.hw) {
    try {
      const prog = await ensureProgress(S.hw.id, true);
      renderHwPanel(view, ctx);

      // 全部答完：补一条明确反馈，避免"做完了却不知道老师那边有没有收到"
      if (prog.total && prog.answered >= prog.total && chats[chats.length - 1]?.kind !== "done") {
        chats.push({
          kind: "done",
          text: `这套作业 ${prog.answered}/${prog.total} 题都已作答并同步给老师，其中做对 ${prog.correct} 题。`,
        });
      }
    } catch {
      // 进度拉不到不影响聊天，下次进页面还会再拉
    }
  }

  renderChat(view, ctx);
}

const NODE_TIP = {
  safety_check: "内容安全检查",
  classify_intent: "判断你的意图",
  guide_scaffold: "准备下一层提示",
  grade_submit: "正在判分",
  explain_concept: "组织讲解",
  error_attribution: "分析错因",
  respond: "整理回复",
};

function messageNode(m) {
  const node = document.createElement("div");
  if (m.kind === "done") {
    // 作业全部答完的收尾提示：居中浅色条，不抢对话主线的注意力
    node.className = "chat-done";
    node.innerHTML = `
      <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor"
        stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 10.5l4 4 8-8"/></svg>
      <span>${esc(m.text)}</span>`;
    return node;
  }
  if (m.kind === "hint") {
    node.className = "hint-card";
    node.innerHTML = `
      <div class="cap">
        <svg viewBox="0 0 20 20" width="13" height="13" fill="none" stroke="currentColor"
          stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 16h12M6 16V9M10 16V5M14 16v-4"/></svg>
        引导提示${m.meta ? ` · 第 ${m.meta} 次` : ""}
      </div>
      <div class="body md">${md(m.text)}</div>`;
    return node;
  }
  if (m.kind === "error") {
    node.className = "msg msg-ai msg-error";
    node.innerHTML = `<span class="avatar">!</span><div class="bubble">${esc(m.text)}</div>`;
    return node;
  }
  if (m.kind === "user") {
    node.className = "msg msg-user";
    node.innerHTML = `<div class="bubble">${esc(m.text)}</div>`;
    return node;
  }
  node.className = "msg msg-ai";
  node.innerHTML = `<span class="avatar">棱</span><div class="bubble md">${md(m.text)}</div>`;
  return node;
}

function scrollToBottom(node) {
  requestAnimationFrame(() => { node.scrollTop = node.scrollHeight; });
}

async function ensureQuestions(homeworkId) {
  if (S.questions[homeworkId]) return;
  const data = await http.get(`/api/student/homework/${homeworkId}/questions`);
  S.questions[homeworkId] = data.questions || [];
}

/** 该生在这份作业上的完成进度（逐题 ✓/✗ + 已答/总题数）。
 *
 *  为什么从后端拉、不在前端自己数：
 *    作答是判完分就实时落库的，前端内存只是个显示缓存；
 *    学生在手机上做几题、回来用电脑接着做，只有后端那份进度是准的。
 */
async function ensureProgress(homeworkId, force = false) {
  if (S.progress[homeworkId] && !force) return S.progress[homeworkId];
  const data = await http.get(`/api/student/homework/${homeworkId}/progress`);
  S.progress[homeworkId] = data || { total: 0, answered: 0, correct: 0, items: [] };
  return S.progress[homeworkId];
}

/* ---------------------------------------------------------------- 诊断测验 S-10 */
async function renderQuiz(view, ctx) {
  const sid = ctx.me.student_id;
  if (!S.mastery.kp) S.mastery.kp = await http.get(`/api/student/mastery/${sid}`, { granularity: "kp" });

  const nameOf = kpNameMap(S.mastery.kp);
  const quiz = S.quiz;

  const head = `
    <div class="page-head">
      <div class="eyebrow">诊断测验 · S-10</div>
      <h1 class="page-title">只练你真正弱的地方</h1>
      <p class="page-sub">近 30 天错题知识点 → 沿图谱向前追溯前置知识点 → 按 BKT 掌握度升序取 Top5 → 逐点配题，共 8 题。题目难度与掌握度反相关。</p>
    </div>`;

  /* 未生成 */
  if (!quiz) {
    view.innerHTML = `${head}
      <div class="grid grid-2">
        <section class="card card-pad">
          <div class="card-title">生成一套 8 题的补救测验</div>
          <div class="card-sub">同一知识点不超过 2 题；题库不足时宁可少题也不乱补。</div>
          <div class="list" style="margin-top:16px">
            ${[
              ["取错题", "近 30 天 error_logs 里你错过的知识点"],
              ["图谱外扩", "沿 HAS_PREREQUISITE 向前找 1~2 跳前置"],
              ["按 BKT 排序", "掌握度升序取 Top5，同分看错题频次"],
              ["逐点配题", "掌握度越低越给基础题，共 8 题"],
            ].map(([t, d], i) => `
              <div class="list-row">
                <span class="pill pill-blue">0${i + 1}</span>
                <div class="grow"><div class="title">${t}</div><div class="meta">${d}</div></div>
              </div>`).join("")}
          </div>
          <div style="margin-top:18px">
            <button class="btn btn-primary btn-lg" id="gen-quiz">生成诊断测验</button>
          </div>
        </section>
        <section class="card card-pad">
          <div class="card-title">为什么要做诊断测验</div>
          <div class="card-sub">单题归因是即时反馈，诊断测验是一次横向铺开的系统性补救。</div>
          <ul class="muted" style="margin:16px 0 0;padding-left:18px;font-size:13.5px;line-height:2">
            <li>目标知识点全部来自「错题点 ∪ 其图谱前置」</li>
            <li>错得越多、掌握度越低的点排得越前</li>
            <li>作答结果回写 BKT，下一轮测验会跟着变</li>
          </ul>
        </section>
      </div>`;

    const btn = view.querySelector("#gen-quiz");
    btn.onclick = async () => {
      const restore = busy(btn, "正在按 BKT 配题…");
      try {
        const out = await http.post("/api/student/quiz/generate");
        S.quiz = out;
        S.quizAnswers = {};
        S.quizResults = null;
        toast(out.quiz_id ? "测验已生成" : "暂时生成不了", out.quiz_id ? "ok" : "err");
        renderQuiz(view, ctx);
      } catch (err) {
        restore();
        toast(err.detail || "生成失败", "err");
      }
    };
    return;
  }

  /* 生成失败（没有错题记录等） */
  if (!quiz.quiz_id) {
    view.innerHTML = `${head}
      <div class="card card-pad">
        <div class="alert alert-warn"><span class="dot"></span>
          <div>${esc(quiz.generated_reason || "当前没有可用于出题的错题记录")}</div></div>
        <div class="form-actions">
          <button class="btn btn-secondary" id="regen">重新生成</button>
          <a class="btn btn-primary" href="#/student/assistant">去做题积累错题</a>
        </div>
      </div>`;
    view.querySelector("#regen").onclick = () => { S.quiz = null; renderQuiz(view, ctx); };
    return;
  }

  const questions = quiz.questions || [];
  const answered = questions.filter((q) => (S.quizAnswers[q.id] || "").trim()).length;
  const results = S.quizResults;

  view.innerHTML = `${head}
    <div class="card card-pad" style="margin-bottom:18px">
      <div style="display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap">
        <div class="grow" style="flex:1;min-width:260px">
          <div class="alert alert-info"><span class="dot"></span>
            <div>${esc(quiz.generated_reason || "根据你的薄弱知识点生成了这套测验")}</div></div>
        </div>
        <div style="text-align:right">
          <div class="tiny">测验 #${quiz.quiz_id} · ${questions.length} 题</div>
          <div class="tiny" id="quiz-count">已作答 ${answered}/${questions.length}</div>
        </div>
      </div>
      <div style="margin-top:16px">
        <div class="tiny" style="margin-bottom:8px">目标知识点与出题理由</div>
        <div class="chip-set">
          ${(quiz.target_points || []).map((tp) => `
            <span class="chip" style="cursor:default">
              ${esc(nameOf[tp.kp_id] || shortName(tp.kp_id, 12))}
              <span class="kp-id">${pct(tp.p_known)} · ${esc(tp.reason || "")}</span>
            </span>`).join("") || '<span class="tiny">—</span>'}
        </div>
      </div>
    </div>

    <div id="quiz-body">
      ${questions.map((q, i) => quizQuestionHTML(q, i, nameOf)).join("")}
    </div>

    ${results ? resultPanelHTML(results, questions) : `
      <div class="card card-pad" style="margin-top:18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap">
        <div class="muted" style="font-size:13.5px">客观题当场出分；主观题 AI 只给建议，等老师确认后才写入成绩。</div>
        <div class="spacer" style="flex:1"></div>
        <button class="btn btn-primary" id="submit-quiz" ${answered === 0 ? "disabled" : ""}>
          提交整套测验
        </button>
      </div>`}`;

  bindQuizInputs(view, ctx);

  const again = view.querySelector("#again-quiz");
  if (again) {
    again.onclick = () => {
      S.quiz = null;
      S.quizAnswers = {};
      S.quizResults = null;
      renderQuiz(view, ctx);
    };
  }

  const submit = view.querySelector("#submit-quiz");
  if (submit) {
    submit.onclick = async () => {
      const answers = Object.entries(S.quizAnswers)
        .filter(([, v]) => String(v || "").trim())
        .map(([qid, content]) => ({ question_id: Number(qid), content: String(content).trim() }));
      if (!answers.length) { toast("至少作答一题", "err"); return; }
      const restore = busy(submit, "批改中…");
      try {
        const out = await http.post(`/api/student/quiz/${quiz.quiz_id}/submit`, { answers });
        S.quizResults = out;
        S.mastery.kp = null;
        toast(`客观题已出分 ${out.confirmed_n} 题，主观题 ${out.pending_n} 题待老师确认`, "ok");
        renderQuiz(view, ctx);
        requestAnimationFrame(() => {
          view.querySelector("#quiz-result")?.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      } catch (err) {
        restore();
        toast(err.detail || "提交失败", "err");
      }
    };
  }
}

function quizQuestionHTML(q, i, nameOf) {
  const opts = Array.isArray(q.options) ? q.options : null;
  const chosen = S.quizAnswers[q.id] || "";
  return `<div class="q-card" data-qcard="${q.id}">
    <div class="q-hd">
      <span class="q-no">第 ${i + 1} 题</span>
      <span class="pill">${DIFF[q.difficulty] || q.difficulty}</span>
      <span class="pill">${nameOf[q.kp_id] ? esc(nameOf[q.kp_id]) : esc(q.kp_id)}</span>
    </div>
    <div class="q-text">${esc(q.content)}</div>
    ${opts ? `<div class="options" role="radiogroup">
      ${opts.map((opt) => {
        const letter = String(opt).trim()[0];
        return `<button type="button" class="option ${chosen === letter ? "selected" : ""}"
            data-qid="${q.id}" data-val="${esc(letter)}" role="radio"
            aria-checked="${chosen === letter}">
            <span class="letter">${esc(letter)}</span>
            <span>${esc(opt)}</span>
          </button>`;
      }).join("")}
    </div>` : `<div style="margin-top:14px">
      ${q.question_type === "subjective"
        ? `<textarea class="input" data-qid="${q.id}" rows="3" placeholder="写下你的解题过程"
             style="height:auto;min-height:82px;padding:10px 13px;line-height:1.65;font-family:inherit;font-size:14px;resize:vertical">${esc(chosen)}</textarea>`
        : `<input class="input" data-qid="${q.id}" placeholder="写下你的答案" value="${esc(chosen)}" />`}
    </div>`}
  </div>`;
}

function bindQuizInputs(view, ctx) {
  view.querySelectorAll(".option").forEach((btn) => {
    btn.onclick = () => {
      const qid = Number(btn.dataset.qid);
      S.quizAnswers[qid] = btn.dataset.val;
      view.querySelectorAll(`.option[data-qid="${qid}"]`).forEach((b) => {
        const on = b === btn;
        b.classList.toggle("selected", on);
        b.setAttribute("aria-checked", String(on));
      });
      updateQuizCounter(view);
    };
  });
  view.querySelectorAll("input[data-qid], textarea[data-qid]").forEach((input) => {
    input.oninput = () => {
      S.quizAnswers[Number(input.dataset.qid)] = input.value;
      updateQuizCounter(view);
    };
  });
}

function updateQuizCounter(view) {
  const questions = S.quiz?.questions || [];
  const answered = questions.filter((q) => (S.quizAnswers[q.id] || "").trim()).length;
  const el = view.querySelector("#quiz-count");
  if (el) el.textContent = `已作答 ${answered}/${questions.length}`;
  const btn = view.querySelector("#submit-quiz");
  if (btn) btn.disabled = answered === 0;
}

function resultPanelHTML(out, questions) {
  const byId = Object.fromEntries(questions.map((q, i) => [q.id, { q, i }]));
  return `<section class="card card-pad" id="quiz-result" style="margin-top:18px">
    <div class="card-title">批改结果</div>
    <div class="card-sub">客观题已即时生效并回写 BKT；主观题等老师确认。</div>
    <div style="display:flex;gap:10px;margin:16px 0 8px;flex-wrap:wrap">
      <span class="pill pill-green">已出分 ${out.confirmed_n} 题</span>
      <span class="pill pill-orange">待确认 ${out.pending_n} 题</span>
    </div>
    <div class="list">
      ${out.results.map((r) => {
        const meta = byId[r.question_id];
        const no = meta ? meta.i + 1 : r.question_id;
        if (r.status === "pending_confirm") {
          return `<div class="list-row">
            <span class="pill pill-orange">待老师确认</span>
            <div class="grow"><div class="title">第 ${no} 题</div>
              <div class="meta">主观题由 AI 给出建议分，需老师裁决后写入成绩</div></div>
          </div>`;
        }
        return `<div class="list-row">
          <span class="pill ${r.is_correct ? "pill-green" : "pill-red"}">${r.is_correct ? "答对" : "答错"}</span>
          <div class="grow"><div class="title">第 ${no} 题</div>
            <div class="meta">得分 ${r.score ?? "—"} · 掌握度更新为 ${fixed(r.p_known_after, 2)}</div></div>
        </div>`;
      }).join("")}
    </div>
    <div class="form-actions">
      <a class="btn btn-secondary" href="#/student/mastery">看掌握度变化</a>
      <button class="btn btn-primary" id="again-quiz">再测一次</button>
    </div>
  </section>`;
}

/* ---------------------------------------------------------------- 考试 M7 */
/* 数据流：
 *   列表  GET /api/student/exams            我们班被推的卷 + 我的考试记录
 *   作答  GET /api/student/exams/{id}       题目（无答案）+ 我已填的答案
 *   交卷  POST /api/student/exams/{id}/submit  客观题当场判、主观题等老师
 *   拍图  POST /api/student/upload/answer-image
 *
 * 草稿：边做边写 localStorage（key = prism.exam.draft.<paperId>）。
 *   考试不是作业，不能逐题实时入库（那等于没交卷就开卷），
 *   但刷新页面把 20 题答案全丢掉同样不可接受 —— 折中方案是本地草稿 + 一次提交。
 */

const EXAM_DRAFT_PREFIX = "prism.exam.draft.";

function examDraftKey(paperId) { return `${EXAM_DRAFT_PREFIX}${paperId}`; }

function readExamDraft(paperId) {
  try {
    const raw = localStorage.getItem(examDraftKey(paperId));
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function writeExamDraft(paperId) {
  try {
    localStorage.setItem(examDraftKey(paperId), JSON.stringify(S.examAnswers));
  } catch { /* 隐私模式写不进就只保留内存态，不影响作答 */ }
}

function clearExamDraft(paperId) {
  try { localStorage.removeItem(examDraftKey(paperId)); } catch { /* 忽略 */ }
}

const EXAM_TYPE = { monthly: "月考", midterm: "期中", final: "期末" };

/* ---- 列表：待考试 / 考试记录 ---- */
async function renderExam(view, ctx) {
  if (S.exams === null) {
    view.innerHTML = `<div class="page-head"><div class="eyebrow">考试 · M7</div>
      <h1 class="page-title">正在取你要考的卷子…</h1></div>
      <div class="grid grid-2">${'<div class="skeleton" style="height:150px"></div>'.repeat(2)}</div>`;
    try {
      S.exams = (await http.get("/api/student/exams")) || [];
    } catch (err) {
      view.innerHTML = `<div class="page-head"><div class="eyebrow">考试 · M7</div>
        <h1 class="page-title">取不到考试列表</h1></div>${errorBox(err.detail || "请求失败")}`;
      return;
    }
  }

  const list = S.exams;
  const todo = list.filter((x) => !x.attempt);
  const done = list.filter((x) => x.attempt);

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">考试 · M7</div>
      <h1 class="page-title">整卷作答，交卷即判</h1>
      <p class="page-sub">试卷由教研组长定稿后按年级推送到你的班级。选择题点选项、填空题填答案、解答题写步骤或拍手写图；交卷后客观题立刻出分，主观题等老师批改。</p>
    </div>

    <section class="card card-pad" style="margin-bottom:22px">
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px">
        <div>
          <div class="card-title">待考试</div>
          <div class="card-sub">${todo.length ? `共 ${todo.length} 份卷等你作答` : "暂时没有新卷子"}</div>
        </div>
      </div>
      <div class="list" style="margin-top:12px">
        ${todo.length ? todo.map((x) => `
          <div class="list-row exam-row">
            <span class="pill pill-blue">${esc(EXAM_TYPE[x.exam_type] || x.exam_type)}</span>
            <div class="grow">
              <div class="title"><span class="paper-id">#${x.paper_id}</span>${esc(x.title)}</div>
              <div class="meta">${esc(x.grade)} · ${x.question_count} 题 · 满分 ${fixed(x.full_score, 0)}
                ${x.published_at ? ` · 推送于 ${dateOf(x.published_at)}` : ""}</div>
            </div>
            <button class="btn btn-primary btn-sm" data-take="${x.paper_id}">开始考试</button>
          </div>`).join("")
          : emptyState("还没有推送的试卷", "教研组长定稿后，卷子会自动出现在这里", "file")}
      </div>
    </section>

    <section class="card card-pad">
      <div class="card-title">考试记录</div>
      <div class="card-sub">交卷后按客观题得分先出分；主观题批完总分自动补齐</div>
      <div class="list" style="margin-top:12px">
        ${done.length ? done.map((x) => {
          const a = x.attempt;
          const waiting = a.status !== "graded";
          return `<div class="list-row exam-row">
            <span class="pill ${waiting ? "pill-orange" : "pill-green"}">${waiting ? "待批改" : "已批完"}</span>
            <div class="grow">
              <div class="title"><span class="paper-id">#${x.paper_id}</span>${esc(x.title)}</div>
              <div class="meta">交卷 ${dateOf(a.submitted_at)} ·
                得分 <b>${fixed(a.total_score, 1)}</b> / ${fixed(a.full_score, 0)}
                ${waiting ? ` · 还有 ${x.pending_subjective} 道主观题等老师批` : ""}</div>
            </div>
            <button class="btn btn-sm btn-secondary" data-take="${x.paper_id}">查看试卷</button>
          </div>`;
        }).join("")
          : emptyState("还没有考试记录", "交卷后这里会生成一条记录", "check")}
      </div>
    </section>`;

  view.querySelectorAll("[data-take]").forEach((btn) => {
    btn.onclick = async () => {
      const restore = busy(btn, "打开…");
      try {
        await openExam(view, ctx, Number(btn.dataset.take));
      } catch (err) {
        restore();
        toast(err.detail || "打开试卷失败", "err");
      }
    };
  });
}

/* ---- 打开一份卷：题目 + 我的答案 ---- */
async function openExam(view, ctx, paperId, { keepResult = false } = {}) {
  view.innerHTML = `<div class="page-head"><div class="eyebrow">考试 · M7</div>
    <h1 class="page-title">正在加载试卷…</h1></div>
    <div class="skeleton" style="height:280px"></div>`;

  const data = await http.get(`/api/student/exams/${paperId}`);
  S.exam = data;

  const draft = data.attempt ? null : readExamDraft(paperId);
  if (draft) {
    S.examAnswers = draft;
  } else {
    S.examAnswers = {};
    (data.questions || []).forEach((q) => {
      if (!q.my_answer) return;
      S.examAnswers[q.id] = {
        content: q.my_answer.content || "",
        attachments: [...(q.my_answer.attachments || [])],
      };
    });
  }
  // 交卷后紧接着重新取卷时不能清空 result：
  //   那个「交卷成功」面板正是本次交卷的反馈，清掉的话学生只看到卷面变灰、以为没交上
  if (!keepResult) S.examResult = null;
  renderExamPaper(view, ctx);
}

function examAnswerOf(qid) {
  return S.examAnswers[qid] || (S.examAnswers[qid] = { content: "", attachments: [] });
}

function examAnsweredCount() {
  const qs = S.exam?.questions || [];
  return qs.filter((q) => {
    const a = S.examAnswers[q.id];
    return a && ((a.content || "").trim() || (a.attachments || []).length);
  }).length;
}

function renderExamPaper(view, ctx) {
  const data = S.exam;
  if (!data) { renderExam(view, ctx); return; }

  const questions = data.questions || [];
  const attempt = data.attempt;
  const readonly = Boolean(attempt);
  const answered = examAnsweredCount();
  const total = questions.length;
  const result = S.examResult;

  view.innerHTML = `
    <div class="exam-bar">
      <button class="btn btn-sm btn-secondary" id="exam-back">← 返回</button>
      <div class="grow" style="min-width:0">
        <div class="exam-bar-title">${esc(data.title)}</div>
        <div class="tiny">${total} 题 · 满分 ${fixed(data.full_score, 0)}
          ${readonly ? ` · 已于 ${dateOf(attempt.submitted_at)} 交卷` : ` · 已答 ${answered}/${total}`}</div>
      </div>
      ${readonly
        ? `<span class="pill ${attempt.status === "graded" ? "pill-green" : "pill-orange"}">
             ${attempt.status === "graded" ? "已批完" : "待老师批改"}</span>
           <span class="exam-score">${fixed(attempt.total_score, 1)}<small>/${fixed(attempt.full_score, 0)}</small></span>`
        : `<button class="btn btn-primary" id="exam-submit">交卷</button>`}
    </div>

    ${result ? examResultHTML(result, questions) : ""}

    <div class="exam-shell">
      <aside class="exam-nav" aria-label="答题卡">
        <div class="tiny" style="margin-bottom:8px">答题卡</div>
        <div class="exam-nav-grid">
          ${questions.map((q, i) => {
            const a = S.examAnswers[q.id];
            const filled = a && ((a.content || "").trim() || (a.attachments || []).length);
            const judged = readonly && q.my_answer;
            return `<button class="exam-dot ${filled ? "filled" : ""} ${judged && q.my_answer?.is_correct === true ? "ok" : ""} ${judged && q.my_answer?.is_correct === false ? "bad" : ""}"
              data-jump="${q.id}" title="第 ${i + 1} 题">${i + 1}</button>`;
          }).join("")}
        </div>
        <div class="tiny" style="margin-top:10px;line-height:1.9">
          ${readonly ? "绿=答对，红=答错，灰=待批改" : "蓝色=已填，灰色=未填"}
        </div>
      </aside>

      <div class="exam-body">
        ${questions.map((q, i) => examQuestionHTML(q, i, readonly)).join("")}
        ${readonly ? "" : `
          <div class="card card-pad" style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
            <div class="muted" style="font-size:13.5px">
              交卷前请确认所有题目都已作答：客观题交卷即判，主观题会进入老师的待批改队列。
            </div>
            <div class="spacer" style="flex:1"></div>
            <button class="btn btn-primary" id="exam-submit-2">交卷</button>
          </div>`}
      </div>
    </div>`;

  view.querySelector("#exam-back").onclick = () => {
    S.exam = null;
    S.exams = null;          // 重新拉列表：交卷后状态要跟着变
    renderExam(view, ctx);
  };

  if (!readonly) bindExamInputs(view, ctx);

  view.querySelectorAll("[data-jump]").forEach((btn) => {
    btn.onclick = () => {
      const card = view.querySelector(`[data-qcard="${btn.dataset.jump}"]`);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
    };
  });

  const doSubmit = () => submitExam(view, ctx);
  const s1 = view.querySelector("#exam-submit");
  const s2 = view.querySelector("#exam-submit-2");
  if (s1) s1.onclick = doSubmit;
  if (s2) s2.onclick = doSubmit;
}

function examQuestionHTML(q, i, readonly) {
  const a = examAnswerOf(q.id);
  const opts = Array.isArray(q.options) ? q.options : null;
  const content = a.content || "";
  const attachments = a.attachments || [];
  const mine = q.my_answer;

  // 已交卷：客观题显示对错、主观题显示待批/得分
  let verdict = "";
  if (readonly && mine) {
    if (mine.is_correct === true) {
      verdict = `<span class="pill pill-green">答对 · ${fixed(mine.score, 1)} 分</span>`;
    } else if (mine.is_correct === false) {
      verdict = `<span class="pill pill-red">答错 · ${fixed(mine.score ?? 0, 1)} 分</span>`;
    } else if (q.question_type === "subjective") {
      verdict = `<span class="pill pill-orange">待老师批改</span>`;
    } else {
      // 客观题只有"对/错"两种结局，不存在待批。走到这里说明后端判分漏了这一条
      // （历史脏数据），显示成"待老师批改"会让学生白等，也污染老师待批队列。
      verdict = `<span class="pill pill-red">未作答 · 0 分</span>`;
    }
  } else if (readonly && !mine) {
    verdict = `<span class="pill pill-red">未作答</span>`;
  }

  let inputHTML = "";
  if (q.question_type === "choice" && opts) {
    inputHTML = `<div class="options" role="radiogroup" aria-label="第 ${i + 1} 题选项">
      ${opts.map((opt) => {
        const letter = String(opt).trim()[0];
        const on = content.trim().toUpperCase() === letter;
        return `<button type="button" class="option ${on ? "selected" : ""}"
          data-qid="${q.id}" data-val="${esc(letter)}" ${readonly ? "disabled" : ""}
          role="radio" aria-checked="${on}">
          <span class="letter">${esc(letter)}</span><span>${esc(opt)}</span>
        </button>`;
      }).join("")}
    </div>`;
  } else if (q.question_type === "fill") {
    inputHTML = `<div class="exam-fill">
      <input class="input exam-input" data-qid="${q.id}" value="${esc(content)}"
        placeholder="把答案填在这里（如 x=1或x=3）" ${readonly ? "disabled" : ""} />
    </div>`;
  } else {
    inputHTML = `
      <textarea class="input exam-textarea" data-qid="${q.id}" rows="5"
        placeholder="写出完整解题步骤：设、列、解、答" ${readonly ? "disabled" : ""}>${esc(content)}</textarea>
      <div class="exam-upload">
        ${readonly ? "" : `
          <button type="button" class="btn btn-sm btn-secondary" data-upload="${q.id}">
            上传手写步骤图片
          </button>
          <input type="file" accept="image/jpeg,image/png,image/webp" hidden data-file="${q.id}" />`}
        <span class="tiny" data-upstatus="${q.id}">
          ${attachments.length ? `已上传 ${attachments.length} 张` : "支持拍照上传，最多 5 张"}
        </span>
      </div>`;
  }

  const thumbs = attachments.length ? `<div class="exam-thumbs">
    ${attachments.map((url) => `
      <a class="exam-thumb" href="${esc(API_BASE + url)}" target="_blank" rel="noopener">
        <img src="${esc(API_BASE + url)}" alt="手写作答图片" loading="lazy" />
        ${readonly ? "" : `<button type="button" class="exam-thumb-del" data-del="${q.id}"
          data-url="${esc(url)}" aria-label="删除这张图">✕</button>`}
      </a>`).join("")}
  </div>` : "";

  return `<section class="q-card exam-q" data-qcard="${q.id}" id="exam-q-${q.id}">
    <div class="q-hd">
      <span class="q-no">第 ${i + 1} 题</span>
      <span class="pill">${esc(QTYPE[q.question_type] || q.question_type)}</span>
      <span class="pill">${fixed(q.score, 1)} 分</span>
      <span class="pill">${esc(DIFF[q.difficulty] || q.difficulty)}</span>
      <span class="spacer" style="flex:1"></span>
      ${verdict}
    </div>
    <div class="q-text">${md(q.content)}</div>
    ${inputHTML}
    ${thumbs}
  </section>`;
}

function bindExamInputs(view, ctx) {
  const paperId = S.exam.paper_id;

  view.querySelectorAll(".option[data-qid]").forEach((btn) => {
    btn.onclick = () => {
      const qid = Number(btn.dataset.qid);
      const a = examAnswerOf(qid);
      a.content = btn.dataset.val;
      view.querySelectorAll(`.option[data-qid="${qid}"]`).forEach((b) => {
        const on = b === btn;
        b.classList.toggle("selected", on);
        b.setAttribute("aria-checked", String(on));
      });
      writeExamDraft(paperId);
      refreshExamCounter(view);
    };
  });

  view.querySelectorAll("input.exam-input, textarea.exam-textarea").forEach((el) => {
    el.oninput = () => {
      examAnswerOf(Number(el.dataset.qid)).content = el.value;
      writeExamDraft(paperId);
      refreshExamCounter(view);
    };
  });

  view.querySelectorAll("[data-upload]").forEach((btn) => {
    const qid = Number(btn.dataset.upload);
    const fileInput = view.querySelector(`input[data-file="${qid}"]`);
    btn.onclick = () => fileInput?.click();
    if (!fileInput) return;

    fileInput.onchange = async () => {
      const file = fileInput.files?.[0];
      fileInput.value = "";                       // 同一张图重选也能触发 change
      if (!file) return;

      const status = view.querySelector(`[data-upstatus="${qid}"]`);
      const restore = busy(btn, "上传中…");
      try {
        const dataUrl = await compressImage(file);
        const out = await http.post("/api/student/upload/answer-image", {
          filename: file.name || "answer.jpg",
          data: dataUrl,
          purpose: "answer",
        });
        examAnswerOf(qid).attachments.push(out.url);
        writeExamDraft(paperId);
        restore();
        renderExamPaper(view, ctx);            // 缩略图要重绘，整体重绘最省事
        requestAnimationFrame(() => {
          view.querySelector(`#exam-q-${qid}`)?.scrollIntoView({ block: "center" });
        });
      } catch (err) {
        restore();
        if (status) status.textContent = err.detail || "上传失败";
        toast(err.detail || "上传失败", "err");
      }
    };
  });

  view.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();                          // 图片外层是 <a>，别让点击跳转
      const qid = Number(btn.dataset.del);
      const a = examAnswerOf(qid);
      a.attachments = (a.attachments || []).filter((u) => u !== btn.dataset.url);
      writeExamDraft(paperId);
      renderExamPaper(view, ctx);
      requestAnimationFrame(() => {
        view.querySelector(`#exam-q-${qid}`)?.scrollIntoView({ block: "center" });
      });
    };
  });
}

function refreshExamCounter(view) {
  const total = (S.exam?.questions || []).length;
  const answered = examAnsweredCount();
  const el = view.querySelector(".exam-bar .tiny");
  if (el) {
    el.textContent = el.textContent.replace(/已答 \d+\/\d+/, `已答 ${answered}/${total}`);
  }
  (S.exam?.questions || []).forEach((q) => {
    const a = S.examAnswers[q.id];
    const filled = Boolean(a && ((a.content || "").trim() || (a.attachments || []).length));
    view.querySelector(`.exam-dot[data-jump="${q.id}"]`)?.classList.toggle("filled", filled);
  });
}

/* 拍照图压缩：考试卷面上传的是手机原图（3~5MB），
 * 不压缩会让 base64 请求体到 7MB、手机网络下等到超时。
 * 1280px 宽 + JPEG 0.82：手写字依然清晰，体积落到 200~400KB。 */
async function compressImage(file, maxWidth = 1280, quality = 0.82) {
  if (!file.type.startsWith("image/")) throw new Error("只能上传图片");
  if (typeof createImageBitmap !== "function") {
    // 老浏览器兜底：不压缩直接转 base64（后端 6MB 上限会兜住）
    return await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("读取图片失败"));
      reader.readAsDataURL(file);
    });
  }
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, maxWidth / bitmap.width);
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", quality);
}

function examResultHTML(out, questions) {
  const byId = Object.fromEntries(questions.map((q, i) => [q.id, i + 1]));
  const wrong = out.results.filter((r) => r.is_correct === false);
  return `<section class="card card-pad" id="exam-result" style="margin-bottom:18px">
    <div class="card-title">交卷成功</div>
    <div class="card-sub">客观题已判完并回写掌握度；主观题已进入老师的待批改队列。</div>
    <div style="display:flex;gap:10px;margin:14px 0 10px;flex-wrap:wrap">
      <span class="pill pill-green">客观题得分 ${fixed(out.objective_score, 1)} / ${fixed(out.full_score, 0)}</span>
      <span class="pill pill-blue">判完 ${out.results.length} 题</span>
      <span class="pill pill-orange">主观题待批 ${out.pending_subjective} 题</span>
      ${wrong.length ? `<span class="pill pill-red">答错 ${wrong.length} 题</span>` : ""}
    </div>
    ${wrong.length ? `<div class="tiny" style="line-height:1.9">
      错题已记入你的错题本，答错的题号：${wrong.map((r) => `第 ${byId[r.question_id] || r.question_id} 题`).join("、")}
    </div>` : ""}
  </section>`;
}

/* ---- 交卷 ---- */
async function submitExam(view, ctx) {
  const data = S.exam;
  const questions = data.questions || [];
  const missing = questions.filter((q) => {
    const a = S.examAnswers[q.id];
    return !(a && ((a.content || "").trim() || (a.attachments || []).length));
  });

  const answers = questions.map((q) => {
    const a = S.examAnswers[q.id] || {};
    return {
      question_id: q.id,
      content: String(a.content || "").trim(),
      attachments: a.attachments || [],
    };
  }).filter((a) => a.content || a.attachments.length);

  if (!answers.length) { toast("一道题都没作答，不能交卷", "err"); return; }

  const go = async (btn) => {
    const restore = busy(btn, "交卷判分中…");
    try {
      const out = await http.post(`/api/student/exams/${data.paper_id}/submit`, { answers });
      clearExamDraft(data.paper_id);
      S.exams = null;              // 列表要重拉：这份卷要从"待考试"搬到"考试记录"
      S.mastery.kp = null;         // 客观题已回写 BKT
      S.examResult = out;
      await openExam(view, ctx, data.paper_id, { keepResult: true });   // 重新取一次：拿最新 attempt 与逐题对错
      toast(`客观题已出分 ${fixed(out.objective_score, 1)} 分`, "ok");
      requestAnimationFrame(() => {
        view.querySelector("#exam-result")?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    } catch (err) {
      restore();
      toast(err.detail || "交卷失败", "err");
    }
  };

  const tip = missing.length
    ? `还有 <b>${missing.length}</b> 题没有作答（第 ${missing.map((q) => questions.findIndex((x) => x.id === q.id) + 1).join("、")} 题）。
       交卷后这些题按 0 分计，且不能再修改。`
    : "全部题目都已作答。交卷后客观题立刻出分，主观题进入老师待批改队列，不能再修改答案。";

  modal({
    title: missing.length ? "还有题目没做完" : "确认交卷",
    sub: `《${data.title}》共 ${questions.length} 题`,
    body: `<div style="font-size:14px;line-height:1.85">${tip}</div>`,
    actions: [
      { label: "再检查一遍" },
      { label: missing.length ? "仍要交卷" : "确认交卷", kind: "primary", onClick: () => go(
        view.querySelector("#exam-submit-2") || view.querySelector("#exam-submit")) },
    ],
  });
}

/* ---------------------------------------------------------------- 掌握度 S-08 */
async function renderMastery(view, ctx) {
  const sid = ctx.me.student_id;
  const gran = S.gran;
  if (!S.mastery[gran]) S.mastery[gran] = await http.get(`/api/student/mastery/${sid}`, { granularity: gran });
  const data = S.mastery[gran];
  const items = data.items || [];
  const avg = items.length ? items.reduce((a, b) => a + b.p_known, 0) / items.length : 0;

  view.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">掌握度 · S-08</div>
      <h1 class="page-title">同一份数据，看得见才改得动</h1>
      <p class="page-sub">BKT 三步更新：遗忘衰减 → 贝叶斯后验修正 → 学习效应。这里只呈现结果，数值越低越薄弱。</p>
    </div>

    <div style="display:flex;gap:12px;align-items:center;margin-bottom:20px;flex-wrap:wrap">
      <div class="segmented" id="gran-switch">
        <button data-gran="kp" class="${gran === "kp" ? "active" : ""}">按知识点</button>
        <button data-gran="chapter" class="${gran === "chapter" ? "active" : ""}">按章节</button>
      </div>
      <span class="pill">平均 ${pct(avg)}</span>
      <span class="pill">${items.length} 个${gran === "kp" ? "知识点" : "章节"}</span>
      <span class="pill pill-red">${items.filter((i) => i.p_known < 0.4).length} 个严重薄弱</span>
    </div>

    <div class="grid grid-2" style="align-items:start">
      <section class="card card-pad">
        <div class="card-title">掌握度雷达</div>
        <div class="card-sub">取最薄弱的 10 个${gran === "kp" ? "知识点" : "章节"}</div>
        <div style="margin-top:12px">
          ${items.length >= 3
            ? radar({
                labels: items.slice(0, 10).map((i) => i.name),
                values: items.slice(0, 10).map((i) => i.p_known),
              })
            : emptyState("数据还不够画雷达图", "至少需要 3 个有掌握度记录的知识点", "chart")}
        </div>
        <div class="divider"></div>
        <div class="tiny" style="line-height:2">
          分级：≥85% 掌握 · 60~85% 基本掌握 · 40~60% 薄弱 · &lt;40% 严重薄弱（触发预警）
        </div>
      </section>

      <section class="card card-pad">
        <div class="card-title">全部${gran === "kp" ? "知识点" : "章节"}</div>
        <div class="card-sub">升序排列，最薄弱的在最上面</div>
        <div style="margin-top:12px">
          ${items.length
            ? `<div class="scroll-list">${barRows(items)}</div>`   // 知识点多时区域内滚动，不撑长页面
            : emptyState("还没有数据", "做几道题就会亮起来", "chart")}
        </div>
      </section>
    </div>`;

  view.querySelectorAll("#gran-switch button").forEach((btn) => {
    btn.onclick = async () => {
      const next = btn.dataset.gran;
      if (next === S.gran) return;
      S.gran = next;
      await renderMastery(view, ctx);
    };
  });
}

/* ---------------------------------------------------------------- 小工具 */
function kpCount(raw) {
  if (!raw) return 0;
  try {
    const arr = typeof raw === "string" ? JSON.parse(raw) : raw;
    return Array.isArray(arr) ? arr.length : 0;
  } catch { return 0; }
}

function kpNameMap(mastery) {
  const map = {};
  (mastery?.items || []).forEach((it) => { if (it.kp_id) map[it.kp_id] = it.name; });
  return map;
}

function skelCards(n) {
  return Array.from({ length: n }).map(() => `<div class="skeleton" style="height:118px"></div>`).join("");
}
