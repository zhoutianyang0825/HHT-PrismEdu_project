"""R-08 出卷的名字与 id 治理（2026-10-26）。

三个钉子：
  ① 配额表里的 chapter_id 必须能回捞到真实章节名（规划师会自造 ch_21_quad_eq）
  ② 缺口清单写知识点中文名，不写 kp_peifang
  ③ 月考不检查的指标要标 not_checked，不能被读成"数据缺失"
"""
import pytest

from app.crews.exam_crew import _fix_plan_ids, check_paper_hard_metrics
from app.schemas.exam import QuotaItem, QuotaPlan
from app.services import naming
from app.services.selection_engine import select_questions_by_quota


def _plan(*items) -> QuotaPlan:
    return QuotaPlan(exam_type="monthly", total_score=100, items=list(items))


def _item(kp: str, chapter: str, count: int = 1, diff: str = "easy",
          qtype: str = "choice") -> QuotaItem:
    return QuotaItem(kp_id=kp, chapter_id=chapter, question_count=count,
                     difficulty=diff, question_type=qtype, reason="测试")


# =====================================================================
# ① chapter_id 纠正 / 不存在的知识点丢弃
# =====================================================================
def test_fix_plan_ids_rewrites_fake_chapter_id(monkeypatch):
    """★ 规划师按"第21章"自己拼出 ch_21_quad_eq → 必须被改成库里的 ch_quadratic。"""
    monkeypatch.setattr(naming, "fetch_kp_chapter_map",
                        lambda ids: {"kp_peifang": "ch_quadratic"})
    monkeypatch.setattr(naming, "fetch_kp_names",
                        lambda ids: {"kp_peifang": "配方法"})

    fixed, notes, names = _fix_plan_ids(_plan(_item("kp_peifang", "ch_21_quad_eq")))

    assert fixed.items[0].chapter_id == "ch_quadratic"
    assert names["kp_peifang"] == "配方法"
    assert any("章节归属已按课程库纠正" in n for n in notes)


def test_fix_plan_ids_drops_unknown_kp(monkeypatch):
    """库里没有的知识点（kp_sqrt）整行丢弃并说明原因，不能只在缺口里刷"池中仅 0 题"。"""
    monkeypatch.setattr(naming, "fetch_kp_chapter_map",
                        lambda ids: {"kp_peifang": "ch_quadratic"})
    monkeypatch.setattr(naming, "fetch_kp_names",
                        lambda ids: {"kp_peifang": "配方法"})

    fixed, notes, _ = _fix_plan_ids(_plan(
        _item("kp_peifang", "ch_quadratic"),
        _item("kp_sqrt", "ch_21_quad_eq"),      # 库里没有
    ))

    assert [it.kp_id for it in fixed.items] == ["kp_peifang"]
    assert any("kp_sqrt" in n and "丢弃" in n for n in notes)


def test_fix_plan_ids_keeps_real_chapter_untouched(monkeypatch):
    """本来就对的 chapter_id 不要动，也不该多报"已纠正"。"""
    monkeypatch.setattr(naming, "fetch_kp_chapter_map",
                        lambda ids: {"kp_peifang": "ch_quadratic"})
    monkeypatch.setattr(naming, "fetch_kp_names", lambda ids: {"kp_peifang": "配方法"})

    fixed, notes, _ = _fix_plan_ids(_plan(_item("kp_peifang", "ch_quadratic")))

    assert fixed.items[0].chapter_id == "ch_quadratic"
    assert not any("已纠正" in n for n in notes)


def test_fix_plan_ids_unknown_chapter_when_kp_has_no_chapter(monkeypatch):
    """知识点本身没挂章节（chapter_id 为 NULL）→ 写 unknown，不能塞 None 进 schema。"""
    monkeypatch.setattr(naming, "fetch_kp_chapter_map", lambda ids: {"kp_x": None})
    monkeypatch.setattr(naming, "fetch_kp_names", lambda ids: {"kp_x": "某点"})

    fixed, _, _ = _fix_plan_ids(_plan(_item("kp_x", "ch_a")))

    assert fixed.items[0].chapter_id == "unknown"


# =====================================================================
# ② 缺口清单用中文名
# =====================================================================
def _pool(kp: str, n: int = 1) -> dict:
    return {kp: [
        {"id": 100 + i, "kp_id": kp, "difficulty": "easy",
         "question_type": "choice", "quality_flag": "normal",
         "chapter_id": "ch_quadratic"}
        for i in range(n)
    ]}


def test_gap_uses_kp_display_name():
    """★ 缺口清单是给教研组长看的："配方法: …" 而不是 "kp_peifang: …"。"""
    picked, gaps = select_questions_by_quota(
        quota_items=[{"kp_id": "kp_peifang", "difficulty": "easy",
                      "question_type": "choice", "question_count": 3}],
        candidates_by_kp=_pool("kp_peifang", 1),
        kp_label={"kp_peifang": "配方法"},
    )

    assert picked
    assert all(g.startswith("配方法: ") for g in gaps), gaps


def test_gap_falls_back_to_kp_id_without_label():
    """不传映射时保持旧行为（纯函数可单测、老调用方不受影响）。"""
    _, gaps = select_questions_by_quota(
        quota_items=[{"kp_id": "kp_peifang", "difficulty": "easy",
                      "question_type": "choice", "question_count": 3}],
        candidates_by_kp=_pool("kp_peifang", 1),
    )

    assert all(g.startswith("kp_peifang: ") for g in gaps), gaps


# =====================================================================
# ③ 审核指标：null 是"本次不检查"，不是"数据缺失"
# =====================================================================
def _picked(n_easy: int, n_medium: int, chapter: str = "ch_quadratic"):
    rows = []
    for i in range(n_easy):
        rows.append({"id": i, "kp_id": "kp_a", "difficulty": "easy",
                     "question_type": "choice", "chapter_id": chapter})
    for i in range(n_medium):
        rows.append({"id": 100 + i, "kp_id": "kp_b", "difficulty": "medium",
                     "question_type": "fill", "chapter_id": chapter})
    return rows


def test_metrics_marks_not_checked_for_monthly():
    """★ 月考不查题型/章节 → 必须给出 not_checked，否则组长会以为"数据丢了、要补齐"。

    这条只管字段语义，不关心审核过不过（难度分布由别的用例管）。
    """
    plan = _plan(*[_item("kp_a", "ch_quadratic")] * 2)
    _, metrics, _ = check_paper_hard_metrics(
        _picked(4, 2), plan, {"kp_a", "kp_b"}, total_chapters=9)

    assert metrics["type_dist"] is None
    assert metrics["chapter_coverage"] is None
    assert set(metrics["not_checked"]) == {"type_dist", "chapter_coverage"}


def test_metrics_chapter_coverage_computed_only_for_mock_exam():
    """摸底型才查章节覆盖，且按题上的真实 chapter_id 去重计数。"""
    plan = QuotaPlan(exam_type="final", total_score=100,
                     items=[_item("kp_a", "ch_quadratic")])
    picked = (
        _picked(6, 1, chapter="ch_quadratic")
        + [{"id": 300, "kp_id": "kp_c", "difficulty": "medium",
            "question_type": "subjective", "chapter_id": "ch_trig"}]
    )

    _, metrics, _ = check_paper_hard_metrics(picked, plan, set(), total_chapters=9)

    # 2 章 / 9 章 = 22.2%，两个 chapter 都真实存在
    assert metrics["chapter_coverage"] == pytest.approx(0.222, abs=0.001)
    assert metrics["not_checked"] == [] or "chapter_coverage" not in metrics["not_checked"]


def test_metrics_unknown_chapter_not_counted_as_covered():
    """unknown 章节不算已覆盖：兜底表给 unknown，不能靠它刷覆盖率。"""
    plan = QuotaPlan(exam_type="final", total_score=100,
                     items=[_item("kp_a", "unknown")])
    picked = _picked(7, 0, chapter="unknown")

    _, metrics, _ = check_paper_hard_metrics(picked, plan, set(), total_chapters=9)

    assert metrics["chapter_coverage"] == 0.0
