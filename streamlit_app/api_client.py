# streamlit_app/api_client.py
# M5-T4 · 前端统一 API 客户端：token 注入、SSE 流读取、错误提示，四个页面共用。
#
# 【为什么收口成一个文件】
#   四个页面都要"带 token 调接口 + 统一处理 401/403"。散在各页写四遍，
#   改一次 header 就要改四处——收口到 `api()` 一个函数。
#
# 【SSE 的前端读取】
#   httpx.stream 逐行读响应体，行格式 `data: {json}\n\n`；
#   按 type 分发：interrupt → 返回给页面渲染确认框；message → 打字区；
#   error → st.error；end → 结束。后端 app/api/student.py 的协议注释是唯一契约。
import json

import httpx
import streamlit as st

# 后端地址：侧边栏可改（演示换机器用），默认本机 8000
DEFAULT_BASE = "http://localhost:8000"


def base_url() -> str:
    return st.session_state.get("api_base", DEFAULT_BASE)


def set_base_url(url: str) -> None:
    st.session_state["api_base"] = url.rstrip("/")


def _headers() -> dict:
    """带 JWT 的请求头。没登录（无 token）就给空头，让 401 走统一提示。"""
    token = st.session_state.get("token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _handle(resp: httpx.Response):
    """统一响应处理：401 提示重登 / 403 越权提示 / 其他非 2xx 抛出详情。"""
    if resp.status_code == 401:
        st.error("登录已失效，请重新登录")
        st.stop()
    if resp.status_code == 403:
        detail = _detail_of(resp)
        st.error(f"权限不足：{detail}")
        st.stop()
    if resp.status_code >= 400:
        st.error(f"请求失败 [{resp.status_code}]：{_detail_of(resp)}")
        st.stop()
    return resp.json() if resp.content else None


def _detail_of(resp: httpx.Response) -> str:
    try:
        return resp.json().get("detail", resp.text[:200])
    except Exception:
        return resp.text[:200]


def api(method: str, path: str, json_body: dict | None = None, params: dict | None = None):
    """普通 REST 调用。method 如 "GET"/"POST"；path 如 "/api/student/homework"。"""
    url = f"{base_url()}{path}"
    try:
        with httpx.Client(timeout=120) as client:   # 120s：analyze/exam 等 LLM 长任务
            resp = client.request(method, url, json=json_body, params=params, headers=_headers())
        return _handle(resp)
    except httpx.ConnectError:
        st.error(f"连不上后端 {base_url()}，请先启动：uv run uvicorn app.main:app --port 8000")
        st.stop()


def login(username: str, password: str) -> dict:
    """登录：成功把 token/roles/student_id 写进 session_state。"""
    url = f"{base_url()}/api/auth/login"
    with httpx.Client(timeout=15) as client:
        resp = client.post(url, json={"username": username, "password": password})
    if resp.status_code != 200:
        raise ValueError(_detail_of(resp))
    data = resp.json()
    st.session_state["token"] = data["access_token"]
    st.session_state["me"] = data
    return data


def stream_chat(path: str, body: dict, on_event) -> None:
    """SSE 消费器：学生端对话专用。

    得到的数据：path="/api/student/answer/stream" 或 "/resume"；body 为请求体；
                on_event(type, payload) 是页面给的回调（渲染一行/更新状态）
    要做的操作：httpx.stream 逐行读 → 按 "data: " 前缀切帧 → json 解析 → 分发
    断流保护：ConnectError/读超时也走 on_event("error", ...)，页面不白屏
    """
    url = f"{base_url()}{path}"
    try:
        with httpx.stream("POST", url, json=body, headers=_headers(), timeout=180) as resp:
            if resp.status_code != 200:
                resp.read()
                on_event("error", {"message": _detail_of(resp)})
                return
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue                     # SSE 规范里的注释/空行，跳过
                event = json.loads(line[len("data: "):])
                on_event(event.get("type"), event.get("payload") or event)
                if event.get("type") == "end":
                    break
    except Exception as exc:   # noqa: BLE001
        on_event("error", {"message": f"连接中断：{exc}"})


def require_login() -> dict:
    """页面守卫：未登录直接停页并给指引（四个页面首行都调它）。"""
    me = st.session_state.get("me")
    if not me:
        st.warning("请先在首页登录")
        st.stop()
    return me


def require_role(*roles: str) -> dict:
    """角色守卫：进入页面要求命中任一角色（admin 恒放行）。"""
    me = require_login()
    if "admin" not in me["roles"] and not (set(me["roles"]) & set(roles)):
        st.error(f"本页面需要 {'/'.join(roles)} 角色，你的角色是 {'、'.join(me['roles'])}")
        st.stop()
    return me
