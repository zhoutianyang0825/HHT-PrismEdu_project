"""M1-T4 · 题库 seed 脚本：题库 CSV → PG（事务）+ Milvus 双写；顺带灌章节/知识点表。

 完成判据对应：
   uv run python scripts/seed_question_bank.py         # 185 题 -> PG + Milvus
   uv run python scripts/seed_question_bank.py --wipe  # PG 清空重灌 + 重建两个 collection

 事务纪律：
   ① 先 validate_question_rows 整体校验，有任何坏行 → 整批不写（M1-T6 单测钉住的行为）
   ② PG 在一个事务里 commit；Milvus 写在 PG 提交成功之后——
      Milvus 失败不影响 PG（题库结构化数据以 PG 为真源，向量库可随时重灌）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from pymilvus import MilvusClient

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import Chapter, KnowledgePoint, Question
from app.rag.collections import drop_collections, ensure_collections
from app.rag.ingest import (
    embed_questions,
    ingest_chapters_and_kps,
    ingest_questions,
    load_question_csv,
    validate_question_rows,
)

BASE = Path(__file__).resolve().parents[1]
CSV_PATH = BASE / "data" / "raw" / "question_bank.csv"
GRAPH_JSON = BASE / "data" / "knowledge_graph" / "math.json"


def main() -> None:
    wipe = "--wipe" in sys.argv

    # 1) 读 CSV + 整体校验：有坏行直接终止，一行都不进库
    rows = load_question_csv(CSV_PATH)
    errors = validate_question_rows(rows)
    if errors:
        print(f"[FAIL] 题库 CSV 校验未通过（{len(errors)} 处），整批不入库：")
        for e in errors[:20]:
            print("   -", e)
        sys.exit(1)
    print(f"[OK] CSV 校验通过：{len(rows)} 题")

    # 2) PG 事务写入：chapters / knowledge_points / questions
    # 陌生点 SessionLocal：db/session.py 里的会话工厂，调一次产出一个 session
    #   （一次数据库对话 = N 条语句 + 1 个事务）。脚本场景没有 FastAPI 的
    #   get_db 依赖帮我们管，所以下面手动 try/rollback/finally 三件套伺候
    session = SessionLocal()
    try:
        if wipe:
            # 重建语义：按 FK 依赖链整库清平。
            # ★ 坑（2026-10-18 实跑踩到）：只 DELETE questions/kps/chapters 会被
            #   student_answers 等子表的 FK 拦住（ForeignKeyViolation）——引用 questions
            #   的表还有 student_answers/error_logs/bkt_state/diagnostic_quizzes/
            #   homework_questions/mastery_records 等，逐个 delete 既啰嗦又容易漏。
            # TRUNCATE ... CASCADE：PG 自动把【引用这三张父表】的子表一并截断，
            #   RESTART IDENTITY 顺便把自增序列归零（重灌后 id 从 1 开始，和 CSV 干净对齐）。
            #   users/students 没被列进来也不引用它们，保留不动。
            session.execute(text(
                "TRUNCATE questions, knowledge_points, chapters RESTART IDENTITY CASCADE"
            ))
            print("[wipe] PG 已清平 questions/kps/chapters 及全部引用子表（CASCADE）")
        n_ch, n_kp = ingest_chapters_and_kps(session, GRAPH_JSON)
        n_q = ingest_questions(session, rows)
        session.commit()
        print(f"[OK] PG 写入完成：chapters {n_ch} / knowledge_points {n_kp} / questions {n_q}")
    except Exception:
        session.rollback()
        print("[FAIL] PG 写入失败，已回滚（不会留下半批数据）")
        raise
    finally:
        session.close()



    # 3) Milvus 写入（PG 成功后才执行）
    # 陌生类 MilvusClient：pymilvus 2.4 的官方客户端，连接 Milvus standalone。
    #   uri=http://localhost:19530 是 Milvus 的 gRPC 端口，不是网页端口
    client = MilvusClient(uri=settings.milvus_uri)
    if wipe:
        drop_collections(client)
        print("[OK] 已删除旧 collection")
    ensure_collections(client)

    session = SessionLocal()
    try:
        milvus_rows = embed_questions(session)   # 从 PG 读（含刚提交的题），向量化
    finally:
        session.close()

    # Milvus 单次 insert 有上限（默认 64MB），185 题带向量分批灌更稳。
    # milvus_rows[i:i+BATCH] 是切片：每 100 条取一段，range 步长正好跳到下一段开头
    BATCH = 100
    for i in range(0, len(milvus_rows), BATCH):
        client.insert(settings.collection_question, milvus_rows[i:i + BATCH])


    # 陌生函数 flush：强制把缓冲区里的数据落盘成"可见"状态，
    # Milvus insert 是先进缓冲，flush 前检索可能查不到刚插的数据；
    # seed 结尾 再flush 一次保证自检查得到
    client.flush(settings.collection_question)
    print(f"[OK] Milvus 写入完成：collection={settings.collection_question}，{len(milvus_rows)} 条")



    # 4) 自检：teacher 视角搜一条"判别式"，确认 expr 过滤与检索可用
    from app.rag.retriever import retrieve

    hits = retrieve(client, "如何判断一元二次方程根的情况", role="teacher", top_k=3)
    print(f"[自检] teacher 检索 top3：")
    for h in hits:
        print(f"   score={h.score:.3f} {h.source} | {h.text[:40]}...")

    student_hits = retrieve(client, "如何判断一元二次方程根的情况", role="student", top_k=3)
    from_collections = {h.collection for h in student_hits}

    if settings.collection_question in from_collections:
        print("[FAIL] student 检索到了题库 collection，权限过滤失效！")
        sys.exit(1)

    print("[自检] student 只能命中 textbook_math，题库对学生不可见 ✓")


if __name__ == "__main__":
    main()
