"""给"只有主观题、没有客观题"的知识点补客观题（填空）→ PG 题库。

为什么要有这个脚本：
    2026-10-26 补完主观题后，有 4 个知识点变成"3 道主观、0 道客观"：
        kp_qe_growth（增长率问题）、kp_qe_area（面积问题）
        kp_func_apply（实际应用-抛物线）、kp_func_undetermined（待定系数法）
    这 4 个原本就是"应用题"类知识点，题库里只有主观题。
    后果：诊断测验有主观题配额（subjective_cap=2），配额一用完，
         这些知识点就一道题都抽不出来 → 整点跳过 → 测验缺口。

    所以每个补 2 道填空：难度一道 easy 一道 medium，答案都是确定数值，
    能被 services/grading.py 的规则判分精确判定，不引入判分歧义。

用法：
    uv run python scripts/seed_objective_fill.py --dry-run   # 只看要写什么
    uv run python scripts/seed_objective_fill.py             # 写库
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import KnowledgePoint, Question
from scripts.seed_subjective_questions import is_duplicate, normalize_stem


# 文案约定：题干里显式写出"填什么形式"，避免"10" vs "10%" 这类判分歧义
FILL_QUESTIONS = [
    # ---- kp_qe_growth 增长率问题 ----
    {
        "kp_id": "kp_qe_growth", "difficulty": "easy",
        "content": "某商品原价 100 元，经过连续两次涨价后售价为 121 元。若两次涨价的百分率相同，"
                   "则每次涨价的百分率为 ______（按「10%」这样的形式填写）。",
        "answer": "10%",
        "analysis": "设每次涨价率为 x：100(1+x)^2=121 → (1+x)^2=1.21 → 1+x=1.1（负根舍去） → x=0.1=10%。",
    },
    {
        "kp_id": "kp_qe_growth", "difficulty": "medium",
        "content": "某工厂 1 月份产值为 50 万元，第一季度总产值为 182 万元。若 2、3 月份产值的月增长率相同，"
                   "则这个月增长率为 ______（按「20%」这样的形式填写）。",
        "answer": "20%",
        "analysis": "设月增长率为 x：50+50(1+x)+50(1+x)^2=182 → 令 t=1+x，t^2+t-3.64=0 的等价式 "
                    "x^2+3x-0.64=0 → x=0.2（负根舍去） → 20%。",
    },

    # ---- kp_qe_area 面积问题 ----
    {
        "kp_id": "kp_qe_area", "difficulty": "easy",
        "content": "一个矩形的长比宽多 3 cm，面积为 40 cm^2，则它的宽为 ______ cm。",
        "answer": "5",
        "analysis": "设宽为 x：x(x+3)=40 → x^2+3x-40=0 → (x+8)(x-5)=0 → x=5（负根舍去）。",
    },
    {
        "kp_id": "kp_qe_area", "difficulty": "medium",
        "content": "用一段长 20 m 的篱笆靠墙围成一个矩形菜地（墙足够长，篱笆只围不靠墙的三边），"
                   "若菜地面积为 50 m^2，则垂直于墙的那条边长为 ______ m。",
        "answer": "5",
        "analysis": "设垂直于墙的边长为 x，则平行于墙的边长为 20-2x：x(20-2x)=50 → x^2-10x+25=0 → (x-5)^2=0 → x=5。",
    },

    # ---- kp_func_apply 实际应用-抛物线 ----
    {
        "kp_id": "kp_func_apply", "difficulty": "easy",
        "content": "小球从地面竖直上抛，离地高度 h（m）与时间 t（s）满足 h=-5t^2+20t，"
                   "则小球能达到的最大高度为 ______ m。",
        "answer": "20",
        "analysis": "h=-5(t^2-4t)=-5(t-2)^2+20，t=2 s 时取最大值 20 m。",
    },
    {
        "kp_id": "kp_func_apply", "difficulty": "medium",
        "content": "某商品每件成本 40 元，当售价为 x 元时，每天可卖出 (200-4x) 件。"
                   "要使每天利润最大，售价应定为 ______ 元。",
        "answer": "45",
        "analysis": "日利润 w=(x-40)(200-4x)=-4x^2+360x-8000=-4(x-45)^2+100，"
                    "x=45 时利润最大（最大 100 元/天）。",
    },

    # ---- kp_func_undetermined 待定系数法 ----
    {
        "kp_id": "kp_func_undetermined", "difficulty": "easy",
        "content": "已知二次函数 y=ax^2+bx+c 的图象经过点 (0,3)、(1,0)、(2,1)，则 a = ______。",
        "answer": "2",
        "analysis": "c=3；代入另外两点：a+b+3=0、4a+2b+3=1 → a+b=-3、2a+b=-1 → a=2、b=-5。",
    },
    {
        "kp_id": "kp_func_undetermined", "difficulty": "medium",
        "content": "已知抛物线与 x 轴交于点 (-1,0) 和 (3,0)，且经过点 (0,-3)，则二次项系数 a = ______。",
        "answer": "1",
        "analysis": "两个 x 轴交点 → 用交点式 y=a(x+1)(x-3)，代 (0,-3)：-3a=-3 → a=1，即 y=x^2-2x-3。",
    },
]


SOURCE_TAG = "补客观题·2026-10-26"


def main() -> None:
    ap = argparse.ArgumentParser(description="给全主观题的知识点补填空题")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        # 知识点 → 章节（questions.chapter_id 是外键，必须带上）
        kp_chapter = dict(session.execute(
            select(KnowledgePoint.id, KnowledgePoint.chapter_id)
        ).all())

        # 现有题干（含所有题型），用于判重
        existing: dict[str, set[str]] = {}
        for kp_id, content in session.execute(
            select(Question.kp_id, Question.content)
        ).all():
            existing.setdefault(kp_id, set()).add(normalize_stem(content)[:40])

        written = 0
        for item in FILL_QUESTIONS:
            kp_id = item["kp_id"]
            if kp_id not in kp_chapter:
                print(f"  ✗ 知识点不存在：{kp_id}")
                continue
            if is_duplicate(item["content"], existing.get(kp_id, set())):
                print(f"  - 已存在，跳过：{item['content'][:30]}…")
                continue

            print(f"  ✓ [{item['difficulty']}] {kp_id}：{item['content'][:34]}…")
            if args.dry_run:
                continue

            session.add(Question(
                subject="math",
                chapter_id=kp_chapter[kp_id],
                kp_id=kp_id,
                question_type="fill",
                difficulty=item["difficulty"],
                content=item["content"],
                answer=item["answer"],
                analysis=item["analysis"],
                source=SOURCE_TAG,
                quality_flag="normal",
            ))
            written += 1

        if args.dry_run:
            session.rollback()
            print("\n（dry-run，未写库）")
        else:
            session.commit()
            print(f"\n[OK] 写库 {written} 道")

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
