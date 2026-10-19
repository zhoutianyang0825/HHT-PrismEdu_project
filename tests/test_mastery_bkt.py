"""M3-T4 · BKT 四条验证序列单测（开发计划 M3 完成判据：四条序列一个不许歪）。

 得到的数据：mastery.py 的纯函数（零 DB / 零 LLM，不碰容器）
 要做的操作：跑四个场景，只断言"方向与相对关系"，不断言精确数值
   ——精确值会随参数微调漂移，方向错了才是真 bug（架构文档："只验证方向，不凭感觉调参"）

 跑法：uv run pytest tests/test_mastery_bkt.py -v
"""
import math

from app.services.mastery import (
    DEFAULT_PARAMS,
    SLIP_BY_ERROR,
    apply_decay,
    update_mastery,
)


# =====================================================================
# 序列 1：连答对 ×3 → 掌握度单调上升
# =====================================================================
def test_consecutive_correct_rises():
    """序列 1：连答对三次，掌握度应单调上升。

    得到的数据：起始 p_known=0.20，每次距上次作答 1 天
    要做的操作：连调 3 次 update_mastery(is_correct=True)
    返回：无返回，断言历史序列逐步递增且末值 > 0.6
    为什么：只断方向不断精确值——参数微调会让数值漂移，方向错了才是真 bug
    """
    p = 0.20
    history = [p]
    for _ in range(3):
        p = update_mastery(p, is_correct=True, error_type=None, days_since_last=1.0)
        history.append(p)

    # 方向断言：每一步都在涨（单调递增）
    assert history[1] > history[0], f"第 1 次答对没涨：{history}"
    assert history[2] > history[1], f"第 2 次答对没涨：{history}"
    assert history[3] > history[2], f"第 3 次答对没涨：{history}"
    # 幅度断言：三轮后过 0.6（架构文档验证表：三次后到 0.6 以上）
    assert history[3] > 0.6, f"三轮连对后仍低于 0.6：{history}"
    print(f"连对序列：{[round(v, 3) for v in history]}")


# =====================================================================
# 序列 2：连答错（概念不清）×3 → 明显下降
# =====================================================================
def test_consecutive_wrong_falls():
    """序列 2：连答错三次（概念不清），掌握度应明显下降。

    得到的数据：起始 p_known=0.80，错因固定"概念不清"，每次距上次作答 1 天
    要做的操作：连调 3 次 update_mastery(is_correct=False)
    返回：无返回，断言末值 < 0.5 且下降量 > 0.25
    为什么：验证"答错 + 真不会"这条最重的惩罚路径没有反向
    """
    p = 0.80
    history = [p]
    for _ in range(3):
        p = update_mastery(p, is_correct=False, error_type="概念不清", days_since_last=1.0)
        history.append(p)

    # 方向断言：整体大幅下降（0.80 → 远低于 0.5）
    assert history[-1] < 0.50, f"连错三次还高于 0.5，方向不对：{history}"
    assert history[-1] < history[0], f"连错反而没降：{history}"
    # 幅度断言：下降量要显著（掉至少 0.25）
    assert history[0] - history[-1] > 0.25, f"下降幅度不足：{history}"
    print(f"连错序列：{[round(v, 3) for v in history]}")


# =====================================================================
# 序列 3：同分错题——粗心惩罚显著轻于概念不清（错因联动的核心验证点）
# =====================================================================
def test_careless_penalty_lighter():
    """序列 3：同分错题，粗心的惩罚要显著轻于概念不清。

    得到的数据：同一初值 p_known=0.70，两次各错一题（错因分别"粗心"/"概念不清"）
    要做的操作：各调一次 update_mastery(is_correct=False)
    返回：无返回，断言 粗心结果 > 概念不清结果 且差距 > 0.15
    为什么：这是错因联动的核心验证点——slip 0.45 vs 0.05 的设计必须在结果上拉开，
            否则"错因只替换 slip"等于白做
    """
    p_careless = update_mastery(0.70, is_correct=False, error_type="粗心", days_since_last=1.0)
    p_concept = update_mastery(0.70, is_correct=False, error_type="概念不清", days_since_last=1.0)

    # 方向断言：粗心后的掌握度显著高于概念不清后的
    assert p_careless > p_concept, f"粗心掉得比概念不清还多，slip 映射失效：{p_careless} vs {p_concept}"
    # 幅度断言：差距要显著（slip 0.45 vs 0.05 的设计必须在结果上拉开）
    assert p_careless - p_concept > 0.15, f"两种错因惩罚拉不开差距：{p_careless} vs {p_concept}"
    # 边界断言：粗心答错不该把"会"重判成"不会"（仍应保持在 0.5 以上）
    assert p_careless > 0.5, f"粗心一次就被判为不会，惩罚过重：{p_careless}"
    print(f"粗心 {p_careless:.3f} > 概念不清 {p_concept:.3f}（同分错题，粗心掉得少）✓")


# =====================================================================
# 序列 4：30 天无作答 → 遗忘衰减跌破 0.1 预警线
# =====================================================================
def test_decay_after_30_days():
    """序列 4：30 天无作答，掌握度应跌破 0.1 预警线。

    得到的数据：起始 p_known=0.80，days_since_last=30
    要做的操作：只调 apply_decay（不调 update_mastery）
    返回：无返回，断言结果 < 0.1 且与手算值 0.80×2^(-30/7) 一致
    为什么：无作答 = 没有新证据，只该走遗忘衰减；调 update_mastery 会把"没练"算成
            "练过一次"，语义就错了
    """
    # 注意场景语义：无作答 = 没有新证据，只走第 1 步遗忘衰减，不能调 update_mastery
    #   （update_mastery 是"一次作答后"的更新，无作答场景调它是语义错误）
    p = apply_decay(0.80, days_since_last=30.0)

    # 精确值手算校验：0.80 × e^(-ln2/7×30) = 0.80 × 2^(-30/7) ≈ 0.041
    expected = 0.80 * math.pow(2, -30 / 7)
    assert math.isclose(p, expected, rel_tol=1e-9), f"衰减公式不对：{p} vs {expected}"
    # 预警线断言：跌破 0.1（S-10 诊断测验的触发依据之一）
    assert p < 0.10, f"30 天后仍高于 0.1 预警线：{p}"
    print(f"30 天无作答：0.80 → {p:.3f}（跌破 0.1 预警线）✓")


# =====================================================================
# 附加：数值卫生 + slip 映射表完整性（防呆）
# =====================================================================
def test_output_always_in_range():
    """防呆：任意极端输入，输出必须落在 (0, 1) 开区间。

    得到的数据：p0 ∈ {0, 0.01, 0.5, 0.99, 1}、对错两种、四种错因（含不存在的）、三种天数
    要做的操作：组合遍历调用 update_mastery
    返回：无返回，断言每个输出都严格在 (0, 1) 内
    为什么：下游拿 p_known 画雷达图、做 <0.1 预警阈值，出界就是事故；也不能为 0（会当除数）
    """
    for p0 in (0.0, 0.01, 0.50, 0.99, 1.0):
        for correct in (True, False):
            for err in (None, "粗心", "概念不清", "不存在的错因"):
                for days in (0.0, 3.0, 365.0):
                    out = update_mastery(p0, correct, err, days)
                    assert 0.0 < out < 1.0, f"出界：p0={p0} correct={correct} err={err} days={days} -> {out}"


def test_slip_map_covers_error_enum():
    """防呆：slip 映射表必须覆盖 error_logs 的四种错因枚举。

    得到的数据：SLIP_BY_ERROR 与 DEFAULT_PARAMS 两个常量
    要做的操作：比对 key 集合 + 检查"粗心"与"概念不清"的倍数关系
    返回：无返回，断言通过即说明错因联动的前提成立
    为什么：新增错因忘了登记会走 .get 兜底、静默退回默认 slip，单测是唯一能提前发现的地方
    """
    assert set(SLIP_BY_ERROR.keys()) == {"概念不清", "方法不会", "粗心", "审题失误"}
    # 粗心 slip 必须显著大于概念不清 slip——这是错因联动设计的数值前提
    assert SLIP_BY_ERROR["粗心"] > SLIP_BY_ERROR["概念不清"] * 5
    # 四个 BKT 参数都在合法开区间
    for key, val in DEFAULT_PARAMS.items():
        assert 0.0 < val < 1.0, f"{key}={val} 出界"
