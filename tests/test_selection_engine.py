"""M4 交付自检：组卷引擎 R-08 配额抽题规则（纯函数，不碰 DB/LLM）。

 覆盖：配额兑现 / 单点跨项截断 / abnormal 沉底 / 题型硬约束 / 难度放宽 / 缺口不乱补 / 可复现
 S-10 的 select_quiz_questions 在 M2 的 test_quiz_generation.py 已覆盖，这里不重复。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.selection_engine import select_questions_by_quota


def make_q(qid, diff, qtype="fill", flag="normal"):
    """造一道候选题，形态对齐 fetch_candidates_by_kp 的一行。

    得到的数据：qid=12；diff="easy"；qtype="fill"；flag="normal"
    要做的操作：拼成 {"id", "kp_id", "difficulty", "question_type", "quality_flag"}
    返回：{"id": 12, "kp_id": "kp_test", "difficulty": "easy",
          "question_type": "fill", "quality_flag": "normal"}
    为什么：测引擎不需要真库，用最小 dict 就能覆盖全部规则分支
    """
    return {"id": qid, "kp_id": "kp_test", "difficulty": diff,
            "question_type": qtype, "quality_flag": flag}


def test_quota_executes_exact_count():
    """配额要 2 道就出 2 道（池子充足且不越单点红线）。"""
    pool = {"kp_test": [make_q(i, "easy") for i in range(1, 7)]}
    quota = [{"kp_id": "kp_test", "difficulty": "easy", "question_type": "fill", "question_count": 2}]
    picked, gaps = select_questions_by_quota(quota, pool)
    assert len(picked) == 2
    assert gaps == [] or all("放宽" in g for g in gaps)   # 无真缺口


def test_per_kp_cap_across_items():
    """单点 ≤2 是跨配额项累计口径：同 kp 两行配额也不许超。"""
    quota = [
        {"kp_id": "kp_a", "difficulty": "easy", "question_type": "fill", "question_count": 3},
        {"kp_id": "kp_a", "difficulty": "hard", "question_type": "fill", "question_count": 3},
    ]
    pool = {"kp_a": [make_q(i, "easy") for i in range(1, 9)]}
    picked, gaps = select_questions_by_quota(quota, pool)
    assert len(picked) == 2                      # 第一行吃到上限，第二行被丢弃
    assert any("上限" in g for g in gaps)


def test_abnormal_sinks_below_normal():
    """abnormal 沉底：normal 够数时 abnormal 一道都进不来；不够时仍能兜底。"""
    pool = {"kp_a": [make_q(1, "easy"), make_q(2, "easy"),
                     make_q(3, "easy", flag="abnormal")]}
    quota = [{"kp_id": "kp_a", "difficulty": "easy", "question_type": "fill", "question_count": 2}]
    picked, _ = select_questions_by_quota(quota, pool)
    assert [q["id"] for q in picked] == [1, 2]   # 3 号 abnormal 没被选

    pool2 = {"kp_a": [make_q(1, "easy"), make_q(3, "easy", flag="abnormal")]}
    picked2, _ = select_questions_by_quota(quota, pool2)
    assert len(picked2) == 2 and {q["id"] for q in picked2} == {1, 3}


def test_type_is_hard_constraint():
    """题型是硬约束：没题宁可记缺口，不拿别的题型凑数。"""
    pool = {"kp_a": [make_q(i, "easy", qtype="choice") for i in range(1, 6)]}
    quota = [{"kp_id": "kp_a", "difficulty": "easy", "question_type": "fill", "question_count": 2}]
    picked, gaps = select_questions_by_quota(quota, pool)
    assert picked == [] and any("缺 2 题" in g for g in gaps)


def test_difficulty_relaxes_to_adjacent():
    """目标难度没题时放宽到相邻档，且放宽记录写进缺口清单。"""
    pool = {"kp_a": [make_q(i, "medium") for i in range(1, 4)]}
    quota = [{"kp_id": "kp_a", "difficulty": "easy", "question_type": "fill", "question_count": 2}]
    picked, gaps = select_questions_by_quota(quota, pool)
    assert len(picked) == 2 and all(q["difficulty"] == "medium" for q in picked)
    assert any("放宽" in g for g in gaps)


def test_gap_not_filled_randomly():
    """宁可少题不乱补：池中仅 1 题时只出 1 题，缺口与超红线截断都如实记账。"""
    pool = {"kp_a": [make_q(1, "easy")]}
    quota = [{"kp_id": "kp_a", "difficulty": "easy", "question_type": "fill", "question_count": 4}]
    picked, gaps = select_questions_by_quota(quota, pool)
    assert len(picked) == 1
    assert any("缺 1 题" in g for g in gaps)               # 池子不足
    assert any("上限" in g for g in gaps)                  # 超 4>2 被截断


def test_exclude_ids_respected():
    """近 7 天去重：exclude_ids 里的题不进卷。"""
    pool = {"kp_a": [make_q(i, "easy") for i in range(1, 6)]}
    quota = [{"kp_id": "kp_a", "difficulty": "easy", "question_type": "fill", "question_count": 2}]
    picked, _ = select_questions_by_quota(quota, pool, exclude_ids={1, 2})
    assert {q["id"] for q in picked} == {3, 4}   # 剩余池按 id 升序取


def test_deterministic_same_input_same_output():
    """可复现铁律：同一输入跑两遍，结果必须完全一致。"""
    pool = {"kp_a": [make_q(i, "easy") for i in range(1, 9)]}
    quota = [{"kp_id": "kp_a", "difficulty": "easy", "question_type": "fill", "question_count": 3}]
    r1, g1 = select_questions_by_quota(quota, pool)
    r2, g2 = select_questions_by_quota(quota, pool)
    assert r1 == r2 and g1 == g2
