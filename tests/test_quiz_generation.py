"""M2 交付自检 · S-10 测验生成（开发计划 M2 完成判据的两个测试 + 配额截断）。

纯逻辑单测：只打 quiz_graph.rank_targets 和 selection_engine.select_quiz_questions
两个纯函数，不碰 PG / Neo4j / LLM——跑不起容器的环境也能验证选题规则。
运行：uv run pytest tests/test_quiz_generation.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.graphs.student.quiz_graph import rank_targets
from app.services.selection_engine import difficulty_for, select_quiz_questions


def _cand(qid: int, kp: str, diff: str, qtype: str = "fill") -> dict:
    """造一道候选题的最小形态（与 fetch_candidates_by_kp 返回结构对齐）。

    qtype 默认 fill（客观题）：老用例都是纯难度/配额断言，不关心题型。
    """
    return {"id": qid, "kp_id": kp, "difficulty": diff, "question_type": qtype}


def test_quiz_targets_weak_kps():
    """判据一：生成的题目知识点 100% 落在目标知识点集合内。"""
    targets = rank_targets(
        error_kp_ids=["kp_crossmult", "kp_peifang", "kp_strong"],
        error_freq={"kp_crossmult": 3, "kp_peifang": 1},
        p_known_map={"kp_crossmult": 0.20, "kp_peifang": 0.45, "kp_strong": 0.85},
    )
    candidates = {
        "kp_crossmult": [_cand(1, "kp_crossmult", "easy"), _cand(2, "kp_crossmult", "easy")],
        "kp_peifang":   [_cand(3, "kp_peifang", "medium")],
        "kp_strong":    [_cand(4, "kp_strong", "hard")],
        # 陷阱：kp_outside 不在目标集里，就算题库有题也绝不能被选中
        "kp_outside":   [_cand(99, "kp_outside", "easy")],
    }
    picked, gaps = select_quiz_questions(targets, candidates, total=8)

    target_ids = {tp["kp_id"] for tp in targets}
    assert all(p["kp_id"] in target_ids for p in picked), "有题落在了目标知识点之外"
    assert all(p["id"] != 99 for p in picked), "候选集之外的题被混进来了"
    assert not gaps, "池子有题却报缺口，说明取数逻辑有问题"


def test_quiz_difficulty_inverse():
    """判据二：题目难度与掌握度反相关——越薄弱越给基础题。"""
    targets = rank_targets(
        error_kp_ids=["kp_weak", "kp_mid", "kp_strong"],
        error_freq={},
        p_known_map={"kp_weak": 0.20, "kp_mid": 0.55, "kp_strong": 0.80},
    )
    assert difficulty_for(0.20) == "easy"      # p<0.40 → easy（先补地基）
    assert difficulty_for(0.55) == "medium"    # 0.40~0.70 → medium
    assert difficulty_for(0.80) == "hard"      # >0.70 → hard

    candidates = {
        "kp_weak":   [_cand(1, "kp_weak", "easy"), _cand(2, "kp_weak", "medium")],
        "kp_mid":    [_cand(3, "kp_mid", "medium"), _cand(4, "kp_mid", "easy")],
        "kp_strong": [_cand(5, "kp_strong", "hard"), _cand(6, "kp_strong", "medium")],
    }
    picked, _ = select_quiz_questions(targets, candidates, total=8)
    first_diff: dict[str, str] = {}
    for p in picked:                        # 记录每个知识点"第一道"被选中的题
        first_diff.setdefault(p["kp_id"], p["difficulty"])
    # 每个知识点选出的第一题必须是目标档（目标档优先、他档只是兜底）
    assert first_diff["kp_weak"] == "easy"
    assert first_diff["kp_mid"] == "medium"
    assert first_diff["kp_strong"] == "hard"


def test_quiz_quota_cap_and_gap():
    """判据三（PRD 5.4）：单知识点 ≤2 题；总量 8 封顶；缺题宁可记缺口不乱补。"""
    targets = rank_targets(
        error_kp_ids=["kp_a", "kp_b", "kp_empty"],
        error_freq={},
        p_known_map={"kp_a": 0.10, "kp_b": 0.10, "kp_empty": 0.10},
    )
    candidates = {
        # kp_a 塞 5 道题，配额必须截断到 2
        "kp_a": [_cand(i, "kp_a", "easy") for i in range(1, 6)],
        "kp_b": [_cand(11, "kp_b", "easy"), _cand(12, "kp_b", "easy")],
        "kp_empty": [],                        # 无题 → 记缺口，绝不从 kp_a 借题乱补
    }
    picked, gaps = select_quiz_questions(targets, candidates, total=8)

    from collections import Counter
    per_kp = Counter(p["kp_id"] for p in picked)
    assert per_kp["kp_a"] == 2, f"单知识点配额应为 2，实际 {per_kp['kp_a']}"
    assert len(picked) == 4
    assert any("kp_empty" in g for g in gaps), "无题知识点应出现在缺口清单里"


def test_rank_targets_order():
    """排序规则：p_known 升序优先；同掌握度按错题频次降序（又弱又常错的优先）。"""
    targets = rank_targets(
        error_kp_ids=["kp_x", "kp_y", "kp_z"],
        error_freq={"kp_y": 3},                 # kp_y 频次高但和 kp_x 同样弱
        p_known_map={"kp_x": 0.30, "kp_y": 0.30, "kp_z": 0.60},
    )
    ids = [tp["kp_id"] for tp in targets]
    assert ids.index("kp_y") < ids.index("kp_x"), "同掌握度时高错频应排前面"
    assert ids[-1] == "kp_z", "掌握度最高的应排最后"
    assert targets[0]["reason"] == "近30天连错3次"


# =====================================================================
# 题型配额（2026-10-26 补题库后新增）
# =====================================================================
def test_quiz_subjective_cap_skips_not_stops():
    """★ 主观题配额：超了就跳过这道、继续抽客观题，而不是整个知识点停掉。

    踩坑背景：给 63 个知识点各补 2~3 道解答题后，题库主观题从 22 涨到近 200。
    如果按顺序无脑取前 N，一个知识点 5 道题里 3 道主观，8 题卷子能抽出 4~5 道——
    而主观题要等老师批改（is_correct=NULL），学生做完看不到分，
    "即时反馈"的诊断测验就废了。
    """
    targets = [{"kp_id": "kp_a", "p_known": 0.20, "reason": "薄弱"}]   # p<0.4 → easy
    candidates = {"kp_a": [
        _cand(1, "kp_a", "easy", "subjective"),     # 主观题排在最前（模拟真实顺序）
        _cand(2, "kp_a", "easy", "subjective"),
        _cand(3, "kp_a", "easy", "fill"),
    ]}

    picked, gaps = select_quiz_questions(
        targets, candidates, total=8, per_kp_cap=3, subjective_cap=1,
    )

    subj = [p for p in picked if p.get("question_type") == "subjective"]
    assert len(subj) == 1, f"主观题应被限到 1 道，实际 {len(subj)} 道"
    assert 3 in [p["id"] for p in picked], "主观题超配额后跳过了，但客观题也必须被抽到"
    assert not gaps, "有题可选却报缺口，说明跳过逻辑写成了中断"


def test_quiz_subjective_cap_zero_gives_pure_objective_paper():
    """subjective_cap=0 → 纯客观卷（老师要快速判分的场景）。"""
    targets = [{"kp_id": "kp_a", "p_known": 0.20, "reason": "薄弱"}]
    candidates = {"kp_a": [
        _cand(1, "kp_a", "easy", "subjective"),
        _cand(2, "kp_a", "easy", "fill"),
    ]}

    picked, _ = select_quiz_questions(
        targets, candidates, total=8, per_kp_cap=2, subjective_cap=0,
    )

    assert all(p.get("question_type") != "subjective" for p in picked)
    assert [p["id"] for p in picked] == [2]


def test_quiz_objective_only_pool_unaffected_by_cap():
    """池子里全是客观题时，配额这道闸完全不影响结果（老行为不变）。"""
    targets = [{"kp_id": "kp_a", "p_known": 0.20, "reason": "薄弱"}]
    candidates = {"kp_a": [
        _cand(1, "kp_a", "easy"), _cand(2, "kp_a", "easy"), _cand(3, "kp_a", "easy"),
    ]}

    picked, _ = select_quiz_questions(targets, candidates, total=8, per_kp_cap=2)

    assert [p["id"] for p in picked] == [1, 2]
