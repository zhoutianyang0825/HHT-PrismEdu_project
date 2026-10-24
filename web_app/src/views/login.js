/* ============================================================================
 * views/login.js · 首页（仅登录）
 *   不做角色选择、不放演示账号、不暴露后端地址 —— 登录成功后按 roles 自动落地：
 *     admin → 管理后台 / teacher → 老师端 / researcher → 教研端 / student → 学生端
 * ========================================================================== */
import { login, toast, busy } from "../core.js";
import { defaultRoute } from "../shell.js";

export function renderLogin(root, { me }) {
  root.innerHTML = `
    <div class="auth">
      <div class="auth-head">
        <div class="auth-mark">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 3.4 20.4 19H3.6Z" fill="rgba(255,255,255,.96)"/>
            <path d="M12 6.6 16.9 15.6H7.1Z" fill="rgba(255,255,255,.42)"/>
            <path d="M12 3.4v16" stroke="rgba(255,255,255,.55)" stroke-width="1.1"/>
          </svg>
        </div>
        <h1>HHT 棱镜课堂</h1>
        <p>同一份掌握度数据，在学生、老师、教研三个端面<br/>折射出不同的光。</p>
      </div>

      <form class="auth-card" id="login-form" autocomplete="on" novalidate>
        <div class="field">
          <label for="username">用户名</label>
          <input class="input" id="username" name="username" autocomplete="username"
                 placeholder="请输入账号" autofocus required />
        </div>
        <div class="field">
          <label for="password">密码</label>
          <input class="input" id="password" name="password" type="password"
                 autocomplete="current-password" placeholder="请输入密码" required />
        </div>
        <div class="auth-error" id="login-error" role="alert"></div>
        <button class="btn btn-primary btn-lg btn-block" type="submit" id="login-submit">登录</button>
      </form>

      <div class="auth-foot">
        账号由学校管理员统一分配，不开放自主注册<br/>
        PrismEdu · 学情驱动的自适应学习闭环
      </div>
    </div>`;

  const form = root.querySelector("#login-form");
  const errBox = root.querySelector("#login-error");
  const submit = root.querySelector("#login-submit");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = form.username.value.trim();
    const password = form.password.value;

    errBox.classList.remove("show");
    if (!username || !password) {
      errBox.textContent = "请输入用户名和密码";
      errBox.classList.add("show");
      return;
    }

    const restore = busy(submit, "正在登录…");
    try {
      const data = await login(username, password);
      toast(`欢迎回来，${data.name}`, "ok");
      const target = defaultRoute({ roles: data.roles });
      if (location.hash === target) window.dispatchEvent(new HashChangeEvent("hashchange"));
      else location.hash = target;
    } catch (err) {
      restore();
      errBox.textContent = err.detail || "登录失败，请稍后重试";
      errBox.classList.add("show");
    }
  });
}
