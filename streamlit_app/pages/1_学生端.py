# streamlit_app/pages/1_学生端.py
# M5-T4 · 学生端页面：引导对话（SSE + interrupt）+ S-10 诊断测验 + BKT 雷达图。
#
# 【一页三块布局】
#   左列  对话区：输入 → stream_chat 读 SSE → 按 type 渲染
#         🔴 interrupt 事件 = Agent 挂起等回应：下一条输入必须走 /resume
#   右列  上：生成测验按钮 + 测验作答；下：BKT 雷达图（Plotly Scatterpolar）
#
# 【interrupt 的前端状态机】（与 app/api/student.py 的协议一一对应）
#   session_state["awaiting_resume"] = question_id  表示"图正挂着等回应"；
#   发消息时按它决定调 /answer/stream（新发言）还是 /resume（回应提示）。
#   后端 answer/stream 还有防御性 resume 兜底，两边双保险。
import sys
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_client

st.set_page_config(page_title="学生端 · HHT棱镜课堂", page_icon="🎒", layout="wide")
me = api_client.require_role("student", "admin")

st.title("🎒 学生端")

# ---- 学生身份：学生本人用登录返回的 student_id；admin 可代演选择 ----
if "admin" in me["roles"] and not me.get("student_id"):
    student_id = st.number_input("代演学生档案 id（students.id）", min_value=1, value=1)
else:
    student_id = me.get("student_id")
    if not student_id:
        st.error("当前账号没有学生档案（students 表无对应行），请联系管理员")
        st.stop()

# ---- 选题目（演示从题库挑一题；正式流程从作业进入）----
st.markdown("---")
c1, c2 = st.columns([1, 3])
with c1:
    question_id = st.number_input("当前题目 id", min_value=1, value=1)
with c2:
    st.caption("演示口径：直接输题号开始对话。正式版从「作业列表」点进题目（见下方作业区）。")

# ---- 作业列表（self 通道，仅展示）----
with st.expander("📋 我的作业", expanded=False):
    try:
        hws = api_client.api("GET", "/api/student/homework")
        if hws:
            for h in hws:
                st.write(f"- **{h['title']}**（作业 #{h['id']}）知识点：{h['kp_ids']}")
            if hws:
                question_id = st.number_input("选作业里的题目 id 开始作答",
                                              min_value=1,
                                              value=int(st.session_state.get("hw_qid", question_id)),
                                              key="hw_qid")
        else:
            st.write("暂无作业")
    except Exception:
        pass

# ================================================================
# 左列：对话区（SSE）
# ================================================================
left, right = st.columns([3, 2])

with left:
    st.subheader("💬 学习助手")
    # 会话历史存 session_state：list[(who, text)]
    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []
    if "awaiting_resume" not in st.session_state:
        st.session_state["awaiting_resume"] = None   # 挂起时记 question_id

    # 渲染历史
    chat_box = st.container()
    with chat_box:
        for who, text in st.session_state["chat_history"]:
            st.chat_message(who).write(text)

    user_input = st.chat_input("输入你的问题或答案…（例：这题答案是多少？）")

    if user_input and question_id:
        st.session_state["chat_history"].append(("user", user_input))
        with chat_box:
            st.chat_message("user").write(user_input)
            with st.chat_message("assistant"):
                holder = st.empty()

        def on_event(etype, payload):
            """SSE 事件 → 页面渲染（见 api_client.stream_chat 的分发）。"""
            if etype == "interrupt":
                # 🔴 图挂起：payload={"type":"scaffold_hint","hint":...,"attempt_count":...}
                hint = payload.get("hint", "")
                holder.write(f"🪜 {hint}\n\n*（第 {payload.get('attempt_count', '?')} 次引导，"
                             "试着回答上面的提示）*")
                st.session_state["chat_history"].append(("assistant", hint))
                st.session_state["awaiting_resume"] = int(question_id)   # 标记挂起
            elif etype == "message":
                holder.write(payload.get("content", ""))
                st.session_state["chat_history"].append(("assistant", payload.get("content", "")))
                st.session_state["awaiting_resume"] = None   # 一轮结束，图跑完了
            elif etype == "node":
                holder.caption(f"… {payload.get('node', '')}")
            elif etype == "error":
                holder.error(payload.get("message", "未知错误"))
                st.session_state["awaiting_resume"] = None
            # end：什么都不用做

        # 关键分叉：挂着 → /resume；否则 → /answer/stream
        if st.session_state.get("awaiting_resume") == int(question_id):
            body = {"student_id": int(student_id), "question_id": int(question_id),
                    "message": user_input}
            api_client.stream_chat("/api/student/resume", body, on_event)
        else:
            body = {"student_id": int(student_id), "question_id": int(question_id),
                    "message": user_input}
            api_client.stream_chat("/api/student/answer/stream", body, on_event)

# ================================================================
# 右列上：S-10 诊断测验
# ================================================================
with right:
    st.subheader("🎯 诊断测验（S-10）")
    if st.button("根据我的错题生成 8 题小测", use_container_width=True):
        with st.spinner("正在按 BKT 薄弱点配题…"):
            quiz = api_client.api("POST", "/api/student/quiz/generate")
        st.session_state["quiz"] = quiz

    quiz = st.session_state.get("quiz")
    if quiz and quiz.get("quiz_id"):
        st.info(quiz.get("generated_reason", ""))
        with st.expander("为什么出这些题（可解释性留痕）", expanded=False):
            for tp in quiz.get("target_points", []):
                st.write(f"- {tp['kp_id']}：p_known={tp['p_known']:.2f} · {tp['reason']}")

        answers = []
        for i, q in enumerate(quiz.get("questions", []), 1):
            st.markdown(f"**第 {i} 题**（{q['difficulty']}）\n\n{q['content']}")
            if q.get("options"):
                opt = st.radio("选项", q["options"], key=f"opt_{q['id']}", label_visibility="collapsed")
                answers.append({"question_id": q["id"], "content": opt[0]})   # 取字母
            else:
                txt = st.text_input("作答", key=f"ans_{q['id']}")
                if txt:
                    answers.append({"question_id": q["id"], "content": txt})

        if answers and st.button("提交整套测验", type="primary", use_container_width=True):
            with st.spinner("批改中（客观题直判，主观题等老师确认）…"):
                out = api_client.api("POST", f"/api/student/quiz/{quiz['quiz_id']}/submit",
                                     json_body={"answers": answers})
            st.success(f"客观题已出分 {out['confirmed_n']} 题；"
                       f"主观题 {out['pending_n']} 题待老师确认")
            for r in out["results"]:
                if r["status"] == "pending_confirm":
                    st.write(f"- 题 {r['question_id']}：⏳ 待老师确认")
                else:
                    mark = "✅" if r.get("is_correct") else "❌"
                    st.write(f"- 题 {r['question_id']}：{mark} 得分 {r.get('score')}"
                             f"（掌握度 → {r.get('p_known_after')}）")
            st.session_state.pop("quiz", None)
    elif quiz:
        st.warning(quiz.get("generated_reason") or "没有可出的题（最近没有错题记录）")

    # ---- BKT 雷达图 ----
    st.subheader("📈 我的掌握度雷达图")
    granularity = st.radio("粒度", ["kp", "chapter"], horizontal=True,
                           format_func=lambda x: "按知识点" if x == "kp" else "按章节")
    data = api_client.api("GET", "/api/student/mastery/{id}".format(id=student_id),
                          params={"granularity": granularity})
    items = data.get("items", [])
    if items:
        fig = go.Figure(go.Scatterpolar(
            # 雷达图轴数量太多会糊：最多取 10 个（最薄弱的 10 个）
            theta=[it["name"] for it in items[:10]],
            r=[it["p_known"] for it in items[:10]],
            fill="toself", name="p_known",
        ))
        fig.update_layout(polar=dict(radialaxis=dict(range=[0, 1])), height=380,
                          margin=dict(l=30, r=30, t=20, b=20))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("数值越低 = 越薄弱（升序排列，取最薄弱 10 个）")
    else:
        st.caption("还没有掌握度数据：先做几道题，BKT 更新后这里就会亮起来")
