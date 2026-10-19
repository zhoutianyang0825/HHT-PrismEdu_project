"""M4 交付自检：R-04 统计指标的手算校准 + quality_flag 写回。

 指标数字全部可手算复核（PRD 验收："指标计算正确（用样本手算验证）"的落点）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.analysis import compute_item_metrics, pick_abnormal, write_back_quality_flags


def make_df(rows):
    """把 (student_id, q_id, score, full_score) 元组列表转成长表 DataFrame。

    得到的数据：[(1, 1, 9, 10), (1, 2, 10, 10), ...]
    要做的操作：pd.DataFrame 指定列名——不指定的话列名会是 0/1/2/3，groupby 就用不了
    返回：DataFrame(columns=["student_id", "q_id", "score", "full_score"])
    为什么：单测只需最小长表，不必碰库
    """
    import pandas as pd

    return pd.DataFrame(
        rows, columns=["student_id", "q_id", "score", "full_score"]
    )


def test_p_d_hand_calibrated():
    """手算校准 P/D：2 题 × 4 学生，结果可笔算。

    卷面（满分 10/题）：s1(9,10)=19 / s2(7,8)=15 / s3(5,6)=11 / s4(3,4)=7
    27% 分组：int(4×0.27)=1 → 高分组={s1}，低分组={s4}
    Q1: P=26/4/10=0.6  D=(9-3)/10=0.6；Q2: P=28/4/10=0.7  D=(10-4)/10=0.6
    """
    df = make_df([
        (1, 1, 9, 10), (1, 2, 10, 10),
        (2, 1, 7, 10), (2, 2, 8, 10),
        (3, 1, 5, 10), (3, 2, 6, 10),
        (4, 1, 3, 10), (4, 2, 4, 10),
    ])
    metrics = {r["q_id"]: r for r in compute_item_metrics(df)}
    assert metrics[1]["P"] == 0.6 and metrics[1]["D"] == 0.6
    assert metrics[2]["P"] == 0.7 and metrics[2]["D"] == 0.6
    assert not metrics[1]["is_abnormal"] and not metrics[2]["is_abnormal"]


def test_abnormal_thresholds():
    """三条阈值各自触发：P<0.3 过难 / P>0.9 过易 / D<0.2 区分度不足。"""
    df = make_df([
        (1, 1, 1, 10), (1, 2, 9.5, 10), (1, 3, 5, 10),
        (2, 1, 2, 10), (2, 2, 9, 10),   (2, 3, 5, 10),
        (3, 1, 1, 10), (3, 2, 10, 10),  (3, 3, 5, 10),
        (4, 1, 0, 10), (4, 2, 9.5, 10), (4, 3, 5, 10),
    ])
    metrics = {r["q_id"]: r for r in compute_item_metrics(df)}
    assert "过难" in metrics[1]["issues"]        # P=0.1
    assert "过易" in metrics[2]["issues"]        # P=0.95
    assert "区分度不足" in metrics[3]["issues"]   # 高分组=低分组=5 分 → D=0
    abnormal = pick_abnormal(list(metrics.values()))
    assert {r["q_id"] for r in abnormal} == {1, 2, 3}


def test_small_sample_no_crash():
    """3 人小样本不炸：int(3×0.27)=0 → max(1, ...) 保证每组至少 1 人。"""
    df = make_df([
        (1, 1, 8, 10), (2, 1, 6, 10), (3, 1, 4, 10),
    ])
    metrics = compute_item_metrics(df)
    assert len(metrics) == 1
    m = metrics[0]
    assert m["P"] == 0.6 and m["D"] == 0.4    # 高={s1} 低={s3}：(8-4)/10
    assert m["is_abnormal"] is False


def test_write_back_quality_flags(session):
    """写回：异常题 normal→abnormal；disabled 不被覆盖；返回实际改动行数。

    得到的数据：session 是 conftest 的 sqlite 夹具；库里造 7/13/19 三题（19 已 disabled）
    要做的操作：metrics 里 7、13 异常 → 调 write_back_quality_flags
    返回：changed=2；7/13 变 abnormal，19 仍是 disabled
    为什么：这条回流是 R-08 降权的数据源，写错了下次出卷会继续抽到烂题
    """
    from app.models import Question

    for qid in (7, 13, 19):
        session.add(Question(
            id=qid, subject="math", chapter_id="ch_quadratic", kp_id="kp_crossmult",
            question_type="fill", difficulty="easy",
            content=f"题{qid}", answer="x=1",
            quality_flag="disabled" if qid == 19 else "normal",   # 19 号人工判过死刑
        ))
    session.commit()

    metrics = [
        {"q_id": 7, "P": 0.28, "D": 0.41, "score_rate": 0.28, "is_abnormal": True, "issues": ["过难"]},
        {"q_id": 13, "P": 0.91, "D": 0.55, "score_rate": 0.91, "is_abnormal": True, "issues": ["过易"]},
        {"q_id": 19, "P": 0.5, "D": 0.3, "score_rate": 0.5, "is_abnormal": False, "issues": []},
    ]
    changed = write_back_quality_flags(metrics, session=session)
    assert changed == 2                              # 7、13 两题改；19 不是 abnormal 不动

    flags = {q.id: q.quality_flag for q in session.query(Question).all()}
    assert flags[7] == "abnormal" and flags[13] == "abnormal"
    assert flags[19] == "disabled"                   # 人工标记永不被覆盖
