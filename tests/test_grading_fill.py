"""填空题判分的回归测试（下标写法 + 多值等价）。

钉住的坑（2026-10-25 老大实测截图）：
    题库标答存的是 "x1=2，x2=3"（逗号分隔、变量带下标编号），
    但讲解里写的是 "x_1=______"，学生照抄答 "x_1=2，x_2=3"，
    学生第一次答 "x=2或x=3"（"或"分隔、变量不带编号）——
    三种写法数学上完全等价，却全被判错。

    根因：判分前的 normalize() 只处理全角/空格/LaTeX 包裹，
          没处理"变量下标"和"多值分隔符"这两类写法差异。

    ★ 判分第一原则：宁可漏判（放过一个格式差异），不可错判
      （把格式差异当成知识错误）。但也不能放松成"看着像就算对"——
      所以多值比对有安全阀：两边组成部分数量必须相同、每部分必须
      能解析出纯数值，任一不满足就不生效。
"""

from app.services.grading import grade_fill, normalize


# =====================================================================
# 变量下标：x_1 / x₁ / x1 是同一个东西
# =====================================================================
def test_normalize_merges_subscript_forms():
    """三种下标写法归一化后必须完全相同。"""
    forms = ["x_1=2,x_2=3", "x₁=2,x₂=3", "x1=2,x2=3"]
    normalized = {normalize(f) for f in forms}
    assert len(normalized) == 1, normalized
    assert normalized.pop() == "x1=2,x2=3"


def test_underscore_subscript_accepted():
    """★ 老大截图的原场景：学生答 x_1=2，x_2=3，标答 x1=2，x2=3 → 判对。"""
    assert grade_fill("x_1=2，x_2=3", "x1=2，x2=3") is True


def test_unicode_subscript_accepted():
    """讲解里显示的是 x₁（Unicode 下标），学生照抄也要判对。"""
    assert grade_fill("x₁=2，x₂=3", "x1=2，x2=3") is True


def test_uppercase_underscore_accepted():
    """大小写差异也要归一化掉。"""
    assert grade_fill("X_1=2, X_2=3", "x1=2，x2=3") is True


def test_wrong_subscript_values_still_wrong():
    """★ 防误放水：下标归一化不能把错的数值也放过。"""
    assert grade_fill("x_1=5，x_2=9", "x1=2，x2=3") is False


# =====================================================================
# 顺序语义：根的顺序无关，但"两个不同的量"顺序有关
# =====================================================================
def test_root_order_irrelevant():
    """两根是同一组解，编号反了也算对（x1=3，x2=2 与 x1=2，x2=3 同解）。"""
    assert grade_fill("x_1=3，x_2=2", "x1=2，x2=3") is True


def test_vieta_order_matters():
    """★ 韦达题（id 42-45）：标答 "7；10" 是 x1+x2 和 x1·x2 两个**不同的量**，
    顺序有关——学生答 "10；7" 必须判错。

    这是多值比对的分水岭：靠"变量名是不是 x+编号"区分根题和量题。
    """
    assert grade_fill("10；7", "7；10") is False
    assert grade_fill("10,7", "7；10") is False
    assert grade_fill("7；10", "7；10") is True


def test_vieta_negative_values_order_matters():
    """负数版本同样按顺序判。"""
    assert grade_fill("14；-9", "-9；14") is False
    assert grade_fill("-9；14", "-9；14") is True


# =====================================================================
# 多值答案：分隔符和变量写法不同，但值集合相同
# =====================================================================
def test_or_separator_accepted():
    """★ 老大截图的第 2 次作答：学生答 x=2或x=3 → 判对。"""
    assert grade_fill("x=2或x=3", "x1=2，x2=3") is True


def test_value_order_irrelevant():
    """两个根的顺序无所谓（同一组解）。"""
    assert grade_fill("x=3或x=2", "x1=2，x2=3") is True
    assert grade_fill("x1=3，x2=2", "x1=2，x2=3") is True


def test_bare_values_accepted():
    """只写数值不写变量名也要认。"""
    assert grade_fill("2，3", "x1=2，x2=3") is True


def test_halfwidth_comma_accepted():
    """半角逗号 vs 全角逗号。"""
    assert grade_fill("x1=2,x2=3", "x1=2，x2=3") is True


def test_or_separator_with_single_value_question():
    """单值题用"或"分隔的两个值，不能被当成一个答案。"""
    assert grade_fill("7或-5", "7 或 -5") is True
    assert grade_fill("7", "7 或 -5") is False
    assert grade_fill("-5", "7 或 -5") is False


# =====================================================================
# 安全阀：多值比对不能把错的放过，也不能把单空题乱认
# =====================================================================
def test_different_values_still_wrong():
    """★ 值不同必须判错。"""
    assert grade_fill("x=2或x=4", "x1=2，x2=3") is False
    assert grade_fill("x=5或x=1", "x1=2，x2=3") is False


def test_partial_answer_still_wrong():
    """只答一个根不能算全对。"""
    assert grade_fill("x=2", "x1=2，x2=3") is False


def test_extra_value_still_wrong():
    """多写一个根不能算对。"""
    assert grade_fill("x=2或x=3或x=4", "x1=2，x2=3") is False


def test_non_numeric_answer_untouched():
    """非数值标答（"检验；排除增根"）不受多值规则影响。"""
    assert grade_fill("检验；排除增根", "检验；排除增根") is True
    assert grade_fill("随便写的", "检验；排除增根") is False


def test_non_numeric_symbol_answer_untouched():
    """带符号的标答（m≠2）不被下标规则误伤。"""
    assert grade_fill("m≠2", "m≠2") is True
    assert grade_fill("m≠3", "m≠2") is False


def test_fullwidth_semicolon_normalized():
    """★ 隐形坑：全角"；"必须转半角。

    不转的话，后面按 [;；] 切空那一步切不开（"10；7" 切不开），
    会漏到多值集合比对那层 → 韦达题（顺序有关）被误判成"顺序无关"而放过错答。
    """
    assert normalize("10；7") == "10;7"
    assert normalize("7；10") == "7;10"


def test_existing_expression_equivalence_still_works():
    """原有能力不能被新规则破坏：因式分解 vs 展开式仍判对。"""
    assert grade_fill("(x-2)(x-3)", "x^2-5x+6") is True
