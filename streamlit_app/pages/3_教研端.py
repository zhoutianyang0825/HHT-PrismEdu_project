# streamlit_app/pages/3_教研端.py
# M5-T4 · 教研端页面：年级掌握度 / 班级对比 / R-08 出卷 / R-04 分析（闭环回流展示位）。
#
# 【本页要讲清的一句话（演示第 7 步的灵魂）】
#   "上一份卷子的分析结果，自动变成下一份卷子的组卷依据"：
#   R-04 analyze → 异常题 quality_flag=abnormal → R-08 抽题时 abnormal 沉底。
#   页面用「异常题清单 → 出卷 → 配额表」的排列顺序把这个故事讲出来。
import sys
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_client

st.set_page_config(page_title="教研端 · HHT棱镜课堂", page_icon="📊", layout="wide")
me = api_client.require_role("researcher", "admin")

st.title("📊 教研端")

grade = st.text_input("负责年级", value="初二")
subject = "math"

# ================================================================
# 一、年级掌握度 + 班级对比（聚合通道，无学生明细）
# ================================================================
c1, c2 = st.columns(2)
with c1:
    st.subheader("🔎 年级最薄弱知识点")
    mastery = api_client.api("GET", f"/api/research/grade/{grade}/mastery",
                             params={"subject": subject})
    for it in mastery.get("items", [])[:8]:
        st.write(f"- {it['name']}：{it['avg_p_known']:.2f}")

with c2:
    st.subheader("⚖️ 班级横向对比")
    cmp_data = api_client.api("GET", "/api/research/mastery/compare",
                              params={"grade": grade, "subject": subject})
    if cmp_data.get("items"):
        fig = go.Figure(go.Bar(
            x=[it["class_name"] for it in cmp_data["items"]],
            y=[it["avg_p_known"] for it in cmp_data["items"]],
            text=[it["avg_p_known"] for it in cmp_data["items"]],
        ))
        fig.update_layout(yaxis=dict(range=[0, 1]), height=320,
                          margin=dict(l=20, r=20, t=10, b=20))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("暂无班级数据")

# ================================================================
# 二、历史异常题（R-04 产出 / R-08 消费——闭环的"回流"证据）
# ================================================================
st.subheader("🚨 历史异常题清单（R-04 写回 → R-08 自动降权）")
abnormal = api_client.api("GET", "/api/research/questions/abnormal", params={"subject": subject})
if abnormal:
    st.dataframe(abnormal, use_container_width=True)
else:
    st.caption("暂无异常题：先对一份有作答数据的试卷跑 R-04 分析")

# ================================================================
# 三、R-08 出卷
# ================================================================
st.subheader("📄 R-08 生成试卷")
with st.form("exam_form"):
    e1, e2, e3 = st.columns(3)
    exam_type = e1.selectbox("类型", ["monthly", "midterm", "final"],
                             format_func=lambda x: {"monthly": "月考（巩固）",
                                                    "midterm": "期中（摸底）",
                                                    "final": "期末（摸底）"}[x])
    grade_in = e2.text_input("年级", value=grade)
    total_score = e3.number_input("总分", 50, 150, 100)
    if st.form_submit_button("开始生成（规划→抽题→审核）", use_container_width=True):
        with st.spinner("三角色 crew 跑动中：命题规划师出配额 → 选题执行 → 质量审核…"):
            out = api_client.api("POST", "/api/research/exam/generate",
                                 json_body={"exam_type": exam_type, "grade": grade_in,
                                            "subject": subject,
                                            "total_score": int(total_score)})
        st.session_state["last_exam"] = out

exam = st.session_state.get("last_exam")
if exam:
    review = exam.get("review", {})
    if review.get("passed"):
        st.success(f"试卷 #{exam['paper_id']} 生成成功（{review.get('rounds_used')} 轮通过审核）")
    else:
        st.warning(f"试卷 #{exam['paper_id']} 已生成，但审核未通过——缺口清单如下（宁可少题不错题）")
    st.caption(review.get("summary", ""))

    with st.expander("配额表 QuotaPlan（可打印给组长人工改）", expanded=True):
        plan = exam.get("plan", {})
        rows = plan.get("items", [])
        st.dataframe(rows, use_container_width=True)
    with st.expander("审核报告与缺口", expanded=False):
        st.json(review)
        if exam.get("gaps"):
            st.write("抽题缺口：")
            for g in exam["gaps"]:
                st.write(f"- {g}")

    cid1, cid2 = st.columns(2)
    assign_classes = cid1.text_input("布置到班级 id（逗号分隔）", value="1")
    if cid2.button(f"定稿布置（paper_id={exam['paper_id']}）", use_container_width=True):
        class_ids = [int(x) for x in assign_classes.split(",") if x.strip()]
        api_client.api("POST", f"/api/research/exam/{exam['paper_id']}/assign",
                       json_body={"class_ids": class_ids})
        st.success("已定稿 published。下一步：跑 scripts/gen_demo_data.py 合成作答，再回来触发分析")

# ================================================================
# 四、R-04 考后分析
# ================================================================
st.subheader("📈 R-04 考后分析（CrewAI 三角色）")
paper_id = st.number_input("试卷 id（需已有已判分作答）", min_value=1, value=1, key="ana_paper")
if st.button("触发分析", type="primary"):
    with st.spinner("Pandas 算指标 → 数据分析师 → 命题质量专家（查图谱）→ 教研组长汇总…"):
        out = api_client.api("POST", f"/api/research/paper/{paper_id}/analyze")
    st.session_state["last_analysis"] = out

ana = st.session_state.get("last_analysis")
if ana:
    report = ana.get("report", {})
    st.info(report.get("overview", ""))
    st.markdown("**异常题诊断卡**")
    for item in report.get("abnormal_items", []):
        st.write(f"- 题 {item['q_id']}（{item['kp_id']}）：P={item['P']}，D={item['D']}，"
                 f"**{item['issue']}** —— {item['cause']}")
    st.markdown("**教研建议（可直接排进课表）**")
    for s in report.get("teaching_suggestions", []):
        st.write(f"- ✅ {s}")
    st.caption(f"异常题已写回 quality_flag（{ana.get('abnormal_written')} 道）——"
               "下一次 R-08 出卷会自动把它们沉底，这就是「考后→考前」的回流")
