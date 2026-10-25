"""LLM 输出的格式清洗：把 LaTeX 记号翻译成初中生看得懂的普通字符。

为什么要有这一层：
    数学类模型（DeepSeek / Qwen 等）写解析时习惯性吐 LaTeX——
    定界符 \\( \\) \\[ \\]、\\pm、\\frac、\\underline{\\quad} 这些。
    学生端是纯文本气泡，不做翻译，学生看到的就是
    "x_1=\\underline{\\quad},\\quad x_2=\\underline{\\quad}" 这种像代码的东西。
    提示词只能降低概率（模型不保证听），清洗才是确定性兜底。

被谁用：
    app/graphs/student/nodes.py —— 引导提示 / 完整讲解 / RAG 讲解
    app/graphs/teacher/grading_graph.py —— AI 批改评语
"""

import re

# ASCII 数字 → Unicode 下标（显示用：x1 → x₁）
_SUBSCRIPT_CHARS = "₀₁₂₃₄₅₆₇₈₉"
_SUBSCRIPT_REVERSE = {str(i): ch for i, ch in enumerate(_SUBSCRIPT_CHARS)}

# 单反斜杠命令 → Unicode 字符（初中生看得懂的写法）
# 值是空串表示"这个记号直接删掉"（如 \left \right 只起配对作用）
_MACRO_MAP = {
    r"\pm": "±", r"\mp": "∓",
    r"\times": "×", r"\div": "÷", r"\cdot": "·",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\neq": "≠", r"\approx": "≈", r"\equiv": "≡",
    r"\infty": "∞", r"\pi": "π", r"\alpha": "α", r"\beta": "β",
    r"\gamma": "γ", r"\theta": "θ", r"\lambda": "λ", r"\mu": "μ",
    r"\triangle": "△", r"\angle": "∠", r"\perp": "⊥", r"\parallel": "∥",
    r"\circ": "°", r"\degree": "°", r"\sim": "∽",
    r"\rightarrow": "→", r"\to": "→", r"\Rightarrow": "⇒",
    r"\leftarrow": "←", r"\in": "∈", r"\notin": "∉", r"\subset": "⊂",
    r"\ldots": "…", r"\cdots": "⋯", r"\dots": "…",
    r"\quad": " ", r"\qquad": "  ", r"\ ": " ",
    r"\left": "", r"\right": "", r"\big": "", r"\Big": "",
    r"\displaystyle": "", r"\limits": "", r"\nolimits": "",
    r"\{": "{", r"\}": "}", r"\%": "%", r"\&": "&", r"\#": "#",
    r"\$": "$", r"\_": "_",
}


# 输出格式规范：三端所有"给用户看的长文本"提示词都拼这一段
# 为什么要写死：模型默认会吐 Markdown + LaTeX 混排（**加粗**、\[公式\]、\underline{\quad}），
# 在纯文本气泡里看起来像乱码。提示词管"想要什么"，clean_llm_text 管"兜住不听话"。
OUTPUT_FORMAT_RULES = (
    "【输出格式要求（必须严格遵守）】\n"
    "1. 只输出正文，不要任何解释性开场白或结束语；\n"
    "2. 不要用 LaTeX：禁止出现 $...$、\\(...\\)、\\[...\\]、\\frac、\\pm、\\sqrt、\\underline 等任何公式记号；\n"
    "3. 数学式直接用普通字符写：x^2、x-3=±2、√5、x_1、x_2（不要写成 \\underline{\\quad} 这种填空线）；\n"
    "4. 需要学生填空的空位用下划线 ______ 表示；\n"
    "5. 用 Markdown 的 **加粗** 标小标题（如 **开平方**），不要用 # 标题；\n"
    "6. 步骤用「1. 2. 3.」编号，每步单独一行，每行不超过 40 字；\n"
    "7. 不要出现空行堆叠、表格、代码块或图片链接。\n"
)


def clean_llm_text(text: str | None) -> str:
    """清洗 LLM 输出里的 LaTeX 记号，转成可直接展示的纯文本。

    作用：把数学定界符、公式命令翻译成普通字符与符号，消掉"像代码"的观感。

    得到的数据：text —— LLM 生成的原始字符串（允许 None / 空串）。

    要做的操作：
      1. 剥掉数学定界符（$$ $$ / \\[ \\] / \\( \\) / $ $），只保留里面的内容；
      2. 处理带花括号的命令（\\frac、\\sqrt、\\underline、\\text 等）；
      3. 把常见单反斜杠命令换成 Unicode 字符，未知命令去掉反斜杠只留字母；
      4. 收口空白：连续空格压成一个、去掉行尾空白、3 个以上空行压成 1 个。

    返回：清洗后的字符串；入参为空时返回空串。

    为什么保留 ** 加粗标记：
        前端有轻量 Markdown 渲染，**xxx** 会被渲染成加粗小标题，
        这里若一并删掉，讲解就失去了层次感。格式归前端，公式归这里。
    """
    if not text:
        return ""

    s = str(text).replace("\r\n", "\n").replace("\r", "\n")

    # ---- 1. 剥数学定界符，只留内容 ----
    # 行间公式（可能跨行）：整块换成 strip 后的内容，
    # 这样 "\[\n  公式\n\]" 不会在正文里留下两个空行
    s = re.sub(r"\\\[(.+?)\\\]", lambda m: m.group(1).strip(), s, flags=re.S)
    s = re.sub(r"\\\((.+?)\\\)", lambda m: m.group(1).strip(), s, flags=re.S)
    s = re.sub(r"\$\$(.+?)\$\$", lambda m: m.group(1).strip(), s, flags=re.S)

    # 行内公式（不跨行）
    s = re.sub(r"\$([^$\n]+)\$", lambda m: m.group(1).strip(), s)
    
    # 落单的定界符（如只有 \] 没有 \[）
    s = re.sub(r"\\[\[\]()]", "", s)

    # ---- 2. LaTeX 的强制换行 ----
    s = s.replace("\\\\", "\n")

    # ---- 3. 带花括号的命令 ----
    # \frac{a}{b} → (a)/(b)
    s = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", s)
    # \sqrt{a} → √a；负数根号补括号（√-3 读着别扭，改成 √(-3)）
    s = re.sub(
        r"\\sqrt\s*\{([^{}]*)\}",
        lambda m: "√(" + m.group(1) + ")" if m.group(1).startswith("-") else "√" + m.group(1),
        s,
    )
    # \underline{\quad} → ______（填空线）；\underline{abc} → abc
    s = re.sub(r"\\underline\s*\{\s*\\quad\s*\}", "______", s)
    s = re.sub(r"\\underline\s*\{([^{}]*)\}", r"\1", s)
    # \text{...} \mathrm{...} \mathbf{...} → 内容本身
    s = re.sub(r"\\(?:text|mathrm|mathbf|mathit|operatorname|textbf|textit)\s*\{([^{}]*)\}", r"\1", s)

    # ---- 4. 单反斜杠命令 → Unicode；未知命令去掉反斜杠 ----
    s = re.sub(
        r"\\[a-zA-Z]+",
        lambda m: _MACRO_MAP.get(m.group(0), m.group(0)[1:]),
        s,
    )

    # ---- 5. 排版收口 ----
    # 连续空格压成一个（\quad 之类会造出 "，  x" 这种双空格）
    s = re.sub(r"[ \t]{2,}", " ", s)
    # 行尾空白
    s = re.sub(r"[ \t]+$", "", s, flags=re.M)
    # 3 个以上连续空行压成 1 个空行
    s = re.sub(r"\n{3,}", "\n\n", s)

    # ---- 6. 下标/上标美化（展示用，不影响语义）----
    # 起因（2026-10-25 老大截图）：讲解里 "x_1=______, x_2=______" 显示成
    # 带下划线的 x_1，学生看着像代码不像数学。改成真正的下标字符 x₁ x₂。
    s = re.sub(r"([A-Za-z])_(\d+)", _to_subscript, s)

    return s.strip()


def _to_subscript(m) -> str:
    """把 x_12 这种写法转成 x₁₂（真正的 Unicode 下标）。

    得到的数据：正则匹配到的 "字母_数字"（m.group(1)=字母，m.group(2)=数字串）
    要做的操作：逐位查表把 ASCII 数字换成 Unicode 下标字符
    返回：转好的字符串，如 "x₁₂"
    """
    digits = "".join(_SUBSCRIPT_REVERSE.get(d, d) for d in m.group(2))
    return m.group(1) + digits
