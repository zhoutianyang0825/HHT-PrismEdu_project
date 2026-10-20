# streamlit_app/Home.py
# M5-T4 · 前端入口：登录页 + 角色导航。
#
# 【运行】uv run streamlit run streamlit_app/Home.py   （默认 http://localhost:8501）
#
# 【为什么登录放 Home 而不是单独 page】
#   Streamlit 的 pages/ 目录按文件名自动成页，没有"全局路由"概念；
#   把登录 + 会话信息放 Home，每个子页面首行调 require_login()/require_role()
#   自行守卫——这是 Streamlit 官方推荐的最简结构。
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 让 pages 里能 import api_client

import api_client

st.set_page_config(page_title="HHT棱镜课堂", page_icon="🔭", layout="wide")

st.title("🔭 HHT棱镜课堂 / PrismEdu")
st.caption("同一份掌握度数据，折射出学生、老师、教研三个切面")

# ---- 后端地址（演示换机器时在侧边栏改）----
with st.sidebar:
    st.subheader("⚙️ 设置")
    new_base = st.text_input("后端地址", value=api_client.base_url())
    if new_base != api_client.base_url():
        api_client.set_base_url(new_base)
        st.rerun()

# ---- 已登录：展示身份 + 页面导航 ----
me = st.session_state.get("me")
if me:
    st.success(f"已登录：{me['name']}（{me['username']}） · 角色：{'、'.join(me['roles'])}")
    st.page_link("pages/1_学生端.py", label="1️⃣ 学生端", icon="🎒")
    st.page_link("pages/2_老师端.py", label="2️⃣ 老师端", icon="👨‍🏫")
    st.page_link("pages/3_教研端.py", label="3️⃣ 教研端", icon="📊")
    st.page_link("pages/4_管理后台.py", label="4️⃣ 管理后台", icon="🛠️")
    if st.button("退出登录"):
        for k in ("token", "me"):
            st.session_state.pop(k, None)
        st.rerun()
    st.stop()

# ---- 登录表单 ----
st.subheader("登录")
with st.form("login_form"):
    username = st.text_input("用户名", value="")
    password = st.text_input("密码", type="password")
    submitted = st.form_submit_button("登录", use_container_width=True)

if submitted:
    if not username or not password:
        st.error("用户名和密码都要填")
    else:
        try:
            data = api_client.login(username, password)
            st.success(f"欢迎，{data['name']}！角色：{'、'.join(data['roles'])}")
            st.rerun()
        except ValueError as e:
            st.error(str(e))

st.divider()
st.markdown(
    """
**演示账号提示**（由 seed/演示脚本创建，或管理后台 → 用户导入）：

| 用户名 | 角色 | 用途 |
|---|---|---|
| S101 / demo_student | student | 学生端：引导对话、测验、雷达图 |
| T020 | teacher | 老师端：布置作业、主观题确认 |
| R003 | researcher | 教研端：R-04 / R-08 |
| admin | admin | 管理后台：CSV 导入、授课关系 |
"""
)
