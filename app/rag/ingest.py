"""M1-T4 · 入库流水线：题库 → PG + Milvus 双写；教材 PDF → 切片 → textbook_math。

 两个入口：
   ingest_questions(session, rows)        题库（CSV 解析校验在 validate_question_rows）
   ingest_textbook_pdf(client, pdf_path)  教材 PDF 切片入库（500 字/片，overlap 50）

 事务性（M1-T6 单测点）：PG 侧全部在一个事务里，任何一行校验失败整个回滚——
 不允许"导了一半"的脏库。Milvus 侧在 PG 提交成功后才写，失败可整删重灌（数据可再生）。
"""
# 陌生写法 from __future__ import annotations：让所有类型注解变成"字符串延迟求值"——
#   注解里的类名（如下面的 MilvusClient）在函数定义时不再当场解析。
#   必须配下面的 TYPE_CHECKING 用，否则函数签名里的 MilvusClient 会 NameError
from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Chapter, KnowledgePoint, Question

# 陌生写法 TYPE_CHECKING：恒为 False 的特殊常量——这个 if 块运行时永远不会执行，
#   只有 IDE / mypy 这类静态检查工具会"看见"里面的 import。
#   效果：类型注解能引用 pymilvus（IDE 补全/类型检查正常），
#   运行时却完全不加载它（单测环境不用装这个重依赖）
if TYPE_CHECKING:                     # 仅供类型提示，运行时不拉重依赖
    from pymilvus import MilvusClient

# CSV 必备列：缺列直接报错，避免静默导出半残数据
REQUIRED_COLS = {"chapter_id", "kp_id", "question_type", "difficulty",
                 "content", "answer", "quality_flag"}
VALID_QTYPE = {"choice", "fill", "subjective"}
VALID_DIFF = {"easy", "medium", "hard"}
VALID_FLAG = {"normal", "abnormal", "disabled"}


def validate_question_rows(rows: list[dict]) -> list[str]:
    """逐行校验题库数据，返回错误列表（空列表 = 全部通过）。

    得到的数据：CSV 读出的 dict 列表
    要做的操作：查必备列 / 枚举值合法 / 内容与答案非空
    设计：先整体校验再入库 —— 返回任何一条错误，调用方就整批不写，
         这正是 M1-T6 "CSV 导入事务性" 单测要钉住的行为。
    """
    errors: list[str] = []
    if not rows:
        return ["文件没有任何数据行"]
    cols = set(rows[0].keys())
    missing = REQUIRED_COLS - cols
    if missing:
        return [f"缺少必备列: {sorted(missing)}"]

    seen_contents: set[str] = set()
    for i, r in enumerate(rows, start=2):   # start=2：第 1 行是表头
        if r["question_type"] not in VALID_QTYPE:
            errors.append(f"第{i}行 question_type 非法: {r['question_type']!r}")
        if r["difficulty"] not in VALID_DIFF:
            errors.append(f"第{i}行 difficulty 非法: {r['difficulty']!r}")
        if r.get("quality_flag") and r["quality_flag"] not in VALID_FLAG:
            errors.append(f"第{i}行 quality_flag 非法: {r['quality_flag']!r}")
        if not r["content"].strip():
            errors.append(f"第{i}行 content 为空")
        if not r["answer"].strip():
            errors.append(f"第{i}行 answer 为空")
        if r["content"] in seen_contents:
            errors.append(f"第{i}行 题干重复")
        seen_contents.add(r["content"])
    return errors


def ingest_chapters_and_kps(session: Session, graph_json_path: Path) -> tuple[int, int]:
    """图谱 JSON → PG 侧 chapters / knowledge_points 表（Neo4j 侧由 seed_knowledge_graph 负责）。

    幂等：用 merge 语义——存在则更新字段，不存在才插入。
    """
    import json

    graph = json.loads(graph_json_path.read_text(encoding="utf-8"))
    n_ch = n_kp = 0
    for ch in graph["chapters"]:
        row = session.get(Chapter, ch["id"])
        if row is None:
            row = Chapter(id=ch["id"])
            session.add(row)
        row.name, row.subject = ch["name"], graph["subject"]
        row.grade, row.order = ch["grade"], ch["order"]
        n_ch += 1
        for kp in ch["knowledge_points"]:
            krow = session.get(KnowledgePoint, kp["id"])
            if krow is None:
                krow = KnowledgePoint(id=kp["id"])
                session.add(krow)
            krow.name, krow.chapter_id = kp["name"], ch["id"]
            krow.subject, krow.difficulty = graph["subject"], kp["difficulty"]
            krow.description = kp.get("description")
            n_kp += 1
    session.flush()
    return n_ch, n_kp


def ingest_questions(session: Session, rows: list[dict]) -> int:
    """校验通过的题库行 → questions 表（调用方负责 commit / rollback）。"""
    inserted = 0
    for r in rows:
        session.add(Question(
            subject=r.get("subject", "math"),
            chapter_id=r["chapter_id"],
            kp_id=r["kp_id"],
            question_type=r["question_type"],
            difficulty=r["difficulty"],
            content=r["content"],
            answer=r["answer"],
            options=r.get("options") or None,
            analysis=r.get("analysis") or None,
            source=r.get("source") or None,
            quality_flag=r.get("quality_flag", "normal"),
        ))
        inserted += 1
    session.flush()   # 拿到自增 id，供向量侧引用
    return inserted


def embed_questions(session: Session) -> list[dict]:
    """questions 表（本次会话新插入的）→ Milvus 行数据：文本向量化 + metadata。

    得到的数据：db 里已有 id 的 Question
    要做的操作：题目全文（题干+选项+答案+解析）拼成入库文本 → embed → 拼 Milvus 行
    为什么带答案解析：老师检索"判别式相关题"时需要连解析一起看，且题库对学生物理不可见
    """
    from app.rag.embeddings import get_embeddings   # 惰性导入：单测环境不必装 langchain

    embs = get_embeddings()
    questions = session.query(Question).all()
    texts = []
    for q in questions:
        piece = q.content
        if q.options:
            piece += "\n" + q.options
        piece += f"\n【答案】{q.answer}\n【解析】{q.analysis or '略'}"
        texts.append(piece)
    vectors = embs.embed_documents(texts)
    return [
        {
            "id": f"q_{q.id}",
            "text": text[:8000],
            "embedding": vec,
            "kp_id": q.kp_id,
            "chapter_id": q.chapter_id,
            "difficulty": q.difficulty,
            "role_visible": "teacher_only",
            "source": f"题库 #{q.id}（{q.difficulty}）",
        }
        for q, text, vec in zip(questions, texts, vectors)
    ]


def ingest_textbook_pages(client: MilvusClient, pages: list[tuple[str, int]],
                          source_name: str, chunk_size: int = 500, overlap: int = 50) -> int:
    """教材页文本 → 滑窗切片 → 向量化 → textbook_math collection。

    得到的数据：pages = [(第1页全文, 1), (第2页全文, 2), ...]
    切片策略（架构文档 7.1）：一周版按固定长度滑窗（500 字、overlap 50），
    metadata.page 记页码供溯源（C-02 验收：出处=文件名+页码）。
    """
    from app.rag.collections import ensure_collections   # 惰性导入：单测不碰 pymilvus
    from app.rag.embeddings import get_embeddings

    ensure_collections(client)
    embs = get_embeddings()

    # ★ id 必须带源文件指纹：seq 每次调用都从 0 数起，
    #   灌第二个 PDF 时 tb_0/tb_1 会和第一个撞主键（Milvus 允许重复 PK → 检索出重复结果）
    # 陌生库 hashlib：标准库的哈希摘要模块。md5(bytes) 把任意输入压成固定 32 位
    #   十六进制指纹——输入差一个字，指纹完全不同。这里取前 8 位当文件"身份证"
    import hashlib
    src_tag = hashlib.md5(source_name.encode("utf-8")).hexdigest()[:8]

    docs: list[dict] = []
    seq = 0
    for text, page_no in pages:
        text = text.strip()
        if not text:
            continue
        start = 0
        while start < len(text):
            piece = text[start:start + chunk_size]
            if len(piece) < 20:        # 碎片太小没有检索价值
                break
            docs.append({
                "id": f"tb_{src_tag}_{seq}",
                "text": piece,
                "page": page_no,
                "source": f"{source_name} 第{page_no}页",
                "chapter_id": "",      # 页级切片暂不绑章节，二期按目录回填
                "role_visible": "all",  # 教材三端可见
            })
            seq += 1
            start += chunk_size - overlap

    vectors = embs.embed_documents([d["text"] for d in docs])
    client.insert(settings.collection_textbook,
                  [{**d, "embedding": v} for d, v in zip(docs, vectors)])
    return len(docs)


def ingest_textbook_pdf(client: MilvusClient, pdf_path: Path) -> int:
    """PDF 教材：pypdf 提取各页文本后交给 ingest_textbook_pages。"""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pages = []
    for page_no, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append((text, page_no))
    return ingest_textbook_pages(client, pages, source_name=pdf_path.name)


def load_question_csv(csv_path: Path) -> list[dict]:
    """读题库 CSV 为 dict 列表（UTF-8，带 BOM 兼容）。"""
    with csv_path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))
