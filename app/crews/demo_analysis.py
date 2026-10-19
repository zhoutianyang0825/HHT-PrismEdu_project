"""M4-T6 · R-04/R-08 演示入口。

 用法：
   uv run python -m app.crews.demo_analysis           # R-04：合成长表 → 算指标 → crew 三角色
   uv run python -m app.crews.demo_analysis --exam    # R-08：出一张月考卷（写 exam_papers，draft）
 依赖：R-04 需 .env 的 LLM_API_KEY + Neo4j/PG 容器（专家的工具要查库）；R-08 还会写 exam_papers。
"""
import csv
import json
import random
import sys
from pathlib import Path

# __file__ 是当前文件路径；parents[2] 往上三层 = 项目根（app/crews/demo_analysis.py → app/crews → app → 根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 让 `python -m` 与 `python 文件路径` 两种跑法都能 import app.*
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.analysis import (  # noqa: E402  import 必须放在 sys.path 调整之后
    attach_kp_ids,
    compute_item_metrics,
    pick_abnormal,
)


def load_q_kp_map(n: int = 25) -> dict[int, str]:
    """读题库 CSV 前 n 题的"题号 → 知识点"映射。

    得到的数据：n=25
    要做的操作：csv.DictReader 逐行读，用行号（从 1 起）当题号
    返回：{1: "kp_power_ops", 2: "kp_power_ops", 3: "kp_poly_mult", ...}
    为什么：demo 不查库也要给指标补 kp_id；M1 种子脚本按 CSV 行序插入，自增 id 与行号一致
    """
    csv_path = PROJECT_ROOT / "data" / "raw" / "question_bank.csv"
    with open(csv_path, encoding="utf-8") as f:
        # 陌生类 DictReader：按表头把每行转成 dict，之后用 r["kp_id"] 取值，比列下标稳
        rows = list(csv.DictReader(f))
    return {i + 1: r["kp_id"] for i, r in enumerate(rows[:n])}


def synthesize_answers(n_students: int = 40, n_questions: int = 25,
                       full_per_q: float = 4.0) -> list[dict]:
    """合成一张卷的作答长表，并埋好三个异常题。

    得到的数据：n_students=40；n_questions=25；full_per_q=4.0（每题满分）
    要做的操作：按"学生能力 × 题目难度"造分，Q07 压到 P≈0.25、Q13 抬到 P≈0.93、Q19 与能力解耦
    返回：[{"student_id": 1, "q_id": 1, "score": 2.6, "full_score": 4.0}, ...]（40×25=1000 行）
    为什么：演示与单测都要"已知异常"的数据——指标算得对不对，一眼能对上
    """
    # 陌生类 random.Random：独立随机数发生器，种子固定 → 每次跑 demo 得到同一份数据
    rng = random.Random(42)
    ability = [rng.random() for _ in range(n_students)]               # 学生能力 0~1
    base_rate = [rng.uniform(0.55, 0.8) for _ in range(n_questions)]  # 题目基础得分率

    records = []
    for sid in range(1, n_students + 1):
        for qid in range(1, n_questions + 1):
            if qid == 7:                          # 埋点1：偏难（全员低分）
                rate = rng.uniform(0.12, 0.38)
            elif qid == 13:                       # 埋点2：过易（全员高分）
                rate = rng.uniform(0.9, 0.99)
            elif qid == 19:                       # 埋点3：与能力无关 → 区分度≈0
                rate = rng.uniform(0.3, 0.9)
            else:                                 # 正常题：能力越强得分率越高
                rate = min(0.98, base_rate[qid - 1] * (0.45 + 0.75 * ability[sid - 1]))
            # gauss 加噪声更接近真实分布；min/max 把得分率夹在 0~1，防负分/超满分
            score = full_per_q * min(1.0, max(0.0, rng.gauss(rate, 0.06)))
            records.append({
                "student_id": sid, "q_id": qid,
                "score": round(score, 1), "full_score": full_per_q,
            })
    return records


def run_analysis_demo() -> None:
    """R-04 演示：合成数据 → 本地算指标 → crew 三角色 → 打印报告。

    得到的数据：无入参
    要做的操作：synthesize_answers → compute_item_metrics → attach_kp_ids → run_paper_analysis
    返回：无（直接打印到终端）
    为什么：M4 的验收判据就是"跑这个命令能看到异常题被点名 + 教研建议"
    """
    print("=" * 64)
    print("R-04 试卷分析演示（合成 2 个虚拟班 × 25 题，固定种子可复现）")
    print("=" * 64)

    records = synthesize_answers()
    metrics = compute_item_metrics(records)               # 纯 Pandas：无 LLM、无 DB
    metrics = attach_kp_ids(metrics, load_q_kp_map())

    # 先把本地指标打出来：crew 的输入原料，可对着手算核对
    print("\n[本地 Pandas 指标]（异常阈值：P<0.3 过难 / P>0.9 过易 / D<0.2 区分度不足）")
    for r in metrics:
        if r["is_abnormal"]:
            print(f"  Q{r['q_id']:>02}  P={r['P']:.2f}  D={r['D']:.2f}  kp={r['kp_id']:<16} 异常：{'、'.join(r['issues'])}")
    print(f"  …… 其余 {sum(1 for r in metrics if not r['is_abnormal'])} 题指标正常")

    print("\n[启动 analysis_crew：数据分析师 → 命题质量专家 → 教研组长]（要调 LLM，稍等）")
    from app.crews.analysis_crew import run_paper_analysis

    report = run_paper_analysis(metrics, title="初三数学合成演示卷", grade="初三")

    print("\n[教研组长 · 结构化报告]")
    print(f"  概况：{report.overview}")
    for it in report.abnormal_items:
        print(f"  异常题 Q{it.q_id}（{it.kp_id}）：{it.issue} — {it.cause}")
    print("  建议：")
    for i, s in enumerate(report.teaching_suggestions, 1):
        print(f"   {i}. {s}")


def run_exam_demo() -> None:
    """R-08 演示：真实出一张月考卷并打印配额表/选题/审核报告。

    得到的数据：无入参（固定 monthly + 初三 + math）
    要做的操作：调 run_exam_generation（写 exam_papers，status=draft），分三段打印
    返回：无（打印到终端）
    为什么：让"配额表可打印、缺口可追溯、审核结论可读"在一屏里看到
    """
    print("=" * 64)
    print("R-08 试卷生成演示（月考巩固型，写 exam_papers 表）")
    print("=" * 64)

    from app.crews.exam_crew import run_exam_generation

    result = run_exam_generation(exam_type="monthly", grade="初三", subject="math")

    print(f"\n[paper_id={result['paper_id']}] 配额表（QuotaPlan，可人工改后重抽）：")
    for it in result["plan"]["items"]:
        print(f"  {it['kp_id']:<16} {it['question_count']}题 {it['difficulty']:<6} "
              f"{it['question_type']:<10} —— {it['reason']}")

    print(f"\n选题结果：{len(result['question_ids'])} 题 → {result['question_ids']}")
    if result["gaps"]:
        print("缺口清单（宁可少题不乱补）：")
        for g in result["gaps"]:
            print(f"  - {g}")

    print("\n审核报告：")
    print(f"  passed={result['review']['passed']}  rounds={result['review']['rounds_used']}")
    print(f"  指标：{json.dumps(result['review']['metrics'], ensure_ascii=False)}")
    for p in result["review"]["problems"]:
        print(f"  问题：{p}")
    print(f"  结论：{result['review']['summary']}")


if __name__ == "__main__":
    if "--exam" in sys.argv:
        run_exam_demo()
    else:
        run_analysis_demo()
