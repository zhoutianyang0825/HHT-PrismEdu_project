"""给 63 个知识点批量补主观题（解答题）→ PG 题库。

为什么要有这个脚本：
    题库 185 道题里 fill 133 / choice 63 / **subjective 只有 22**，
    而这 22 道只撒在 12 个知识点上——另外 51 个知识点一道解答题都没有。
    结果两个功能同时残废：
      ① 学生端抽题永远是填空/选择，练不到"写过程、说理由"的能力；
      ② 老师端没有主观题可批改，M3 的 HITL 批改链路（LLM 建议 → 老师确认）等于空跑。

做法：
    按知识点逐个调 LLM 出 2~3 道解答题 → 校验 → 去重 → 写库。
    - 每个知识点补到 --per-kp 道（默认 3），已经够数的不动
    - 该知识点已有的题干会塞进 prompt，让模型换角度出题，避免撞题
    - 写库前统一过 validate_item + 去重，坏数据一条都不写（跟 seed_question_bank 同纪律）

用法：
    # 先看质量：只跑前 3 个知识点，不写库
    uv run python scripts/seed_subjective_questions.py --dry-run --limit 3

    # 全量补齐到每个知识点 3 道
    uv run python scripts/seed_subjective_questions.py

    # 只补到 2 道 / 只补某个知识点
    uv run python scripts/seed_subjective_questions.py --per-kp 2
    uv run python scripts/seed_subjective_questions.py --kp kp_vieta --kp kp_peifang

    # 补完顺带把题库向量重建（老师端 RAG 检索才搜得到新题）
    uv run python scripts/seed_subjective_questions.py --milvus
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.llm import get_llm
from app.core.structured import invoke_structured
from app.db.session import SessionLocal
from app.models import Chapter, KnowledgePoint, Question


# 写进 source 字段，便于日后审计/回滚这批数据（DELETE ... WHERE source LIKE 'LLM补主观题%'）
SOURCE_TAG = "LLM补主观题·2026-10-26"

# 主观题的合法难度（与题库原有取值一致）
DIFFICULTIES = ("easy", "medium", "hard")


# =====================================================================
# LLM 输出结构
# =====================================================================
class GeneratedQuestion(BaseModel):
    """一道解答题：题干 + 参考答案 + 解析 + 难度。"""

    content: str = Field(description="题干，必须是一道需要写过程的解答题，不能是填空或选择")
    answer: str = Field(description="参考答案，关键步骤写全，最后给结论（供批改对照采分点）")
    analysis: str = Field(description="解析：切入点、用到的定理/公式、易错点")
    difficulty: str = Field(description="难度，只能是 easy / medium / hard 之一")


class GeneratedBatch(BaseModel):
    """一个知识点的整批题（2~3 道）。"""

    questions: list[GeneratedQuestion] = Field(description="本知识点新出的解答题")


# =====================================================================
# 校验与去重（纯函数，可单测）
# =====================================================================
# LaTeX 记号在系统里会原样漏到前端（见 docs/05 案例），出题阶段就直接禁掉
_LATEX_MARKS = ("\\frac", "\\sqrt", "\\(", "\\)", "\\[", "\\]", "\\pm", "\\times", "\\div")
# 填空/选择的痕迹：出现这些说明模型没按"解答题"出
_CHOICE_MARKS = ("____", "（ ）", "( )", "A.", "B.", "C.", "D.")


def validate_item(item: dict) -> str | None:
    """校验一道生成题，返回错误原因（None = 通过）。

    得到的数据：
        item，形如 {"content": "解方程…", "answer": "x1=2…", "analysis": "…", "difficulty": "medium"}

    要做的操作：
        逐条卡底线：字段齐全 → 题干像解答题 → 答案/解析不是敷衍 → 难度合法 → 没有 LaTeX

    返回：
        None 表示通过；否则返回一句中文原因，调用方直接丢这道题。

    为什么校验要卡这么细：
        LLM 出题最常见的四种翻车——出成填空题（带 ____）、答案只给结论、
        解析写成"略"、难度写成"中等"（中文而不是 easy/medium/hard）。
        这些进了库会在学生端/批改链路里变成脏数据，事后清理比事前拦截贵得多。
    """
    content = (item.get("content") or "").strip()
    answer = (item.get("answer") or "").strip()
    analysis = (item.get("analysis") or "").strip()
    difficulty = (item.get("difficulty") or "").strip()

    if not content or not answer or not analysis:
        return "字段缺失"

    if len(content) < 15:
        return f"题干太短（{len(content)} 字）"
    if len(content) > 400:
        return f"题干太长（{len(content)} 字）"

    if len(answer) < 6:
        return f"答案太短（{len(answer)} 字），不像参考答案"
    if len(analysis) < 12:
        return f"解析太短（{len(analysis)} 字）"

    if difficulty not in DIFFICULTIES:
        return f"难度非法：{difficulty!r}（只能是 easy/medium/hard）"

    for mark in _CHOICE_MARKS:
        if mark in content:
            return f"题干带填空/选择痕迹：{mark!r}"

    for mark in _LATEX_MARKS:
        if mark in content or mark in answer or mark in analysis:
            return f"含 LaTeX 记号：{mark!r}"

    return None


def normalize_stem(text: str) -> str:
    """题干归一化：去空白和标点，只留文字数字，用于判重。

    得到的数据："解方程：x^2 - 5x + 6 = 0。" → 归一化后 "解方程x2-5x6=0"
    要做的操作：删掉所有空白 + 中文/英文标点，统一小写
    返回：归一化串
    为什么：同一个模型批量出题，"解方程 x^2-5x+6=0" 和 "解方程：x² - 5x + 6 = 0。"
             在字面上差别很大，但其实是同一道题——只比前 30 字就够判重了。
    """
    t = str(text or "").lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[，。；：、！？,.;:!?（）()【】\[\]“”\"'’‘]", "", t)
    return t


def is_duplicate(content: str, known_stems: set[str]) -> bool:
    """题干是否与已知题目重复（比归一化后的前 40 字）。"""
    norm = normalize_stem(content)
    return norm[:40] in known_stems


# =====================================================================
# 知识点的现有题况
# =====================================================================
def load_targets(per_kp: int, only: list[str] | None, limit: int | None) -> list[dict]:
    """读出所有知识点 + 各自已有主观题数/题干，算出还差几道。

    返回：
        [{"id","name","chapter_id","chapter_name","have","need","stems": set[str]}, ...]
        只返回 need > 0 的（已经够数的不浪费时间调 LLM）。
    """
    session = SessionLocal()
    try:
        kps = session.execute(
            select(KnowledgePoint.id, KnowledgePoint.name,
                   KnowledgePoint.chapter_id, Chapter.name)
            .join(Chapter, Chapter.id == KnowledgePoint.chapter_id)
            .order_by(KnowledgePoint.chapter_id, KnowledgePoint.id)
        ).all()

        # 每个知识点的主观题：数量 + 全部题干（含填空/选择的题干也拿来防撞题）
        stat = {}
        for kp_id, qtype, content in session.execute(
            select(Question.kp_id, Question.question_type, Question.content)
        ).all():
            rec = stat.setdefault(kp_id, {"subj": 0, "stems": set()})
            if qtype == "subjective":
                rec["subj"] += 1
            rec["stems"].add(normalize_stem(content)[:40])

        targets = []
        for kp_id, name, ch_id, ch_name in kps:
            if only and kp_id not in only:
                continue
            rec = stat.get(kp_id, {"subj": 0, "stems": set()})
            need = max(0, per_kp - rec["subj"])
            targets.append({
                "id": kp_id, "name": name,
                "chapter_id": ch_id, "chapter_name": ch_name or "",
                "have": rec["subj"], "need": need, "stems": rec["stems"],
            })

        targets = [t for t in targets if t["need"] > 0]
        if limit:
            targets = targets[:limit]
        return targets

    finally:
        session.close()


# =====================================================================
# 出题
# =====================================================================
SYSTEM_PROMPT = (
    "你是人教版九年级数学的命题老师，正在给题库补解答题（主观题）。\n"
    "纪律（每条都要守）：\n"
    "1. 必须是解答题：学生要写出过程或理由，不能是填空、不能是选择、不能出现空格线；\n"
    "2. 同一批题的考法要拉开：一道概念辨析/直接应用，一道常规计算或证明，一道稍难的综合或实际应用；\n"
    "3. 难度分别标 easy / medium / hard（英文小写，不是中文）；\n"
    "4. answer 写参考答案：关键步骤要写全（老师批改要照它给采分点），最后给明确结论；\n"
    "5. analysis 写解题思路：从哪切入、用了哪个定理或公式、最容易错在哪；\n"
    "6. 不要出现 LaTeX 记号（如 \\frac \\sqrt \\( \\[ ）；数学式子用 ^ × ÷ √ ≤ ≥ 这类普通符号；\n"
    "7. 需要图形时用文字描述清楚（如「AB 是⊙O 的直径」），不要依赖图片；\n"
    "8. 不要和已给题目重复，换个情境或换个问法。"
)


def build_human_prompt(kp: dict, need: int) -> str:
    """拼人类消息：知识点 + 章节 + 要几道 + 已有题干（防撞题）。"""
    existing = sorted(kp["stems"])[:12]     # 只带前 12 条，prompt 别撑爆
    existed = "\n".join(f"- {s}" for s in existing) if existing else "（这个知识点还没有题）"

    return (
        f"知识点：{kp['name']}（{kp['chapter_id']}，{kp['chapter_name']}）\n"
        f"请出 {need} 道解答题。\n\n"
        f"这个知识点已有的题目（不要重复这些）：\n{existed}"
    )


def generate_for_kp(kp: dict) -> list[dict]:
    """调 LLM 给一个知识点出题，返回原始 dict 列表（还没校验）。

    异常时返回空列表：单个知识点失败不该让整批停摆。
    """
    try:
        batch: GeneratedBatch = invoke_structured(
            get_llm(temperature=0.7),
            GeneratedBatch,
            [
                ("system", SYSTEM_PROMPT),
                ("human", build_human_prompt(kp, kp["need"])),
            ],
        )
        return [q.model_dump() for q in batch.questions]

    except Exception as exc:
        print(f"    ⚠ LLM 调用失败：{type(exc).__name__}: {str(exc)[:120]}")
        return []


# =====================================================================
# 主流程
# =====================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="给知识点批量补主观题")
    ap.add_argument("--per-kp", type=int, default=3, help="每个知识点的主观题目标数（默认 3）")
    ap.add_argument("--kp", action="append", help="只处理指定知识点 id（可重复）")
    ap.add_argument("--limit", type=int, help="只处理前 N 个知识点（调试用）")
    ap.add_argument("--dry-run", action="store_true", help="只生成不写库")
    ap.add_argument("--milvus", action="store_true", help="写库后重建题库向量（老师端检索可见）")
    args = ap.parse_args()

    targets = load_targets(args.per_kp, args.kp, args.limit)

    print(f"[目标] {len(targets)} 个知识点需要补题，每个补到 {args.per_kp} 道")
    if not targets:
        print("[OK] 所有知识点都已达标，无需补题")
        return

    total_need = sum(t["need"] for t in targets)
    print(f"[计划] 共需生成 {total_need} 道\n")

    generated, dropped, written = [], [], 0
    session = SessionLocal()

    try:
        for i, kp in enumerate(targets, 1):
            print(f"[{i}/{len(targets)}] {kp['id']} {kp['name']}："
                  f"现有 {kp['have']} 道，补 {kp['need']} 道")

            items = generate_for_kp(kp)

            # 逐条校验 + 去重（同批内也要去重，模型偶尔会自己撞自己）
            accepted = []
            batch_stems = set(kp["stems"])
            for it in items:
                reason = validate_item(it)
                if reason:
                    dropped.append((kp["id"], it.get("content", "")[:30], reason))
                    print(f"    ✗ 丢弃：{reason}")
                    continue
                if is_duplicate(it["content"], batch_stems):
                    dropped.append((kp["id"], it["content"][:30], "与前题重复"))
                    print("    ✗ 丢弃：与已有/同批题目重复")
                    continue
                batch_stems.add(normalize_stem(it["content"])[:40])
                accepted.append(it)

            for it in accepted:
                generated.append({**it, "kp_id": kp["id"], "chapter_id": kp["chapter_id"]})
                print(f"    ✓ [{it['difficulty']}] {it['content'][:38]}…")

            if accepted and not args.dry_run:
                for it in accepted:
                    session.add(Question(
                        subject="math",
                        chapter_id=kp["chapter_id"],
                        kp_id=kp["id"],
                        question_type="subjective",
                        difficulty=it["difficulty"],
                        content=it["content"].strip(),
                        answer=it["answer"].strip(),
                        analysis=it["analysis"].strip(),
                        source=SOURCE_TAG,
                        quality_flag="normal",
                    ))
                session.commit()
                written += len(accepted)

            time.sleep(0.3)      # 轻微限速，别把 API 打满

    except KeyboardInterrupt:
        session.rollback()
        print("\n[中断] 已回滚未提交的部分")
        raise

    except Exception:
        session.rollback()
        print("[FAIL] 写库失败，已回滚本批")
        raise

    finally:
        session.close()

    # ---- 汇总 ----
    print()
    print("=" * 60)
    print(f"生成 {len(generated)} 道 / 写库 {written} 道 / 丢弃 {len(dropped)} 道"
          + ("（dry-run，未写库）" if args.dry_run else ""))
    if dropped:
        print("丢弃明细：")
        for kp_id, stem, reason in dropped[:20]:
            print(f"  {kp_id} | {stem} | {reason}")

    # ---- 补后复查 ----
    check = SessionLocal()
    try:
        n_subj = check.execute(
            select(func.count()).select_from(Question)
            .where(Question.question_type == "subjective")
        ).scalar_one()
        n_kp_ok = check.execute(
            select(func.count()).select_from(
                select(Question.kp_id).where(Question.question_type == "subjective")
                .group_by(Question.kp_id)
                .having(func.count() >= args.per_kp).subquery()
            )
        ).scalar_one()
        print(f"\n[复查] 主观题共 {n_subj} 道；达到 ≥{args.per_kp} 道的知识点 {n_kp_ok}/63 个")
    finally:
        check.close()

    # ---- 可选：重建题库向量 ----
    if args.milvus and not args.dry_run:
        rebuild_milvus()


def rebuild_milvus() -> None:
    """重建题库 collection：老师端 RAG 检索才搜得到新题。

    为什么整库重建而不是增量插：
        embed_questions() 按全表出向量，Milvus 按主键 q_{id} 写；
        先 drop 再灌，避免"新题 id 与旧数据撞主键"这种半新半旧的状态。
    """
    from pymilvus import MilvusClient

    from app.core.config import settings
    from app.rag.collections import drop_collections, ensure_collections
    from app.rag.ingest import embed_questions

    print("\n[Milvus] 重建题库向量…")
    client = MilvusClient(uri=settings.milvus_uri)
    try:
        drop_collections(client)
        ensure_collections(client)

        session = SessionLocal()
        try:
            rows = embed_questions(session)
        finally:
            session.close()

        for i in range(0, len(rows), 100):
            client.insert(settings.collection_question, rows[i:i + 100])
        client.flush(settings.collection_question)
        print(f"[OK] Milvus 重建完成：{len(rows)} 条")

    finally:
        client.close()


if __name__ == "__main__":
    main()
