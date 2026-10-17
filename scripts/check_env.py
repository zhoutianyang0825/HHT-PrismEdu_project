# scripts/check_env.py
# M0-T3 环境自检：一键探测 PG / Neo4j / Milvus / LLM API 四项连通性
# 用法：uv run python scripts/check_env.py   （全部 OK 退出码 0，任一 FAIL 退出码 1）

import sys

import httpx
import psycopg
from neo4j import GraphDatabase
from pymilvus import MilvusClient

# ★ 把项目根加进模块搜索路径：脚本是 python scripts/check_env.py 这样直接跑的，
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402


def check_postgres() -> bool:
    """探测 PostgreSQL：能连上并执行 SELECT 1 才算 OK"""
    try:
        # 得到的数据：一个数据库连接（with 块结束自动关闭，不用手动 close）
        # ★ connect_timeout=5：TCP 连不上时最多等 5 秒就放弃；不设的话默认要等
        #   系统级 TCP 超时（可能 75 秒+），自检脚本会卡到怀疑人生
        with psycopg.connect(settings.postgres_dsn, connect_timeout=5) as conn:
            # 要做的操作：执行最简单的探活 SQL，能返回就说明服务与鉴权都正常
            row = conn.execute("SELECT version()").fetchone()
        print(f"[OK] PostgreSQL  {settings.postgres_host}:{settings.postgres_port}  ->  {row[0][:50]}...")
        return True
    
    except Exception as e:
        print(f"[FAIL] PostgreSQL  {e.__class__.__name__}: {e}")
        print("       排查：docker compose up -d postgres 是否执行？端口 5432 是否被占？")
        return False


def check_neo4j() -> bool:
    """探测 Neo4j：verify_connectivity 走完 Bolt 握手 + 鉴权才算 OK"""
    try:
        # ★ GraphDatabase.driver() 是"惰性"的：只创建驱动对象，此刻不发任何网络请求！
        #   密码写错、服务没起，这行都不会报错——所以必须配合下一行 verify_connectivity
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=5,
        )
        # ★ verify_connectivity()：真正连一次 Bolt 端口 + 完成握手 + 校验账号密码。
        #   三关全过不吭声；任何一关失败抛异常。这就是 fail-fast——
        #   把"环境问题"提前到自检环节暴露，而不是业务查询跑到一半才炸
        driver.verify_connectivity()
        info = driver.get_server_info()
        driver.close()
        print(f"[OK] Neo4j        {settings.neo4j_uri}  ->  agent={info.agent}")
        return True
    
    except Exception as e:
        print(f"[FAIL] Neo4j  {e.__class__.__name__}: {e}")
        print("       排查：容器是否起？密码含空格是否与 compose 一致？7687 端口是否通？")
        return False


def check_milvus() -> bool:
    """探测 Milvus：能列出 collection 才算 OK（内部 etcd 就绪前这里会连接失败，等 30~60 秒重试）"""
    try:
        # ★ MilvusClient(uri=...)：pymilvus 的轻量客户端。
        #   传入的是 http:// 开头的地址，但底下走的是 gRPC 协议（19530 是 gRPC 端口），
        #   pymilvus 会把每个方法调用翻译成 protobuf 二进制消息发过去
        client = MilvusClient(uri=settings.milvus_uri)
        names = client.list_collections()   # 全新实例返回 []，能返回本身就说明 gRPC 通了
        client.close()
        print(f"[OK] Milvus       {settings.milvus_uri}  ->  collections={names}")
        return True
    except Exception as e:
        print(f"[FAIL] Milvus  {e.__class__.__name__}: {e}")
        print("       排查：首启需等 30~60 秒；19530 被占则按 compose 注释改映射端口")
        return False


def check_llm() -> bool:
    """探测 LLM API：发一条 1 token 的真实对话请求，验证 base_url + key + model 三者都有效"""
    if not settings.llm_api_key or "填入" in settings.llm_api_key:
        print("[FAIL] LLM API    .env 里 LLM_API_KEY 未填写（cp .env.example .env 后填入）")
        return False
    try:
        # 要做的操作：OpenAI 兼容格式的最小请求；max_tokens=1 省钱省时间
        # ★ httpx.post：发 HTTP 请求，返回 resp（状态码+响应体都在里面）。
        #   注意：状态码是 401/500 时它不抛异常，resp 照样拿到手——判断要靠下一行
        resp = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            timeout=15,
        )
        # ★ raise_for_status()：检查 HTTP 状态码——4xx/5xx 抛异常（带状态码信息），2xx 什么都不做。
        #   为啥必须有它：API 报错时响应体往往也是合法 JSON（比如 {"error": "InvalidApiKey"}），
        #   不检查状态码的话 resp.json() 照样解析"成功"，拿到的是内容为错误信息的假成功，
        #   下游只会看到"choices 为空"，看不出真实原因是鉴权失败
        resp.raise_for_status()        # 非 2xx 直接抛异常，进 except 分支
        data = resp.json()
        # 得到的数据：choices 列表；能取到 content 说明整条链路（官方 API -> 模型）全通
        has_content = bool(data.get("choices"))
        print(f"[OK] LLM API      {settings.llm_base_url}  model={settings.llm_model}  ->  {'通' if has_content else '响应异常'}")
        return has_content
    
    except Exception as e:
        print(f"[FAIL] LLM API  {e.__class__.__name__}: {e}")
        print("       排查：LLM_API_KEY 是否为 DeepSeek 官方 key？base_url 应为 https://api.deepseek.com（不带 /v1 也能通）？")
        return False


def main() -> int:
    print("=" * 60)
    print("HHT-PrismEdu 环境自检（M0-T3）")
    print("=" * 60)
    results = [
        check_postgres(),
        check_neo4j(),
        check_milvus(),
        check_llm(),
    ]
    passed = sum(results)
    print("-" * 60)
    print(f"{passed}/4 passed" + ("  —— M0 done" if passed == 4 else "  —— 存在 FAIL，按上方提示排查"))
    return 0 if passed == 4 else 1


if __name__ == "__main__":
    # ★ sys.exit(main())：把 main() 的返回值变成"进程退出码"。
    #   退出码是给"调用脚本的东西"看的——终端的 $?、CI/CD、未来的 make/docker healthcheck。
    #   惯例：0 = 成功，非 0 = 失败。不加 sys.exit 的话脚本无论成败都返回 0，
    #   "全绿才允许进下一个里程碑"的自动化判断就失效了
    sys.exit(main())
