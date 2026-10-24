#!/usr/bin/env python3
"""web_app/serve.py · 前端静态服务器（仅开发用，无第三方依赖）

为什么不用 `python3 -m http.server`：
  它会让浏览器缓存 .js 模块，改完代码刷新还是旧逻辑（本项目踩过这个坑）。
  这里统一给 .js/.css/.html 打 Cache-Control: no-store，改完刷新即生效。

用法：
  python3 serve.py                          # 默认 5173；前端自动连 http://<当前主机名>:8100
  python3 serve.py --port 5174              # 换端口
  python3 serve.py --bind 0.0.0.0           # 同一局域网用手机/另一台电脑看
  # 后端不在 8100？不用改脚本，在地址栏加 ?api=http://host:port 即可

启动后终端会打印可点的前端地址，直接点开即可，不需要带任何参数。
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import socketserver
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NO_STORE = {".js", ".mjs", ".css", ".html", ".json"}
API_PORT = 8100   # 与 src/core.js 的 API_PORT 保持一致


def build_version() -> int:
    """给前端一个"当前文件版本"（所有源文件里最新的 mtime）。

    用途：标签页开着的时候你改了代码，页面里的 app.js 会拿这个值对比，
    发现变了就提示刷新——避免"改了没生效其实是标签页没刷新"这种糊弄人的现象。
    0.5 秒缓存，避免每次请求都扫目录。
    """
    global _ver_cache
    now = time.time()
    if _ver_cache and now - _ver_cache[0] < 0.5:
        return _ver_cache[1]
    latest = 0
    for path in [ROOT / "index.html", *ROOT.joinpath("src").rglob("*.js"), *ROOT.joinpath("assets").rglob("*")]:
        try:
            latest = max(latest, int(path.stat().st_mtime_ns))
        except OSError:
            pass
    _ver_cache = (now, latest)
    return latest


_ver_cache: tuple[float, int] | None = None


class Handler(http.server.SimpleHTTPRequestHandler):
    """静态文件 + 关掉前端资源缓存。"""

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/__version":
            body = json.dumps({"v": build_version()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self) -> None:
        # 只对代码/样式做 no-store，图片仍可缓存
        if Path(self.path.split("?")[0]).suffix.lower() in NO_STORE:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # 静音，避免刷屏
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def backend_status(port: int) -> str:
    """起前端时顺手探一下后端，省得对着白页猜。

    只探 /health（不鉴权），失败也不阻止前端启动——前端能跑，
    只是登录会报错，把话说清楚比假装一切正常有用。
    """
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=0.8) as res:
            data = json.load(res)
        if data.get("status") == "ok":
            return f"✓ 已连通（{data.get('service', 'ok')}）"
        return f"⚠ 有响应但不是本项目（{str(data)[:40]}）"
    except urllib.error.HTTPError as exc:
        return f"⚠ 有服务在 {port}，但 /health 返回 {exc.code}（端口被别的程序占了？）"
    except Exception:
        return ("✗ 连不上 —— 先起后端："
                "uv run uvicorn app.main:app --port 8100")


def main() -> int:
    ap = argparse.ArgumentParser(description="HHT 棱镜课堂 · 前端静态服务器")
    ap.add_argument("--port", type=int, default=5173, help="端口，默认 5173")
    ap.add_argument("--bind", default="127.0.0.1", help="监听地址，默认 127.0.0.1；局域网演示用 0.0.0.0")
    args = ap.parse_args()

    if not (ROOT / "index.html").exists():
        print(f"✗ 没找到 {ROOT / 'index.html'}，请在 web_app 目录内运行", file=sys.stderr)
        return 1

    handler = functools.partial(Handler, directory=str(ROOT))

    # 前端 core.js 默认就是「当前主机名:8100」，地址栏不用带 ?api=。
    # 所以前端端口一旦也是 8100，请求会被静态服务器接住（POST 回 501），
    # 看起来像后端坏了。提前拦住。
    if args.port == API_PORT:
        print(
            f"✗ 前端端口不能和后端用同一个（都是 {API_PORT}）。\n"
            f"  换个前端端口：python3 serve.py --port 5173",
            file=sys.stderr,
        )
        return 1

    try:
        httpd = Server((args.bind, args.port), handler)
    except OSError as exc:
        print(f"✗ 端口 {args.port} 起不来：{exc}\n  换个端口：python3 serve.py --port {args.port + 1}", file=sys.stderr)
        return 1

    host = "localhost" if args.bind in ("127.0.0.1", "0.0.0.0") else args.bind
    print("─" * 56)
    print(f"  前端   http://{host}:{args.port}/")
    print(f"  后端   http://{host}:{API_PORT}   ← 默认就直连，地址栏不用带 ?api=")
    print(f"         {backend_status(API_PORT)}")
    print(f"  目录   {ROOT}")
    print("  退出   Ctrl+C")
    print("─" * 56)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
