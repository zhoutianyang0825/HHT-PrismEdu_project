# streamlit_app/pages/2_老师端.py
# M5-T4 · 老师端页面：布置作业 / 待批改列表 / 🔴 主观题 AI 建议确认（HITL 闭环）。
#
# 【主观题确认的页面流】（对应 app/api/teacher.py 的两步走）
#   ① "待批改"列表点「AI 批改」→ POST /grade/{id}
#      返回 pending_confirm + suggestion（建议分/评语/采分点）→ 渲染确认卡
#   ② 老师三选一：采纳 confirm / 改分 edit / 驳回 reject → POST /grade/{id}/confirm
#      返回终局：final_score + BKT 前后对比（这一步触发了掌握度更新）
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_client

st.set_page_config(page_title="老师端 · HHT棱镜课堂", page_icon="👨‍🏫", layout="wide")
me = api_client.require_role("teacher", "admin")

st.title("👨‍🏫 老师端")

# ================================================================
# 一、布置作业（T-01，复用组卷引擎）
# ================================================================
with st.expander("📝 布置作业（按知识点抽题）", expanded=False):
    classes = api_client.api("GET", "/api/teacher/classes")
    if not classes:
        st.warning("你还没有任教班级：请管理员在「管理后台 → 授课关系」里添加")
    else:
        cls_map = {f"{c['grade']}{c['name']}（#{c['id']}）": c["id"] for c in classes}
        with st.form("hw_form"):
            cls_label = st.selectbox("班级", list(cls_map.keys()))
            title = st.text_input("作业标题", value=f"作业 {st.session_state.get('hw_n', 1)}")
            kps_raw = st.text_input("知识点 id（逗号分隔）", value="kp_crossmult,kp_peifang")
            per_kp = st.number_input("每知识点题数", 1, 5, 2)
            score = st.number_input("每题分值", 1.0, 20.0, 4.0)
            submitted = st.form_submit_button("抽题布置", use_container_width=True)

        if submitted:
            body = {"class_id": cls_map[cls_label], "title": title,
                    "kp_ids": [k.strip() for k in kps_raw.split(",") if k.strip()],
                    "per_kp": int(per_kp), "score": float(score)}
            out = api_client.api("POST", "/api/teacher/homework/create", json_body=body)
            st.session_state["hw_n"] = st.session_state.get("hw_n", 1) + 1
            st.success(f"作业 #{out['homework_id']} 布置成功，抽中 {len(out['question_ids'])} 题")
            if out.get("gaps"):
                st.warning("配题缺口（宁可少题不错题）：\n" + "\n".join(out["gaps"]))

# ================================================================
# 二、待批改 / 待确认列表
# ================================================================
st.subheader("🧾 待批改列表")
pend = api_client.api("GET", "/api/teacher/grade/pending")
if not pend:
    st.caption("全部处理完毕 🎉")

for row in pend:
    with st.expander(f"{row['student']} · {row['question']}…"
                     f"（{row['question_type']}）"):
        st.write(f"**学生作答**：{row['content']}")
        c1, c2 = st.columns(2)

        # ---- ① 触发 AI 批改 ----
        if c1.button("AI 批改", key=f"grade_{row['answer_id']}"):
            with st.spinner("客观题直判 / 主观题 LLM 打分…"):
                out = api_client.api("POST", f"/api/teacher/grade/{row['answer_id']}")
            st.session_state[f"grade_result_{row['answer_id']}"] = out
            st.rerun()

        out = st.session_state.get(f"grade_result_{row['answer_id']}")
        if not out:
            continue

        if out.get("status") == "pending_confirm":
            # ---- ② 渲染 AI 建议确认卡 ----
            sug = out["suggestion"]
            st.info(f"AI 建议分：**{sug.get('ai_score')}** / {sug.get('full_score')}"
                    f"（置信度 {sug.get('confidence', 0):.0%}）\n\n"
                    f"评语：{sug.get('ai_comment')}")
            for basis in sug.get("scoring_basis", []):
                st.write(f"- {basis}")

            act = st.radio("裁决", ["confirm", "edit", "reject"], horizontal=True,
                           format_func=lambda x: {"confirm": "采纳", "edit": "改分",
                                                  "reject": "驳回"}[x],
                           key=f"act_{row['answer_id']}")
            edit_score = None
            if act == "edit":
                edit_score = st.number_input("最终分数", 0.0, float(sug.get("full_score", 100)),
                                             float(sug.get("ai_score", 0)),
                                             key=f"score_{row['answer_id']}")
            if st.button("确认提交", key=f"confirm_{row['answer_id']}", type="primary"):
                with st.spinner("落库 + BKT 掌握度更新…"):
                    final = api_client.api(
                        "POST", f"/api/teacher/grade/{row['answer_id']}/confirm",
                        json_body={"action": act, "score": edit_score})
                st.success(f"已生效：最终分 {final.get('final_score')}，"
                           f"掌握度 {final.get('p_known_before')} → {final.get('p_known_after')}")
                st.session_state.pop(f"grade_result_{row['answer_id']}", None)
        else:
            # ---- 客观题终局（或已批过的幂等返回）----
            mark = "✅" if out.get("is_correct") else "❌"
            st.success(f"{mark} 规则直判：得分 {out.get('final_score')}，"
                       f"掌握度 → {out.get('p_known_after')}")

# ================================================================
# 三、班级错题汇总 + 掌握度
# ================================================================
st.subheader("📊 班级学情")
classes = api_client.api("GET", "/api/teacher/classes")
if classes:
    cls_map2 = {f"{c['grade']}{c['name']}（#{c['id']}）": c["id"] for c in classes}
    pick = st.selectbox("选班级", list(cls_map2.keys()), key="cls_pick")
    cid = cls_map2[pick]

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Top3 薄弱知识点（错因汇总）**")
        summary = api_client.api("GET", f"/api/teacher/class/{cid}/error-summary")
        for kp in summary.get("top_weak_kps", []):
            st.write(f"- {kp['name']}：错 {kp['wrong_count']} 次 "
                     f"（{', '.join(f'{k}×{v}' for k, v in kp['error_types'].items())}）")
        if not summary.get("top_weak_kps"):
            st.caption("还没有错因数据（先让学生做几道题）")
    with c2:
        st.markdown("**班级掌握度（最薄弱在前）**")
        mastery = api_client.api("GET", f"/api/teacher/class/{cid}/mastery")
        for m in mastery[:8]:
            st.write(f"- {m['kp_id']}：{m['avg_p_known']:.2f}（{m['student_count']} 人）")

    with st.expander("该作业提交情况", expanded=False):
        hw_id = st.number_input("作业 id", min_value=1, value=1, key="sub_hw")
        subs = api_client.api("GET", f"/api/teacher/homework/{hw_id}/submissions")
        if subs:
            st.dataframe(subs, use_container_width=True)
        else:
            st.caption("暂无作答记录")
