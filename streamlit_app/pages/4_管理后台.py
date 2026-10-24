# streamlit_app/pages/4_管理后台.py
# M5-T4 · A-01 管理后台：6 项功能按 P0（导入/CRUD/授课关系）→ P1（任命/班级/日志）排布。
#
# 【页面编排即优先级】PRD 2.2 定的 P0 三项放在最上面，时间紧砍尾时从下往上砍。
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api_client

st.set_page_config(page_title="管理后台 · HHT棱镜课堂", page_icon="🛠️", layout="wide")
me = api_client.require_role("admin")

st.title("🛠️ A-01 管理后台")

# ================================================================
# P0-1 · CSV 批量导入（逐行校验 / 全过才写 / 失败报告带行号）
# ================================================================
st.subheader("📥 P0 · 组织账户 CSV 批量导入（大宽表）")
st.caption("格式：username,password,name,roles,classes,student_no,lead_grades　|　"
           "classes=年级:班名（如 初三:1班，多班用分号）；student 必填 classes；"
           "researcher 必填 lead_grades（年级）；多角色各列都填。任教关系/任命小批量也可用下方手动窗口")
if st.button("下载模板", key="tpl"):
    import httpx
    r = httpx.get(f"{api_client.base_url()}/api/admin/users/import-template",
                  headers={"Authorization": f"Bearer {st.session_state.get('token')}"})
    st.code(r.text, language="csv")

csv_text = st.text_area("粘贴 CSV 内容", height=140,
                        value="username,password,name,roles,classes,student_no,lead_grades\n"
                              "S201,Passw0rd1,王小红,student,初三:1班,2026201,\n"
                              "T021,Passw0rd2,赵老师,teacher,初三:1班,,\n")
if st.button("开始导入", type="primary"):
    out = api_client.api("POST", "/api/admin/users/import", json_body={"csv_content": csv_text})
    if out.get("ok"):
        st.success(f"导入成功 {out['imported']} 人（学生档案 {out.get('students', 0)} / "
                   f"任教关系 {out.get('teaching_assignments', 0)} / 教研任命 {out.get('research_leads', 0)}）")
        st.dataframe(out.get("items", []), use_container_width=True)
    else:
        # 事务性展示：整批未写 + 每条坏行的行号与原因
        st.error(f"整批未导入：{out.get('message', '')}")
        for e in out.get("errors", []):
            st.write(f"- 第 {e['line']} 行：{e['reason']}")

# ================================================================
# P0-2 · 用户 CRUD
# ================================================================
st.subheader("👥 P0 · 用户管理")
c1, c2, c3 = st.columns(3)
kw = c1.text_input("搜索（用户名/姓名）", key="u_kw")
role_f = c2.selectbox("按角色过滤", ["", "student", "teacher", "researcher", "admin"])
page = c3.number_input("页码", 1, 100, 1)
users = api_client.api("GET", "/api/admin/users",
                       params={"keyword": kw or None, "role": role_f or None,
                               "page": int(page), "size": 20})
st.write(f"共 {users.get('total', 0)} 人")
if users.get("items"):
    st.dataframe(users["items"], use_container_width=True)

with st.expander("➕ 新建用户", expanded=False):
    with st.form("new_user"):
        n1, n2 = st.columns(2)
        nu_name = n1.text_input("用户名")
        nu_pwd = n2.text_input("密码（≥8 位含字母数字）", type="password")
        nu_real = n1.text_input("姓名")
        nu_roles = n2.multiselect("角色", ["student", "teacher", "researcher", "admin"])
        nu_grade = n1.text_input("researcher 任命年级（勾 researcher 必填）", value="初二")
        nu_subj = n2.text_input("任命学科", value="math")
        if st.form_submit_button("创建", use_container_width=True):
            body = {"username": nu_name, "password": nu_pwd, "name": nu_real,
                    "roles": nu_roles}
            if "researcher" in nu_roles:
                body["research_leads"] = [{"grade": nu_grade, "subject": nu_subj}]
            out = api_client.api("POST", "/api/admin/users", json_body=body)
            st.success(f"已创建 #{out['id']} {out['username']}（{'、'.join(out['roles'])}）")

with st.expander("✏️ 修改 / 停用用户", expanded=False):
    uid = st.number_input("用户 id", min_value=1, key="edit_uid")
    e1, e2, e3 = st.columns(3)
    new_active = e2.selectbox("状态", ["保持不变", "启用", "停用"])
    new_pwd = e3.text_input("重置密码（可空）", type="password")
    if st.button("提交修改", key="u_edit"):
        body = {}
        if new_active == "启用":
            body["is_active"] = True
        elif new_active == "停用":
            body["is_active"] = False
        if new_pwd:
            body["password"] = new_pwd
        if not body:
            st.warning("没有要改的字段")
        else:
            api_client.api("PUT", f"/api/admin/users/{int(uid)}", json_body=body)
            st.success("已更新（停用账号下一次请求即 401）")

# ================================================================
# P0-3 · 授课关系（teacher 明细 scope 的数据源，改完权限即时生效）
# ================================================================
st.subheader("🧑‍🏫 P0 · 授课关系（改完即时生效，无需重新登录）")
tas = api_client.api("GET", "/api/admin/teaching-assignments")
if tas:
    st.dataframe(tas, use_container_width=True)

c1, c2 = st.columns(2)
with c1:
    with st.form("add_ta"):
        st.markdown("**新增授课关系**")
        t_id = st.number_input("教师 users.id", min_value=1, key="ta_t")
        c_id = st.number_input("班级 id", min_value=1, key="ta_c")
        subj = st.text_input("学科", value="math")
        if st.form_submit_button("添加", use_container_width=True):
            api_client.api("POST", "/api/admin/teaching-assignments",
                           json_body={"teacher_id": int(t_id), "class_id": int(c_id),
                                      "subject": subj})
            st.success("已添加")
with c2:
    del_id = st.number_input("删除授课关系：输入关系 id", min_value=1, key="ta_del")
    if st.button("删除", key="ta_del_btn"):
        api_client.api("DELETE", f"/api/admin/teaching-assignments/{int(del_id)}")
        st.success("已删除")

# ================================================================
# P1-1 · 组长任命
# ================================================================
st.subheader("🎖️ P1 · 教研组长任命（researcher 聚合 scope 的数据源）")
leads = api_client.api("GET", "/api/admin/research-leads")
if leads:
    st.dataframe(leads, use_container_width=True)
c1, c2 = st.columns(2)
with c1:
    with st.form("add_rl"):
        rl_u = st.number_input("用户 users.id", min_value=1)
        rl_g = st.text_input("年级", value="初二")
        rl_s = st.text_input("学科", value="math")
        if st.form_submit_button("任命", use_container_width=True):
            api_client.api("POST", "/api/admin/research-leads",
                           json_body={"user_id": int(rl_u), "grade": rl_g, "subject": rl_s})
            st.success("已任命")
with c2:
    rl_del = st.number_input("撤销任命：输入任命 id", min_value=1, key="rl_del")
    if st.button("撤销", key="rl_del_btn"):
        api_client.api("DELETE", f"/api/admin/research-leads/{int(rl_del)}")
        st.success("已撤销")

# ================================================================
# P1-2 · 班级管理
# ================================================================
st.subheader("🏫 P1 · 班级管理")
classes = api_client.api("GET", "/api/admin/classes")
st.dataframe(classes, use_container_width=True)
c1, c2 = st.columns(2)
with c1:
    with st.form("add_cls"):
        cg = st.text_input("年级", value="初二")
        cn = st.text_input("班名", value="3班")
        if st.form_submit_button("建班", use_container_width=True):
            api_client.api("POST", "/api/admin/classes",
                           json_body={"grade": cg, "name": cn})
            st.success("已建班")
with c2:
    cls_del = st.number_input("删班：输入班级 id", min_value=1, key="cls_del")
    if st.button("删班", key="cls_del_btn"):
        api_client.api("DELETE", f"/api/admin/classes/{int(cls_del)}")
        st.success("已删除（班里有人会报 409，先迁移）")

# ================================================================
# P1-3 · 审计日志（只读）
# ================================================================
st.subheader("📜 P1 · 审计日志")
logs = api_client.api("GET", "/api/admin/audit-logs", params={"page": 1, "size": 30})
if logs:
    st.dataframe(logs, use_container_width=True)
