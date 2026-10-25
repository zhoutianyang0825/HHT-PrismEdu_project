"""M3-T1 · 客观题判分服务接口：选择/填空 值相等 或者 表达式等价expr_equal（sympy库）

 本文件定位：纯函数，零 DB / 零 LLM 依赖 —— 教师端agent侧 grading_graph 的 grade_objective 节点调它
 三层结构：归一化 → 表达式等价 → 分题型判分
 依赖：sympy（pyproject 已显式声明 sympy>=1.13）
"""
import re
import signal
from contextlib import contextmanager


# =====================================================================
# 下标数字：Unicode 下标 ↔ ASCII 数字
# =====================================================================
# 场景实测：
#   讲解里写 "x_1=______"，标准答案存 "x1=2"，学生照抄 "x_1=2"，
#   归一化后 "x_1=2" ≠ "x1=2" → 明明答对却判错。
#   下标写法至少三种：x_1（LaTeX/纯文本）、x₁（Unicode 下标）、x1（纯 ASCII），
#   判分时必须视作同一个东西。
SUBSCRIPT_CHARS = "₀₁₂₃₄₅₆₇₈₉"
_SUPERSCRIPT_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹"

# 下标数字 → ASCII 数字
_SUBSCRIPT_MAP = {ch: str(i) for i, ch in enumerate(SUBSCRIPT_CHARS)}
# ASCII 数字 → 下标数字（显示用：x1 → x₁）
_SUBSCRIPT_REVERSE = {str(i): ch for i, ch in enumerate(SUBSCRIPT_CHARS)}
# ASCII 数字 → 上标数字（显示用：x2 → x²）
_SUPERSCRIPT_REVERSE = {str(i): ch for i, ch in enumerate(_SUPERSCRIPT_CHARS)}

# 预编译：匹配一个 Unicode 下标数字
_SUBSCRIPT_DIGITS = re.compile("[" + SUBSCRIPT_CHARS + "]")


# =====================================================================
# 第 1 层：答案归一化——判分前先消灭格式噪音
# =====================================================================
def normalize(text: str) -> str:
    """答案归一化：全角→半角、去空格/美元符/LaTeX 包裹、写下标、转小写。

    得到的数据：任意形态的答案字符串，类似 " （X−2)(X−3) "
    要做的操作：逐类替换格式差异，只留语义内容
    返回：归一化后的字符串，类似 "(x−2)(x−3)"
    为什么：学生答 " (x-2)(x-3) " 和标准答案 "(X-2)(X-3)" 语义相同，
            不归一化会误判——判分第一原则是先消灭格式噪音
    """
    t = text.strip().lower()

    # LaTeX 包裹符直接剥掉：$...$、\(...\)、\[...\]
    t = t.replace("$", "").replace("\\(", "").replace("\\)", "")
    t = t.replace("\\[", "").replace("\\]", "")

    # 去掉所有空白（含中间空格）
    t = re.sub(r"\s+", "", t)

    # 全角 → 半角（括号/等号/逗号/分号/减号是数学答案重灾区）
    # ★ 分号必须转：全角"；"不清掉的话，后面按 [;；] 切空那一步切不开，
    #   "10；7" 会漏到多值集合比对那层 → 韦达题（两个不同的量，顺序有关）
    #   会被误判成"顺序无关"而放过错误答案。
    for full, half in [("（", "("), ("）", ")"), ("＝", "="),
                       ("，", ","), ("；", ";"), ("－", "-"),
                       ("．", "."), ("：", ":")]:
        t = t.replace(full, half)


    # ★ 变量下标归一化：x_1 / x₁ / x1 三种写法是同一个东西
    #   做法：把 Unicode 下标数字先转成 ASCII，再去掉下划线 → "x1"
    t = _SUBSCRIPT_DIGITS.sub(lambda m: str(_SUBSCRIPT_MAP[m.group(0)]), t)
    t = re.sub(r"([a-z])_(\d)", r"\1\2", t)     # x_1 → x1
    t = t.replace("_", "")                      # 兜底：残留的孤立下划线

    return t


# =====================================================================
# 第 2 层：用 sympy 把两边化成同一个表达式（带超时保护）
# =====================================================================
@contextmanager
def _sympy_timeout(seconds: int = 3):
    """给一次 sympy 计算设置耗时上限。

    得到的数据：seconds 超时秒数，默认 3
    要做的操作：进 with 块前装seconds闹钟，出 with 块取消闹钟 并还原原 handler
    返回：上下文管理器本身（with 用，不返回值）
    为什么：复杂表达式 simplify 可能卡死，整条批改链路不能被一道题拖死。
    """
    def _handler(signum, frame):
        raise TimeoutError("sympy 判分超时")

    # 陌生点 signal.SIGALRM：Unix 的闹钟信号——seconds 秒后内核给进程发 SIGALRM，
    #   _handler 抛 TimeoutError 中断当前计算；出 with 块时统一取消，不会残留
    # 限制：只能主线程装（非主线程 raise ValueError → 静默降级为无超时）
    try:
        old = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(seconds)
    except ValueError:
        old = None            # 非主线程：装不上闹钟，静默降级
    try:
        yield                 # yield 是 contextmanager的锚点，先执行yield前代码，然后再执行yield后代码
    finally:
        if old is not None:
            signal.alarm(0)   # 取消闹钟
            signal.signal(signal.SIGALRM, old)  # 恢复原 handler


def expr_equal(student: str, standard: str) -> bool:
    """判断两个表达式是否数学等价：simplify(学生式 - 标准式) == 0。

    得到的数据：两个数学表达式字符串，一个学生写的，一个标答的，类似 "x+1" 与 "1+x"

    要做的操作：剥变量前缀 → ^ 转 ** → parse_expr 解析 → 差值化简看是否恒为 0

    返回：True=数学上等价（如 (x-2)(x-3) 与 x^2-5x+6）；False=不等价或解析失败
    
    为什么：等价写法（x+1 与 1+x、展开式与因式积）不该判错；
            解析失败/超时一律 False——宁可误判错也别把错判对
    """

    # 剥掉 "x=" / "y=" 前缀，填空答 "x=2" 和标准 "2" 是一回事
    def _strip_var_prefix(s: str) -> str:
        return re.sub(r"^[a-z]=", "", s)

    a = _strip_var_prefix(normalize(student))   # 剥离学生回答前缀
    b = _strip_var_prefix(normalize(standard))  # 剥离标答前缀

    # 交给 sympy 前，把 "^" 换成 "**"（学生习惯 ^ 表示乘方，sympy 只认 **）
    a, b = a.replace("^", "**"), b.replace("^", "**")


    # 对 学生回答 和 标答，进行sympy的转换，转化以后，比较两个回答的差值
    try:
        with _sympy_timeout(3):     # 进入上下问管理器
            """
              standard_transformations     基础规则（自动有理数、切分符号等）
                  + 
              implicit_multiplication_application 隐式乘法/函数调用：

              没有它 "5x" 会被解析成符号名 "5x" 直接报错；
              加了它 5x→5*x、2(x+1)→2*(x+1)，学生手写习惯才能被认出来
            """
            from sympy.parsing.sympy_parser import (
                implicit_multiplication_application,
                parse_expr,
                standard_transformations,
            )
            from sympy import simplify

            # transformations：sympy 解析器的"预处理规则集"。由2个规则组合
            transformations = standard_transformations + (implicit_multiplication_application,)
            lhs = parse_expr(a, transformations=transformations, evaluate=True)
            rhs = parse_expr(b, transformations=transformations, evaluate=True)

            # 核心一步：差值恒等于 0 ⇔ 两式数学等价
            # 耗时封顶3秒，超过3秒，自动抛异常
            return simplify(lhs - rhs) == 0
        
    except Exception:
        # 解析失败 / 超时 / 没装 sympy → 判不等价（调用方前面已有归一化精确匹配兜过一轮）
        return False


# =====================================================================
# 第 3 层：分题型判分
# =====================================================================
def grade_choice(student: str, standard: str) -> bool:
    """选择题判分专用：选项字母集合相等即对。

    得到的数据：学生作答 "A"、标准答案 "A"（多选 "AB"）
    要做的操作：两边各抽 A~D 字母，比集合
    返回：True / False
    为什么：用集合比对，顺带消除 "A" / "选A" / "a " 这类写法差异
    """
    # 取答案"A"，并转换成大写，然后存入集合
    s_letters = set(re.findall(r"[A-D]", student.upper()))
    t_letters = set(re.findall(r"[A-D]", standard.upper()))

    # 学生有回答，且回答和标答一致相同，返回True
    return bool(s_letters) and s_letters == t_letters


def _expand_alternatives(standard: str) -> list[str]:
    """把 标准答案 {答案1|答案2} -> ["答案1", "答案2"]。

    得到的数据：标准答案原文，类似 "{x=1|x=-1}" /  "x=2 或 x=3"
    要做的操作：把每个 {a|b} 组替换成各候选答案，展开成[a,b]
    返回：可接受的标准答案变体列表，形如 ["x=1", "x=-1"]
    为什么：一道题的正确答案常有多个（如两个根），题库里用一行语法表达，判分时展开比
    """

    #标答集合
    variants = [standard]

    # 逐个处理 {..} 组：每组展开后替换进现有所有变体
    for group in re.findall(r"\{([^{}]+)\}", standard):  # group："x=1|x=-1"
        options = [o for o in group.split("|")] # ["x=1", "x=-1"]
        variants = [v.replace("{" + group + "}", o) for v in variants for o in options]  # 把 标答 v 全部换成 o

    return variants # ["x=1", "x=-1"]


# =====================================================================
# 第 3.5 层：多值答案按"值的集合"比对（顺序无关）
# =====================================================================
# 场景（2026-10-25 老大实测）：
#   题库标答写成 "x1=2，x2=3"（逗号分隔、变量带下标编号），
#   学生答成 "x=2或x=3"（"或"分隔、变量不带编号）——数学上完全等价，
#   但字符串比不出来 → 明明答对却判错。
#
# 做法：两边都拆成若干"组成部分"，每部分取出一个数值，
#       当作集合比较（顺序无关，x1=3,x2=2 与 x1=2,x2=3 是同一组根）。
# 安全阀：两部分数量必须相同、每部分都必须能解析出一个纯数值，
#         否则本层不生效（宁可不判，也别把错的判对）。

# 答案组成部分的分隔符：逗号 / 分号 / 顿号 / "或"（中文"或者"要排在"或"前面）
_ANSWER_SEP = re.compile(r"或者|或|[，,；;、]")

# 纯数值：可带正负号和小数
_PURE_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")

# 变量名 + 编号：x1 / x2 / x₁（判定"这是根的编号"还是"这是不同的量"）
_ROOT_LABEL = re.compile(r"^x[₀-₉0-9]+$")


def _split_answer_parts(text: str) -> list[str]:
    """把一个答案串拆成多个组成部分。

    得到的数据：已归一化的答案串，类似 "x1=2,x2=3" / "x=2或x=3"
    要做的操作：按逗号/分号/顿号/"或"切分，丢掉空段
    返回：组成部分列表，如 ["x1=2", "x2=3"]
    """
    return [p for p in _ANSWER_SEP.split(text) if p]


def _extract_number(part: str) -> str | None:
    """从一个组成部分里取出数值。

    得到的数据：组成部分，如 "x1=2" / "x=2" / "2" / "-5"
    要做的操作：有 "=" 就取等号右边（丢掉变量名），否则整段当数值
    返回：数值文本（"2" / "-5" / "0.5"）；不是纯数值时返回 None
    为什么：标答的 "x1=2" 和学生的 "x=2" 差的就是变量名，比数值才对得上
    """
    if "=" in part:
        part = part.rpartition("=")[2]
    part = part.strip()
    return part if _PURE_NUMBER.match(part) else None


def _is_root_label(part: str) -> bool:
    """判断一个组成部分的变量名是不是"根的编号"（x1 / x2 / x₁）。

    得到的数据：组成部分的等号左侧，如 "x1" / "x2" / "x"
    要做的操作：匹配 "x + 数字" 的模式
    返回：True=是根编号；False=是别的量（如韦达题里的两个不同量）
    为什么：★ 这是顺序语义的分水岭——
        根（x1=2，x2=3）是同一组解，顺序无关，反了也算对；
        而韦达题（"x1+x2=7；x1·x2=10"）两个空是**不同的量**，顺序有关，
        答 "10；7" 必须判错。靠"变量名是不是 x+编号"来区分这两类题。
    """
    if "=" not in part:
        return False
    left = part.rpartition("=")[0].strip()
    return bool(_ROOT_LABEL.match(left))


def _values_match(student: str, standard: str) -> bool:
    """多值答案比对：根的顺序无关，不同量的顺序有关。

    得到的数据：已归一化的学生答案与标答
    要做的操作：
      1. 两边按分隔符拆组成部分，数量必须相同且 >1
      2. 每部分都必须能解析出纯数值（任一解析不出 → 本层不生效）
      3. ★ 分两种语义：
         - 标答是"根"（x1=2，x2=3）→ 数值集合相同即对（根的编号顺序无所谓）
         - 标答不是"根"（韦达题的 7；10）→ 按顺序逐位比（两个空是不同的量）
    返回：True=答案对；False=不同，或本层不适用
    """
    s_parts = _split_answer_parts(student)
    t_parts = _split_answer_parts(standard)
    if len(s_parts) != len(t_parts) or len(s_parts) <= 1:
        return False

    s_vals = [_extract_number(p) for p in s_parts]
    t_vals = [_extract_number(p) for p in t_parts]
    if any(v is None for v in s_vals) or any(v is None for v in t_vals):
        return False

    s_nums = [float(v) for v in s_vals]
    t_nums = [float(v) for v in t_vals]

    # 标答是"根"语义 → 集合比对（顺序无关）
    if all(_is_root_label(p) for p in t_parts):
        return sorted(s_nums) == sorted(t_nums)

    # 标答是"不同的量"（韦达题等）→ 顺序比对（顺序有关）
    return s_nums == t_nums


def grade_fill(student: str, standard: str) -> bool:
    """填空题判分：精确匹配 → 等价变体 → 表达式等价，多值再按集合比，四层递进。

    得到的数据：学生作答、标准答案，类似 "(x-2)(x-3)" / "{2|-2}" / "x1=2，x2=3"
    要做的操作：
      1. 归一化后精确相等 → 判对
      2. 展开标答 {a|b} 等价语法逐个再比 -- （值相等 或者 表达式等价expr_equal）
      3. 一题多空按序比对：按 "；/;" 切开逐空比 -- （值相等 或者 表达式等价expr_equal）
      4. 多值答案按数值集合比（"x=2或x=3" vs "x1=2，x2=3" 这类写法差异）
    返回：True / False
    为什么：填空题的"正确答案"形态最杂（多空、等价、未化简、写法不一），必须逐层放宽，
            但每一层都是确定性规则，不做模糊匹配
    """
    # 层 1：归一化后，比对，相等返回true
    s, t = normalize(student), normalize(standard)
    if s == t:
        return True

    # 层 2：等价变体（{a|b} 语法）
    for variant in _expand_alternatives(standard):
        v = normalize(variant)
        if s == v or expr_equal(s, v):
            return True

    # 层 3：一题多空按序比对（中文分号/英文分号切空）
    s_blanks = [b for b in re.split(r"[;；]", s) if b]  # 判断是否有多个逗号分割的回答（就是有多空）
    t_blanks = [b for b in re.split(r"[;；]", t) if b]
    if len(s_blanks) == len(t_blanks) and len(s_blanks) > 1:
        return all(
            sb == tb or expr_equal(sb, tb) for sb, tb in zip(s_blanks, t_blanks)  # 值相等 或者 表达式等价
        )

    # 层 4：多值答案按数值集合比（"x=2或x=3" vs "x1=2，x2=3"）
    if _values_match(s, t):
        return True

    # 层 4 之外的兜底：单空但形态是表达式（如 (x-2)(x-3) vs x^2-5x+6）
    return expr_equal(s, t)  # 纯表达式比对



def grade_objective(
    question_type: str,
    student_answer: str,
    standard_answer: str,
    full_score: float,
) -> dict:
    """所有客观题判分统一入口。供外部调用

    得到的数据（逐参数）：
      question_type    题型，选择"choice" / 填空"fill"； 主观题subjective 不进这里（走 LLM 批改图）
                       传未知值 → 按最保守的字符串比对处理，标 method="fallback"
      student_answer   学生作答原文，类似 "x=2 或 x=-2"
      standard_answer  标准答案原文，类似 "{2|-2}"
      full_score       该题满分，类似 4.0；答对给满分、答错给 0，不设部分分

    要做的操作：按题型分发 → 调比对函数 → 组装返回结果
    返回：结果 dict，形如 {"is_correct": True, "score": 4.0, "method": "expr"}
          method 记录本次走的哪条规则（choice/exact/expr/fallback），留痕排查误判
    为什么：客观题答案确定，规则判分比 LLM 更准更快更便宜，也便于事后复现
    """
    if question_type == "choice":
        ok = grade_choice(student_answer, standard_answer)
        return {"is_correct": ok, "score": full_score if ok else 0.0, "method": "choice"}

    if question_type == "fill":
        # 先看是不是精确匹配命中（记 exact），再走三层递进（记 expr）
        if normalize(student_answer) == normalize(standard_answer):
            return {"is_correct": True, "score": full_score, "method": "exact"}
        
        # 再走选择题比对
        ok = grade_fill(student_answer, standard_answer)
        return {"is_correct": ok, "score": full_score if ok else 0.0, "method": "expr"}

    # 未知题型兜底：归一化字符串比对（最保守，绝不放水）
    ok = normalize(student_answer) == normalize(standard_answer)
    return {"is_correct": ok, "score": full_score if ok else 0.0, "method": "fallback"}
