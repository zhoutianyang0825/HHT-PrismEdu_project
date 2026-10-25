"""LLM 输出格式清洗的回归测试（app/core/textfmt.py）。

钉住的坑（2026-10-25 老大截图反馈）：
    数学模型写解析时习惯性吐 LaTeX，前端是纯文本气泡，
    学生看到的是这种东西：
        x_1=\\underline{\\quad},\\quad x_2=\\underline{\\quad}
        x-3=\\pm 2
        \\[ (x-3)^2=4 \\]
    —— 最后那行"像代码"，实际是 LaTeX 的填空线记号。

这里断言：清洗器能把这些记号翻译成初中生看得懂的普通字符，
同时**不能**动 Markdown 的 ** 加粗（前端要靠它做层级）。
"""

from app.core.textfmt import OUTPUT_FORMAT_RULES, clean_llm_text


def test_display_math_delimiters_removed():
    """\\[ ... \\] 和 \\( ... \\) 只留内容，定界符不出现。"""
    s = clean_llm_text(r"配方得 \[ (x-3)^2=4 \]，所以 \(x-3=\pm 2\)。")
    assert r"\[" not in s and r"\]" not in s
    assert r"\(" not in s and r"\)" not in s
    assert "(x-3)^2=4" in s
    assert "x-3=± 2" in s


def test_inline_dollar_math_removed():
    """行内 $...$ 定界符剥掉，内容保留（下标会转成 Unicode 下标，见下）。"""
    s = clean_llm_text("答案是 $x_1=5$，另一个是 $x_2=1$。")
    assert "$" not in s
    # x_1 → x₁（下标美化，2026-10-25 加的）
    assert "x₁=5" in s and "x₂=1" in s


def test_underline_quad_becomes_blank_line():
    """\\underline{\\quad}（LaTeX 填空线）→ ______，这是截图里最像代码的那一段。"""
    s = clean_llm_text(r"写成 x_1=\underline{\quad},\quad x_2=\underline{\quad}")
    assert r"\underline" not in s
    assert r"\quad" not in s
    assert "______" in s
    assert "x₁=" in s and "x₂=" in s


def test_subscript_made_pretty():
    """★ x_1 → x₁（2026-10-25 老大截图：x_1 显示得像代码不像数学）。

    只影响显示，不影响语义——判分侧另有归一化（见 test_grading_fill.py）。
    """
    assert clean_llm_text("x_1=2") == "x₁=2"
    assert clean_llm_text("x_12 和 a_3") == "x₁₂ 和 a₃"
    # 没有下划线的 x1 保持原样（避免和"x1=2"这种题库标答写法混淆）
    assert clean_llm_text("x1=2") == "x1=2"
    # 上标不动（x^2 保持原样，LaTeX 上标命令是另一套）
    assert clean_llm_text("x^2-6x+5=0") == "x^2-6x+5=0"


def test_frac_and_sqrt_translated():
    """\\frac{a}{b} → (a)/(b)；\\sqrt{a} → √a。"""
    s = clean_llm_text(r"原式是 \frac{1}{2}，根是 \sqrt{9}。")
    assert r"\frac" not in s and r"\sqrt" not in s
    assert "(1)/(2)" in s
    assert "√9" in s


def test_common_macros_become_unicode():
    """常见数学符号命令 → Unicode 字符。"""
    s = clean_llm_text(r"\pm \times \div \leq \geq \neq \pi")
    assert s.split() == ["±", "×", "÷", "≤", "≥", "≠", "π"]


def test_unknown_macro_keeps_letters():
    """未知命令：去掉反斜杠留字母，不要把内容整段吃掉。"""
    s = clean_llm_text(r"这是 \newcommand 的用法")
    assert r"\newcommand" not in s
    assert "newcommand" in s


def test_markdown_bold_is_preserved():
    """★ 关键不变量：**加粗** 必须保留——前端靠它渲染小标题层级。"""
    s = clean_llm_text("**开平方：** x-3=±2")
    assert "**开平方：**" in s


def test_blank_lines_collapsed():
    """3 个以上连续空行压成 1 个，避免排版散架。"""
    s = clean_llm_text("第一段\n\n\n\n\n第二段")
    assert s.count("\n\n") == 1


def test_empty_input_safe():
    """None / 空串都不炸。"""
    assert clean_llm_text(None) == ""
    assert clean_llm_text("") == ""


def test_output_format_rules_mention_latex_ban():
    """格式规范里必须明确禁掉 LaTeX，否则提示词这层形同虚设。"""
    assert "LaTeX" in OUTPUT_FORMAT_RULES
    assert "underline" in OUTPUT_FORMAT_RULES
    assert "加粗" in OUTPUT_FORMAT_RULES
