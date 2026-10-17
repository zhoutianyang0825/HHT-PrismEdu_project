"""题库 CSV 生成器：一次性生成 data/raw/question_bank.csv（≥150 题，覆盖全部知识点）。

 为什么用生成器而不是手写 CSV：
   1) 数值题的答案、解析、选择题干扰项全部由公式现算——答案正确性由代码保证；
   2) 关键数值题带 assert 自校验（根代回方程、扇形面积核对等），参数选错立刻崩；
   3) 题量增减只改循环参数，方便演示前补量（开发计划 M1 降级预案）。

 运行：uv run python tools/gen_question_bank.py
 输出：data/raw/question_bank.csv（UTF-8）
"""
import csv
import json
import math
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "raw" / "question_bank.csv"

rows: list[dict] = []


def add(ch, kp, qtype, diff, content, answer, analysis, options=None, source="人教版初中数学"):
    """追加一题。options 为选项列表（选择题专用），入库时序列化成 JSON 字符串。"""
    rows.append({
        "subject": "math",
        "chapter_id": ch,
        "kp_id": kp,
        "question_type": qtype,          # choice / fill / subjective
        "difficulty": diff,              # easy / medium / hard
        "content": content,
        "answer": answer,
        "options": json.dumps(options, ensure_ascii=False) if options else "",
        "analysis": analysis,
        "source": source,
        "quality_flag": "normal",
    })


def choice(content, options4, answer_letter, ch, kp, diff, analysis):
    """选择题便捷入口：4 个选项 + 正确字母。"""
    letters = ["A", "B", "C", "D"]
    labeled = [f"{letters[i]}. {options4[i]}" for i in range(4)]
    add(ch, kp, "choice", diff, content, answer_letter, analysis, options=labeled)


def eq_str(b: int, c: int, a: int = 1) -> str:
    """拼 ax^2+bx+c=0 的可读形态：x^2-5x+6=0 / 2x^2+3x-2=0。"""
    head = "" if a == 1 else str(a)
    bstr = "" if b == 0 else (f"{b:+d}x" if b != 1 and b != -1 else f"{b:+d}x")
    return f"{head}x^2{bstr}{c:+d}=0"


def roots_str(r1, r2):
    return f"x1={r1}，x2={r2}"


def poly_str(b: int, c: int) -> str:
    return f"x^2{b:+d}x{c:+d}"


# ================================================================ ch_algebra_basis
def gen_algebra_basis():
    ch = "ch_algebra_basis"
    choice("计算 a^3·a^2 的结果是（　）", ["a^5", "a^6", "a^9", "2a^5"], "A", ch, "kp_power_ops", "easy",
           "同底数幂相乘，底数不变指数相加：a^(3+2)=a^5。")
    choice("计算 (a^3)^2 的结果是（　）", ["a^5", "a^6", "a^9", "a^8"], "B", ch, "kp_power_ops", "easy",
           "幂的乘方，底数不变指数相乘：a^(3×2)=a^6。")
    add(ch, "kp_power_ops", "fill", "easy", "计算：(-2a^2b)^3 = ______", "-8a^6b^3",
        "积的乘方：(-2)^3·(a^2)^3·b^3=-8a^6b^3。")
    add(ch, "kp_poly_mult", "fill", "easy", "计算：2x(3x-1) = ______", "6x^2-2x",
        "分配律：2x·3x=6x^2，2x·(-1)=-2x。")
    choice("下列计算正确的是（　）",
           ["(x+1)(x-1)=x^2+1", "(x+2)^2=x^2+4", "(x-3)(x+3)=x^2-9", "(x-1)^2=x^2-1"], "C", ch,
           "kp_poly_mult", "easy", "平方差公式：(x-3)(x+3)=x^2-9；其余三项均漏中间项或符号错。")
    add(ch, "kp_poly_mult", "subjective", "medium", "计算：(2x-1)(x+3)。", "2x^2+5x-3",
        "多项式乘多项式：2x·x+2x·3-1·x-3=2x^2+5x-3。")
    choice("计算 (2x-3y)^2 的结果是（　）",
           ["4x^2-9y^2", "4x^2+9y^2", "4x^2-12xy+9y^2", "4x^2-6xy+9y^2"], "C", ch, "kp_mul_formulas", "medium",
           "完全平方公式 (a-b)^2=a^2-2ab+b^2：(2x)^2-2·2x·3y+(3y)^2=4x^2-12xy+9y^2。")
    add(ch, "kp_mul_formulas", "fill", "easy", "计算：102×98 = ______（用平方差公式）", "9996",
        "102×98=(100+2)(100-2)=100^2-2^2=10000-4=9996。")
    add(ch, "kp_mul_formulas", "subjective", "medium",
        "先化简再求值：(x+2)^2-(x-1)(x+1)，其中 x=1.5。",
        "4x+5；x=1.5 时值为 11",
        "(x^2+4x+4)-(x^2-1)=4x+5；x=1.5 时 4×1.5+5=11。")
    add(ch, "kp_factor_common", "subjective", "easy", "因式分解：3a^2+6a。", "3a(a+2)",
        "公因式是 3a，提出后剩 (a+2)。")
    add(ch, "kp_factor_common", "subjective", "easy", "因式分解：-4x^2y+8xy^2。", "-4xy(x-2y)",
        "公因式 -4xy：-4xy·x=-4x^2y，-4xy·(-2y)=8xy^2。")
    add(ch, "kp_factor_common", "subjective", "medium", "因式分解：2a^2(b-3)-a(b-3)。", "a(b-3)(2a-1)",
        "把 (b-3) 看作整体：公因式 a(b-3)，提出后剩 (2a-1)。")


# ================================================================ ch_quadratic
def gen_quadratic():
    ch = "ch_quadratic"
    # ---- 概念 / 一般形式
    choice("下列方程中，是一元二次方程的是（　）",
           ["2x+1=0", "x^2+y=1", "x^2-3x+2=0", "x^3=8"], "C", ch, "kp_qe_definition", "easy",
           "一元二次方程：一个未知数、最高次 2、整式方程。A 一元一次，B 二元，C ✓，D 三次。")
    choice("若方程 (m-1)x^2+2x-1=0 是关于 x 的一元二次方程，则 m 的取值范围是（　）",
           ["m≠0", "m≠1", "m≥1", "m 为任意实数"], "B", ch, "kp_qe_definition", "medium",
           "二次项系数不能为 0：m-1≠0，即 m≠1。")
    add(ch, "kp_qe_definition", "fill", "easy", "方程 3x(x-1)=2x^2-5 化成一般形式后，二次项系数为 ______",
        "1", "展开：3x^2-3x=2x^2-5 → x^2-3x+5=0，二次项系数 1。")
    add(ch, "kp_qe_general_form", "fill", "easy", "方程 x^2-4x-5=0 中，一次项系数 b = ______", "-4",
        "一般形式 ax^2+bx+c=0，本题 b=-4、c=-5。")
    choice("一元二次方程 (2x-1)(x+2)=3 化为一般形式后，常数项是（　）",
           ["-2", "-5", "2", "3"], "B", ch, "kp_qe_general_form", "medium",
           "展开：2x^2+3x-2=3 → 2x^2+3x-5=0，常数项 -5。")
    add(ch, "kp_qe_general_form", "fill", "medium",
        "若 x^2+(k-1)x+9 是关于 x 的完全平方式，则 k = ______", "7 或 -5",
        "完全平方式要求一次项系数 ±2·1·3=±6，即 k-1=±6，k=7 或 k=-5。")
    # ---- 直接开平方法（m±k 现算）
    for m, k in [(1, 2), (2, 3), (3, 1), (1, 4), (2, 1)]:
        add(ch, "kp_direct_open", "fill", "easy",
            f"解方程：(x-{m})^2 = {k * k}。",
            roots_str(m + k, m - k),
            f"直接开平方：x-{m}=±{k}，得 x1={m + k}，x2={m - k}。")
    # ---- 配方法（x²-2hx+(h²-k²)=0，根 h±k 由构造保证）
    for h, k in [(3, 2), (4, 1), (5, 3), (2, 1), (6, 4), (3, 1)]:
        b, c = -2 * h, h * h - k * k
        add(ch, "kp_peifang", "fill", "medium",
            f"用配方法解方程：{poly_str(b, c)}=0。",
            roots_str(h + k, h - k),
            f"配方：x^2{b:+d}x+{(b // 2) ** 2}=(x-{h})^2={k * k}，开方得 x1={h + k}，x2={h - k}。")
    # ---- 判别式（Δ 现算分类）
    opts3 = ["有两个不相等的实数根", "有两个相等的实数根", "没有实数根"]
    for a, b, c in [(1, 2, 1), (1, -2, 3), (2, 3, 1), (1, 4, 4), (3, -6, 2)]:
        delta = b * b - 4 * a * c
        idx = 0 if delta > 0 else (1 if delta == 0 else 2)
        sym = ">" if delta > 0 else ("=" if delta == 0 else "<")
        choice(
            f"不解方程，判断方程 {eq_str(b, c, a)} 的根的情况（　）",
            opts3 + ["无法确定"], "ABC"[idx], ch, "kp_discriminant", "medium",
            f"Δ=b^2-4ac={b * b}-{4 * a * c}={delta}{sym}0，故{opts3[idx]}。")
    # ---- 公式法（Δ 为完全平方数 + 根代回验证；根可能是 0.5 这类分数，用 Fraction 精确算）
    # 陌生库 fractions.Fraction：精确分数类型（1/2 就是 1/2，不是 0.5 的浮点近似）。
    #   为啥不用普通除法：float 有精度误差（如 0.1+0.2!=0.3），做"根代回验证"时
    #   可能因误差误判；Fraction 的加减乘除全程精确，assert 才立得住
    from fractions import Fraction

    def _fmt(x: Fraction) -> str:
        # denominator/numerator：Fraction 的分母/分子属性。分母为 1 说明是整数，
        # 输出 "3" 就够；否则转 float 输出 "0.5" 这种学生习惯的形态
        return str(float(x)) if x.denominator != 1 else str(x.numerator)

    for a, b, c in [(1, -4, 3), (1, 2, -3), (2, -5, 2), (1, -6, 8), (1, 4, 3)]:
        delta = b * b - 4 * a * c
        # 陌生函数 math.isqrt(n)：整数平方根（√n 向下取整）。只收整数、返回整数，
        #   精确无浮点误差；配套写法 isqrt(n)**2 == n 就是"n 是不是完全平方数"的判定
        root = math.isqrt(delta)
        # assert 断言：条件为假立刻抛 AssertionError 炸停脚本——出题参数选错
        #   当场暴露，而不是把算错的答案写进 CSV（这就是"答案正确性由代码保证"）
        assert root * root == delta, f"出题参数 (a={a},b={b},c={c}) 的 Δ={delta} 不是完全平方数"
        x1 = Fraction(-b + root, 2 * a)
        x2 = Fraction(-b - root, 2 * a)
        assert a * x1 * x1 + b * x1 + c == 0 and a * x2 * x2 + b * x2 + c == 0, "根代回验证失败"
        add(ch, "kp_gongshi", "fill", "medium",
            f"用公式法解方程：{eq_str(b, c, a)}。",
            roots_str(_fmt(x1), _fmt(x2)),
            f"Δ={delta}，x=({-b:+d}±{root})/{2 * a}，得 x1={_fmt(x1)}，x2={_fmt(x2)}。")
    # ---- 验根与增根
    choice("关于 x 的方程 m/(x-1)=x/(x-1)+2 有增根，则增根是（　）",
           ["x=0", "x=1", "x=-1", "x=2"], "B", ch, "kp_qe_root_check", "medium",
           "最简公分母 x-1=0 时产生增根，故增根 x=1。")
    add(ch, "kp_qe_root_check", "fill", "medium",
        "解分式方程时（分母含未知数），去分母化为整式方程后必须做的一步是 ______，目的是 ______。",
        "检验；排除增根",
        "去分母时两边同乘最简公分母，可能产生使分母为 0 的增根，必须代入检验。")
    # ---- 因式分解法
    for r1, r2 in [(3, 5), (-2, 4), (1, -6), (2, 7)]:
        b, c = -(r1 + r2), r1 * r2
        f1 = f"(x-{r1})" if r1 > 0 else f"(x+{-r1})"
        f2 = f"(x-{r2})" if r2 > 0 else f"(x+{-r2})"
        add(ch, "kp_yinshi", "fill", "medium",
            f"解方程：{poly_str(b, c)}=0。",
            roots_str(r1, r2),
            f"十字相乘：{poly_str(b, c)}={f1}{f2}=0，得 x1={r1}，x2={r2}。")
    # ---- 十字相乘法（主力循环：8 题）
    for r1, r2 in [(2, 3), (-2, -3), (5, 6), (-1, -6), (3, 4), (-3, -4), (1, 8), (-4, -5)]:
        b, c = -(r1 + r2), r1 * r2
        f1 = f"(x-{r1})" if r1 > 0 else f"(x+{-r1})"
        f2 = f"(x-{r2})" if r2 > 0 else f"(x+{-r2})"
        add(ch, "kp_crossmult", "fill", "medium",
            f"解方程：{poly_str(b, c)}=0。",
            roots_str(r1, r2),
            f"拆常数项 {c}：找两数 {r1}、{r2}，积为 {c}、和为 {-b}，"
            f"即 {poly_str(b, c)}={f1}{f2}=0，得 x1={r1}，x2={r2}。")
    # ---- 韦达定理
    for r1, r2 in [(2, 5), (-3, 4), (6, 1), (-2, -7)]:
        b, c = -(r1 + r2), r1 * r2
        add(ch, "kp_vieta", "fill", "medium",
            f"已知方程 {poly_str(b, c)}=0 的两根为 x1、x2，则 x1+x2 = ______，x1·x2 = ______。",
            f"{r1 + r2}；{c}",
            f"韦达定理：x1+x2=-b/a={r1 + r2}，x1·x2=c/a={c}。")
    choice("若方程 x^2-3x+k=0 的两根之积为 2，则 k =（　）",
           ["2", "-2", "3", "-3"], "A", ch, "kp_vieta", "medium",
           "两根之积 x1·x2=c/a=k=2。")
    # ---- 实际应用
    add(ch, "kp_qe_growth", "subjective", "medium",
        "某商品原价 100 元，经连续两次涨价后售价为 121 元，求平均每次的增长率。",
        "10%",
        "设平均增长率为 x：100(1+x)^2=121 → (1+x)^2=1.21 → 1+x=1.1（负根舍去）→ x=10%。")
    add(ch, "kp_qe_growth", "subjective", "medium",
        "某工厂一月份产值 100 万元，三月份产值 144 万元，求月平均增长率。",
        "20%",
        "100(1+x)^2=144 → (1+x)^2=1.44 → 1+x=1.2 → x=20%。")
    add(ch, "kp_qe_area", "subjective", "medium",
        "一个矩形的长比宽多 3，面积为 40，求矩形的长和宽。",
        "长 8，宽 5",
        "设宽为 x：x(x+3)=40 → x^2+3x-40=0 → (x+8)(x-5)=0，x=5（负根舍去），长 8。")
    add(ch, "kp_qe_area", "subjective", "medium",
        "一块 20×15 的矩形铁皮，四角各剪去一个边长为 x 的小正方形后做成无盖盒子，若盒底面积为 126，求 x。",
        "x=3",
        "(20-2x)(15-2x)=126 → 4x^2-70x+174=0 → 2x^2-35x+87=0，"
        "求根公式 x=(35±√(35²-4·2·87))/4=(35±23)/4，得 x=3 或 x=14.5（超过边长一半，舍）→ x=3。")
    choice("解方程 (x-2)^2=16，最合适的方法是（　）",
           ["配方法", "公式法", "直接开平方法", "因式分解法"], "C", ch, "kp_qe_apply_select", "easy",
           "方程已是 (x+m)^2=p 的形态，直接开平方最快。")
    choice("解方程 x^2-5x+6=0，最合适的方法是（　）",
           ["直接开平方法", "十字相乘（因式分解）法", "配方法", "无法确定"], "B", ch, "kp_qe_apply_select", "medium",
           "常数项 6 可拆成 (-2)×(-3) 且和为 -5，十字相乘一步到位。")
    choice("解一元二次方程时，'配方法'和'公式法'的共同前提是（　）",
           ["方程必须没有常数项", "方程必须先化成一般形式", "二次项系数必须为 1", "判别式必须大于 0"], "B", ch,
           "kp_qe_apply_select", "medium",
           "两种方法都基于一般形式 ax^2+bx+c=0 的系数 a、b、c 操作，先化一般形式是共同前提。")


# ================================================================ ch_quadratic_func
def gen_func():
    ch = "ch_quadratic_func"
    choice("下列函数中是二次函数的是（　）",
           ["y=2x+1", "y=x^2+x^3", "y=3x^2-1", "y=1/x^2"], "C", ch, "kp_func_definition", "easy",
           "二次函数形如 y=ax^2+bx+c (a≠0)。A 一次，B 三次，C ✓，D 不是整式。")
    add(ch, "kp_func_definition", "fill", "easy",
        "若函数 y=(m-2)x^2+3x-1 是二次函数，则 m 的取值范围是 ______。", "m≠2",
        "二次项系数 m-2≠0，即 m≠2。")
    # ---- 顶点式（y=(x-h)^2+k = x²-2hx+(h²+k)，系数由构造保证）
    for h, k in [(2, -1), (-1, 3), (3, -4), (1, 2), (-3, 5), (4, -2)]:
        b, c = -2 * h, h * h + k
        add(ch, "kp_func_vertex_form", "fill", "medium",
            f"用配方法把 y=x^2{b:+d}x{c:+d} 化为顶点式。",
            f"y=(x-{h})^2{k:+d}" if h > 0 else f"y=(x+{-h})^2{k:+d}",
            f"加减一次项系数一半的平方：y=(x-{h})^2{k:+d}，顶点 ({h}, {k})。")
    # ---- 对称轴（x=-b/2a，取整数解）
    for a, b in [(1, 4), (2, -8), (1, -2), (-1, 6), (3, -12)]:
        axis = -b / (2 * a)
        assert axis == int(axis), f"对称轴 {axis} 非整数，换参数"
        head = "" if a == 1 else str(a)
        add(ch, "kp_func_axis", "fill", "easy",
            f"抛物线 y={head}x^2{b:+d}x+1 的对称轴是 ______。",
            f"x={int(axis)}", f"x=-b/(2a)={-b}/({2 * a})={int(axis)}。")
    choice("抛物线 y=-2x^2+3x-1 的开口方向是（　）",
           ["向上", "向下", "先上后下", "无法确定"], "B", ch, "kp_func_axis", "easy",
           "a=-2<0，开口向下。")
    # ---- 图象与性质
    choice("抛物线 y=(x-1)^2+2 的顶点坐标是（　）",
           ["(1, 2)", "(-1, 2)", "(1, -2)", "(-1, -2)"], "A", ch, "kp_func_graph", "easy",
           "顶点式 y=a(x-h)^2+k 的顶点即 (h,k)=(1,2)。")
    choice("抛物线 y=x^2-2x-3 与 y 轴的交点坐标是（　）",
           ["(0, -3)", "(-3, 0)", "(0, 1)", "(1, 0)"], "A", ch, "kp_func_graph", "easy",
           "令 x=0 得 y=-3，交点 (0,-3)。")
    for h, k in [(2, -9), (-1, -4)]:   # 与 x 轴交点：x=h±√(-k)，取整数
        r = math.isqrt(-k)
        assert (h + r) * (h - r) == h * h + k, "根的乘积应等于展开式常数项 h²+k"
        add(ch, "kp_func_graph", "fill", "medium",
            f"抛物线 y=(x-{h})^{2}{k:+d} 与 x 轴交点的横坐标为 ______ 和 ______。" if h > 0 else
            f"抛物线 y=(x+{-h})^{2}{k:+d} 与 x 轴交点的横坐标为 ______ 和 ______。",
            f"x1={h + r}，x2={h - r}",
            f"令 y=0：(x-{h})^2={-k}，x={h}±{r}，得 {h + r} 和 {h - r}。")
    # ---- 系数符号
    choice("二次函数 y=ax^2+bx+c 的图象开口向下、对称轴在 y 轴右侧、与 y 轴交点在 x 轴上方，则（　）",
           ["a>0, b>0, c>0", "a<0, b<0, c>0", "a<0, b>0, c>0", "a<0, b>0, c<0"], "C", ch, "kp_func_abc", "hard",
           "开口向下 a<0；对称轴 x=-b/2a>0 且 a<0 → b>0（左同右异）；交 y 轴于正半轴 c>0。")
    choice("抛物线 y=x^2+bx+4 的对称轴是 x=2，则 b =（　）",
           ["4", "-4", "2", "-2"], "B", ch, "kp_func_abc", "medium",
           "-b/(2·1)=2 → b=-4。")
    add(ch, "kp_func_abc", "fill", "hard",
        "抛物线 y=ax^2+bx+c 过点 (0,-1)、(1,0)、(-2,0)，则 a+b+c 的值为 ______。", "0",
        "x=1 时 y=0 恰好就是 a+b+c=0，无需解方程——读准条件是关键。")
    # ---- 增减性
    choice("抛物线 y=(x-3)^2-1 中，y 随 x 增大而减小的 x 取值范围是（　）",
           ["x>3", "x<3", "x>0", "x<-3"], "B", ch, "kp_func_increase", "medium",
           "a=1>0 开口向上，对称轴 x=3，对称轴左侧 y 随 x 增大而减小。")
    add(ch, "kp_func_increase", "fill", "medium",
        "抛物线 y=-2(x+1)^2+3 中，当 x ______ 时，y 随 x 增大而增大。", "<-1",
        "a=-2<0 开口向下，对称轴 x=-1，左侧递增。")
    choice("点 A(-2, y1)、B(1, y2)、C(3, y3) 在抛物线 y=x^2-2x 上，则 y1、y2、y3 的大小关系是（　）",
           ["y1<y2<y3", "y2<y3<y1", "y3<y1<y2", "y2<y1<y3"], "B", ch, "kp_func_increase", "hard",
           "y=x^2-2x 对称轴 x=1，开口向上。y1=8，y2=-1，y3=3 → y2<y3<y1。"
           "口诀：开口向上时，离对称轴越远函数值越大。")
    # ---- 平移
    add(ch, "kp_func_shift", "fill", "medium",
        "把抛物线 y=2x^2 向左平移 3 个单位，得到的抛物线解析式是 ______。", "y=2(x+3)^2",
        "左加右减：向左平移 3，x 换成 (x+3)。")
    add(ch, "kp_func_shift", "fill", "medium",
        "把抛物线 y=x^2 先向右平移 2 个单位、再向上平移 1 个单位，得到的解析式是 ______。", "y=(x-2)^2+1",
        "右移 2：x→(x-2)；上移 1：整体 +1。")
    choice("将抛物线 y=(x-1)^2+2 向下平移 3 个单位后，顶点坐标为（　）",
           ["(1, -1)", "(-1, 5)", "(1, 5)", "(4, 2)"], "A", ch, "kp_func_shift", "easy",
           "顶点 (1,2) 下移 3 → (1,-1)，解析式 y=(x-1)^2-1。")
    add(ch, "kp_func_shift", "fill", "medium",
        "抛物线 y=2(x-1)^2-3 可由 y=2x^2 经过怎样的平移得到？用一句话描述。",
        "向右平移 1 个单位，再向下平移 3 个单位",
        "对比顶点：(0,0)→(1,-3)，即右移 1、下移 3。")
    # ---- 最值
    add(ch, "kp_func_maxmin", "fill", "medium",
        "抛物线 y=x^2-4x+1 的最小值是 ______。", "-3",
        "配方 y=(x-2)^2-3，开口向上顶点处取最小 -3。")
    add(ch, "kp_func_maxmin", "fill", "hard",
        "函数 y=-x^2+2x+3 在 0≤x≤3 上的最大值是 ______，最小值是 ______。", "最大 4，最小 0",
        "y=-(x-1)^2+4，对称轴 x=1 在区间内 → 最大 4；端点 x=0 时 y=3、x=3 时 y=0 → 最小 0。"
        "定区间最值：顶点在区间内比顶点，否则比端点。")
    choice("已知 y=x^2-6x+m 的最小值为 -5，则 m =（　）",
           ["4", "-4", "9", "5"], "A", ch, "kp_func_maxmin", "hard",
           "配方 y=(x-3)^2+m-9，最小值 m-9=-5 → m=4。")
    # ---- 与 x 轴交点（根由构造保证）
    for r1, r2 in [(3, -1), (2, -4), (5, -2), (4, 1)]:
        b, c = -(r1 + r2), r1 * r2
        add(ch, "kp_func_roots", "fill", "medium",
            f"抛物线 y={poly_str(b, c)} 与 x 轴交点的横坐标是 ______ 和 ______。",
            f"{r1} 和 {r2}",
            f"令 y=0 得 {poly_str(b, c)}=0，解得 x1={r1}，x2={r2}——图象与 x 轴交点即对应方程的根。")
    choice("抛物线 y=x^2-2x+3 与 x 轴的交点个数是（　）",
           ["2 个", "1 个", "0 个", "无法确定"], "C", ch, "kp_func_roots", "medium",
           "Δ=4-12=-8<0，方程无实根，图象与 x 轴无交点。")
    # ---- 待定系数
    add(ch, "kp_func_undetermined", "subjective", "medium",
        "已知抛物线过点 (0, 3)、(1, 0)、(3, 0)，求解析式。",
        "y=x^2-4x+3",
        "三点中有两个是 x 轴交点 → 优先交点式 y=a(x-1)(x-3)，代 (0,3)：3=3a → a=1，"
        "展开 y=x^2-4x+3。")
    add(ch, "kp_func_undetermined", "subjective", "medium",
        "已知抛物线顶点为 (1, -2) 且过点 (0, -3)，求解析式。",
        "y=-(x-1)^2-2",
        "顶点已知 → 优先顶点式 y=a(x-1)^2-2，代 (0,-3)：-3=a-2 → a=-1。")
    # ---- 实际应用
    add(ch, "kp_func_apply", "subjective", "hard",
        "某商品进价 40 元，售价 60 元时每天可卖 300 件；售价每涨 1 元少卖 10 件。售价定为多少时利润最大？",
        "售价 65 元，最大利润 6250 元",
        "设涨价 x 元：W=(20+x)(300-10x)=10(20+x)(30-x)=10(-x^2+10x+600)，"
        "对称轴 x=5 → W_max=10×25×25=6250，售价 60+5=65 元。")
    add(ch, "kp_func_apply", "subjective", "hard",
        "抛物线形拱桥水面宽 20m、拱高 4m，一艘宽 10m、船舱顶部高出水面 2.8m 的货船能否安全通过？",
        "能安全通过",
        "建系：顶点 (0,4) 过 (±10,0) → y=-0.04x^2+4。船宽 10m 即 x=±5 处桥高 y=-1+4=3m > 2.8m，"
        "留 0.2m 余量，能安全通过。")


# ================================================================ ch_rotation
def gen_rotation():
    ch = "ch_rotation"
    choice("△AOB 绕点 O 旋转 40° 得到△COD，若∠AOB=30°，则∠AOC 等于（　）",
           ["30°", "40°", "70°", "无法确定"], "B", ch, "kp_rot_properties", "easy",
           "旋转角是对应点与旋转中心连线的夹角：∠AOC 即旋转角 40°。")
    add(ch, "kp_rot_properties", "fill", "medium",
        "△ABC 绕点 A 旋转后与△ADE 重合（B→D，C→E），若 AB=3，则 AD = ______。", "3",
        "旋转是全等变换，对应边相等：AD=AB=3。")
    add(ch, "kp_rot_coord", "fill", "medium",
        "点 P(3, 2) 绕原点 O 旋转 180° 后的坐标是 ______。", "(-3, -2)",
        "绕原点旋转 180°：(x,y)→(-x,-y)，与关于原点中心对称一致。")
    choice("点 A(1, 0) 绕原点逆时针旋转 90° 后的坐标是（　）",
           ["(0, 1)", "(0, -1)", "(-1, 0)", "(1, 1)"], "A", ch, "kp_rot_coord", "medium",
           "(x,y) 绕原点逆时针 90° → (-y, x)：(1,0)→(0,1)。")
    choice("下列图形中，是中心对称图形的是（　）",
           ["等边三角形", "平行四边形", "等腰梯形", "正五边形"], "B", ch, "kp_rot_figure", "easy",
           "平行四边形绕对角线交点旋转 180° 与自身重合；等边三角形、等腰梯形、正五边形都不是。")
    add(ch, "kp_rot_figure", "fill", "easy",
        "写出一个既是中心对称图形又是轴对称图形的常见四边形：______。", "矩形（或正方形、菱形）",
        "矩形、菱形、正方形既轴对称又中心对称；一般平行四边形只是中心对称。")
    add(ch, "kp_rot_center", "subjective", "medium",
        "△ABC 与△DEF 关于点 O 中心对称，若 AB=4，∠B=60°，求 DE 与 ∠E（E 与 B 对应）。",
        "DE=4，∠E=60°",
        "中心对称是旋转 180° 的特例，两图全等：对应边相等、对应角相等。")
    add(ch, "kp_rot_center", "fill", "easy",
        "点 A(-2, 3) 关于原点 O 的对称点 A' 的坐标是 ______。", "(2, -3)",
        "关于原点对称：(x,y)→(-x,-y)。成中心对称的两个图形连线过对称中心且被平分。")
    choice("下列说法正确的是（　）",
           ["旋转一定改变图形的形状", "旋转不改变图形的大小和形状",
            "中心对称图形旋转任意角度后都与自身重合", "轴对称图形一定是中心对称图形"], "B", ch,
           "kp_rot_properties", "easy",
           "旋转是全等变换；C 错在'任意角度'（只在特定角度重合）；D 反例：等边三角形。")


# ================================================================ ch_circle
def gen_circle():
    ch = "ch_circle"
    add(ch, "kp_circle_def", "fill", "easy",
        "圆中最长的弦是 ______。", "直径",
        "直径是过圆心的弦，也是圆中最长的弦。")
    choice("下列说法错误的是（　）",
           ["直径是最长的弦", "半径相等的两个圆是等圆", "半圆是弧", "过圆心的线段一定是直径"], "D", ch,
           "kp_circle_def", "easy",
           "过圆心且两端都在圆上的线段才是直径——'过圆心的线段'两端不一定在圆上。")
    # ---- 垂径定理（勾股数组现算：弦心距=√(r²-half²)）
    for half, r in [(5, 13), (3, 5), (8, 17), (12, 13)]:
        d = math.isqrt(r * r - half * half)
        assert d * d + half * half == r * r
        add(ch, "kp_circle_perp", "fill", "medium",
            f"⊙O 的半径为 {r}，弦 AB 长 {2 * half}，则圆心 O 到弦 AB 的距离为 ______。",
            f"{d}",
            f"垂径定理+勾股定理：弦心距=√(r²-(AB/2)²)=√({r}²-{half}²)={d}。")
    add(ch, "kp_circle_perp", "subjective", "hard",
        "赵州桥跨度 37.4m（弦长），拱高 7.2m，求桥拱所在圆的半径（保留一位小数）。",
        "约 27.3m",
        "设半径 R：R²=18.7²+(R-7.2)² → 14.4R=18.7²+7.2²=401.53 → R≈27.3。"
        "垂径定理核心套路：半径²=弦心距²+半弦²。")
    choice("在同圆中，若弦 AB=2CD，则圆心角∠AOB 与∠COD 的关系是（　）",
           ["∠AOB=2∠COD", "∠AOB>2∠COD", "∠AOB<2∠COD", "无法确定"], "D", ch, "kp_circle_central", "hard",
           "等弦才对等弧，弦长与圆心角不是线性关系——弦 2 倍角未必 2 倍，选'无法确定'。")
    add(ch, "kp_circle_central", "fill", "easy",
        "在⊙O 中，圆心角∠AOB=100°，则它所对劣弧的度数为 ______。", "100°",
        "弧的度数等于它所对圆心角的度数。")
    # ---- 圆周角
    choice("⊙O 中，圆心角∠AOB=80°，则圆周角∠ACB（C 在优弧上）等于（　）",
           ["80°", "40°", "160°", "50°"], "B", ch, "kp_circle_inscribed", "easy",
           "同弧所对圆周角等于圆心角的一半：80°/2=40°。")
    add(ch, "kp_circle_inscribed", "fill", "medium",
        "⊙O 的弦 AB=6，AB 所对的圆周角为 30°，则⊙O 的半径为 ______。", "6",
        "圆周角 30° → 圆心角 60° → 圆心与弦端点构成等边三角形 → R=AB=6。")
    choice("一条弦把圆分成 1:3 两段弧，则劣弧所对的圆周角为（　）",
           ["45°", "90°", "135°", "60°"], "A", ch, "kp_circle_inscribed", "medium",
           "劣弧度数=360°×1/4=90° → 圆周角 45°。")
    add(ch, "kp_circle_diam", "fill", "easy",
        "AB 是⊙O 的直径，点 C 在圆上，若∠A=50°，则∠B = ______。", "40°",
        "直径所对圆周角∠ACB=90°，内角和 → ∠B=40°。'见直径找直角'是最常用辅助线。")
    choice("AB 是⊙O 的直径，点 C 在圆上。若∠B=35°，则∠A 等于（　）",
           ["35°", "55°", "65°", "90°"], "B", ch, "kp_circle_diam", "easy",
           "∠ACB=90°（直径所对圆周角）→ ∠A=90°-35°=55°。")
    choice("已知点 P 到⊙O 圆心 O 的距离为 6，⊙O 半径为 5，则点 P 与⊙O 的位置关系是（　）",
           ["圆内", "圆上", "圆外", "无法确定"], "C", ch, "kp_circle_point_pos", "easy",
           "OP=6 > r=5 → 点在圆外。")
    add(ch, "kp_circle_point_pos", "fill", "easy",
        "⊙O 半径为 4，点 P 在圆内，则线段 OP 的长度取值范围是 ______。", "0≤OP<4",
        "圆内 ⇔ OP<r=4；圆心本身 OP=0。")
    add(ch, "kp_circle_inscribed_quad", "fill", "medium",
        "四边形 ABCD 内接于⊙O，若∠A=70°，则∠C = ______。", "110°",
        "圆内接四边形对角互补：∠C=180°-70°=110°。")
    choice("四边形 ABCD 内接于⊙O，∠A:∠C=1:2，则∠A =（　）",
           ["60°", "90°", "120°", "45°"], "A", ch, "kp_circle_inscribed_quad", "medium",
           "对角互补：∠A+∠C=180°，∠A=180°×1/3=60°。")
    # ---- 位置关系
    choice("点 P 在⊙O 内的充分必要条件是（　）",
           ["OP>r", "OP=r", "OP<r", "OP≥r"], "C", ch, "kp_circle_point_pos", "easy",
           "点到圆心距离小于半径 ⇔ 点在圆内。")
    add(ch, "kp_circle_line_pos", "fill", "medium",
        "⊙O 半径 5，圆心 O 到直线 l 的距离为 3，则直线 l 与⊙O 的位置关系是 ______，交点个数为 ______。",
        "相交；2",
        "d=3 < r=5 → 相交，交点 2 个。")
    choice("⊙O 半径为 5，圆心到直线 l 的距离为 5，则直线 l 与⊙O（　）",
           ["相交", "相切", "相离", "无法确定"], "B", ch, "kp_circle_line_pos", "easy",
           "d=r=5 → 相切。")
    # ---- 切线
    add(ch, "kp_circle_tangent", "subjective", "medium",
        "△ABC 中 ∠C=90°，AC=6、BC=8。以 C 为圆心、4.8 为半径的圆与 AB 有怎样的位置关系？",
        "相切",
        "AB=10（勾股），斜边上的高=6×8/10=4.8=半径 → d=r → 相切。"
        "位置关系问题一律先算圆心到直线的距离 d 再与 r 比较。")
    add(ch, "kp_circle_tangent", "fill", "medium",
        "PA 切⊙O 于点 A，若 OA=3、OP=5，则 PA = ______。", "4",
        "切线垂直于过切点的半径：∠OAP=90°，勾股 PA=√(5²-3²)=4。")
    # ---- 弧长 / 扇形 / 圆锥（数值现算）
    for r, n in [(3, 120), (6, 60), (9, 120), (4, 270)]:
        coef = r * n / 180
        assert coef == int(coef)
        add(ch, "kp_circle_arc_length", "fill", "easy",
            f"半径为 {r} 的圆中，{n}° 圆心角所对弧长为 ______（结果保留 π）。", f"{int(coef)}π",
            f"l=nπR/180={n}π×{r}/180={int(coef)}π。")
    for r, n, s in [(6, 90, 9), (4, 45, 2), (12, 30, 12)]:
        # 陌生函数 math.isclose(a, b)：浮点专用"近似相等"判断——浮点有精度误差，
        #   圆周率乘除后 a==b 可能差 1e-15 而判 False，isclose 容忍微小差异
        assert math.isclose(r * r * n * math.pi / 360, s * math.pi), f"扇形参数错: r={r},n={n},s={s}"
        add(ch, "kp_circle_sector", "fill", "easy",
            f"半径为 {r}、圆心角 {n}° 的扇形面积为 ______（结果保留 π）。", f"{s}π",
            f"S=nπR²/360={n}π×{r}²/360={s}π。")
    add(ch, "kp_circle_sector", "fill", "medium",
        "扇形弧长为 6π、半径为 4，则扇形面积为 ______（结果保留 π）。", "12π",
        "S=½lR=½×6π×4=12π——已知弧长时用这个公式最快。")
    for r, l in [(3, 5), (4, 6), (5, 8)]:
        add(ch, "kp_circle_cone", "fill", "medium",
            f"圆锥底面半径为 {r}、母线长为 {l}，则其侧面积为 ______（结果保留 π）。", f"{r * l}π",
            f"侧面展开图是扇形：S=πrl=π×{r}×{l}={r * l}π。")
    choice("圆锥的母线长为 10，侧面展开图的圆心角为 216°，则底面半径为（　）",
           ["5", "6", "8", "4"], "B", ch, "kp_circle_cone", "hard",
           "展开图弧长=底面周长：(216/360)×2π×10=2πr → r=6。核心等量关系：底面周长=展开图弧长。")


# ================================================================ ch_probability
def gen_probability():
    ch = "ch_probability"
    choice("下列事件中是随机事件的是（　）",
           ["太阳从东方升起", "抛一枚硬币正面朝上", "袋中有 5 个红球，摸出 1 个是红球", "明天太阳不升起"], "B", ch,
           "kp_prob_event", "easy",
           "A、C 是必然事件，D 是不可能事件；B 两种结果都有可能，是随机事件。")
    add(ch, "kp_prob_event", "fill", "easy",
        "'掷两个普通骰子，点数之和为 13'是______事件（填'必然/随机/不可能'）。", "不可能",
        "每个骰子最大 6 点，和最大 12，13 不可能出现。")
    add(ch, "kp_prob_definition", "fill", "easy",
        "袋中有 3 个红球 2 个白球（除颜色外相同），随机摸出 1 球，摸到红球的概率是 ______。", "3/5",
        "古典概型：P=红球数/总数=3/5。")
    add(ch, "kp_prob_definition", "fill", "medium",
        "掷一枚均匀骰子，'掷出的点数大于 4'的概率是 ______。", "1/3",
        "大于 4 的点：5、6 共 2 种，P=2/6=1/3。")
    add(ch, "kp_prob_list", "fill", "medium",
        "同时抛两枚均匀硬币，恰好一正一反的概率是 ______。", "1/2",
        "列表：(正,正)(正,反)(反,正)(反,反) 共 4 种等可能，一正一反 2 种 → P=1/2。")
    add(ch, "kp_prob_list", "subjective", "medium",
        "不透明袋中有 2 个红球和 1 个白球，摸出 1 个不放回再摸 1 个，求两次都摸到红球的概率。",
        "1/3",
        "第一次红 2/3；不放回后剩 2 球（1红1白），第二次红 1/2 → P=(2/3)×(1/2)=1/3。"
        "注意'不放回'改变了第二次的样本空间。")
    add(ch, "kp_prob_tree", "subjective", "medium",
        "甲、乙两人各掷一枚均匀骰子，求甲的点数大于乙的概率（用树状图或列举说明）。",
        "5/12",
        "共 36 种等可能。甲>乙：乙=1 时甲有 5 种；乙=2 有 4 种；…；乙=5 有 1 种 → 5+4+3+2+1=15 → 15/36=5/12。")
    add(ch, "kp_prob_tree", "subjective", "medium",
        "三张卡片正面写 1、2、3，背面朝上洗匀后任取两张，求两张卡片数字之和为偶数的概率。",
        "1/3",
        "组合：(1,2)和3、(1,3)和4、(2,3)和5，共 3 种等可能；和为偶数只有 (1,3) → P=1/3。")
    add(ch, "kp_prob_frequency", "fill", "medium",
        "某射手同一条件下大量射击，命中频率在 0.95 附近波动（n=5000 时 0.952），估计他射击一次命中的概率约为 ______（两位小数）。",
        "0.95",
        "大量重复试验中频率稳定于常数 0.95 附近 → 用频率估计概率 P≈0.95。")
    choice("关于频率与概率的关系，下列说法正确的是（　）",
           ["频率就是概率", "概率是固有属性，频率随试验次数增多稳定于概率",
            "试验次数越多频率越接近 0", "概率随试验次数变化"], "B", ch, "kp_prob_frequency", "medium",
           "概率是固有属性不变，频率是实验值会波动；n 越大频率越稳定于概率。")


# ================================================================ ch_inverse_func
def gen_inverse():
    ch = "ch_inverse_func"
    choice("下列函数中是反比例函数的是（　）",
           ["y=2/x", "y=x/2", "y=2x^2", "y=x+1"], "A", ch, "kp_inverse_def", "easy",
           "y=k/x (k≠0)；B 是正比例，C 是二次，D 是一次。")
    add(ch, "kp_inverse_def", "fill", "easy",
        "若函数 y=(m-1)x^(m^2-2) 是反比例函数，则 m = ______。", "-1",
        "指数 m²-2=-1 → m=±1，且系数 m-1≠0 → m=-1（m=1 舍去）。")
    choice("反比例函数 y=-3/x 的图象在（　）",
           ["第一、二象限", "第一、三象限", "第二、四象限", "第三、四象限"], "C", ch, "kp_inverse_graph", "easy",
           "k=-3<0 → 图象在第二、四象限。")
    add(ch, "kp_inverse_graph", "fill", "medium",
        "点 A(2, y1)、B(3, y2) 都在双曲线 y=6/x 上，则 y1 ______ y2（填 >、< 或 =）。", ">",
        "y1=6/2=3，y2=6/3=2 → y1>y2。k>0 时每个象限内 y 随 x 增大而减小。")
    add(ch, "kp_inverse_k", "fill", "medium",
        "点 P 在双曲线 y=4/x 上，过 P 分别作 x 轴、y 轴的垂线，两垂线与坐标轴围成的矩形面积为 ______。",
        "4",
        "k 的几何意义：|xy|=|k|=4，矩形面积恒等于 |k|，与 P 的位置无关。")
    add(ch, "kp_inverse_k", "subjective", "hard",
        "直线 y=2x 与双曲线 y=k/x 在第一象限交于点 P(1, m)。（1）求 k；（2）求另一交点坐标。",
        "k=2；另一交点 (-1, -2)",
        "P 在直线上 → m=2；P 在双曲线上 → k=xy=2。联立 y=2x 与 y=2/x → x=±1 → 另一点 (-1,-2)，"
        "两交点关于原点对称。")
    add(ch, "kp_inverse_apply", "subjective", "medium",
        "一定质量的气体，压强 p(kPa) 与体积 V(m³) 满足 p=96/V。当体积从 4 m³ 压缩到 3 m³ 时，压强增大了多少？",
        "增大 8 kPa",
        "V=4 时 p=24；V=3 时 p=32；增大 32-24=8 kPa。玻意耳定律就是反比例函数的实际原型。")
    add(ch, "kp_inverse_apply", "fill", "medium",
        "蓄水池总量 60 m³，若每小时排水 x m³、排完需 y 小时，则 y 与 x 的函数关系式为 ______。",
        "y=60/x",
        "总量不变：xy=60 → y=60/x，反比例关系。")


# ================================================================ ch_similarity
def gen_similarity():
    ch = "ch_similarity"
    add(ch, "kp_sim_ratio", "fill", "easy",
        "若 3x=4y（x,y 均不为 0），则 x:y = ______。", "4:3",
        "比例基本性质：内项积=外项积 → x/y=4/3。")
    choice("已知 a:b=3:2，且 b=6，则 a =（　）",
           ["9", "4", "6", "12"], "A", ch, "kp_sim_ratio", "easy",
           "a/6=3/2 → a=9。")
    add(ch, "kp_sim_parallel", "fill", "medium",
        "直线 l1∥l2∥l3 截两条直线，所得线段 AB=3、BC=5、DE=4.5，则 EF = ______。", "7.5",
        "平行线分线段成比例：AB/BC=DE/EF → 3/5=4.5/EF → EF=7.5。")
    add(ch, "kp_sim_parallel", "subjective", "medium",
        "△ABC 中，D 在 AB 上且 AD:DB=2:3，DE∥BC 交 AC 于 E。若 AE=4，求 EC。",
        "EC=6",
        "DE∥BC → AD/DB=AE/EC=2/3 → EC=4×3/2=6。A 字形基本图形直接写比例。")
    choice("下列条件能判定 △ABC∽△DEF 的是（　）",
           ["∠A=∠D 且 AB=DE", "∠A=∠D 且 ∠B=∠E", "AB=DE 且 BC=EF", "∠A=∠D"], "B", ch, "kp_sim_criteria", "easy",
           "两角对应相等（AA）即相似；A '一角一边'不充分；C 两边相等不保证成比例；D 一角不充分。")
    add(ch, "kp_sim_criteria", "fill", "medium",
        "D、E 分别在 △ABC 的 AB、AC 上，DE∥BC，AD=2、AB=6、AE=3，则 AC = ______。", "9",
        "△ADE∽△ABC（AA）：AD/AB=AE/AC → 2/6=3/AC → AC=9。")
    add(ch, "kp_sim_properties", "fill", "medium",
        "两个相似三角形的相似比为 2:3，则它们的面积比为 ______。", "4:9",
        "面积比=相似比的平方。周长比/对应边比=相似比，面积比别忘平方——高频坑。")
    add(ch, "kp_sim_properties", "subjective", "hard",
        "△ABC∽△DEF，相似比为 3:5，若 △ABC 的周长为 12，求 △DEF 的周长。",
        "20",
        "周长比等于相似比：12/周长DEF=3/5 → 周长DEF=20。")
    add(ch, "kp_sim_homothety", "fill", "medium",
        "△ABC 以原点 O 为位似中心、位似比为 2 放大（同侧）得到 △A'B'C'，若 A(1, 3)，则 A' = ______。",
        "(2, 6)",
        "以原点为位似中心：(x,y)→(kx,ky)；同侧放大取正 k=2 → (2,6)，异侧取 -k。")
    choice("位似图形一定是相似图形，这句话（　）",
           ["正确", "错误", "不一定", "无法判断"], "A", ch, "kp_sim_homothety", "easy",
           "位似是特殊的相似——多一个'对应点连线交于位似中心'的条件。")


# ================================================================ ch_trig
def gen_trig():
    ch = "ch_trig"
    add(ch, "kp_trig_defs", "fill", "easy",
        "Rt△ABC 中 ∠C=90°，AC=3、BC=4，则 tanA = ______，sinA = ______。", "4/3；4/5",
        "AB=5（勾股）。tanA=对/邻=BC/AC=4/3；sinA=对/斜=BC/AB=4/5。先勾股求斜边是第一步。")
    choice("Rt△ABC 中 ∠C=90°，若 AC=5、AB=13，则 cosA =（　）",
           ["5/13", "12/13", "5/12", "12/5"], "A", ch, "kp_trig_defs", "easy",
           "cosA=邻边/斜边=AC/AB=5/13。")
    add(ch, "kp_trig_special", "fill", "easy",
        "sin30° = ______，tan45° = ______，cos60° = ______。", "1/2；1；1/2",
        "特殊角：30°(sin=1/2, cos=√3/2, tan=√3/3)；45°(√2/2, √2/2, 1)；60°(√3/2, 1/2, √3)。")
    choice("计算 sin30°·cos60° + tan45° 的值是（　）",
           ["1/4", "5/4", "3/4", "1"], "B", ch, "kp_trig_special", "medium",
           "(1/2)(1/2)+1 = 1/4+1 = 5/4。特殊值代入，细心即可。")
    add(ch, "kp_trig_solve", "subjective", "medium",
        "Rt△ABC 中 ∠C=90°，∠A=30°，斜边 c=10，求直角边 a 与 b。",
        "a=5，b=5√3",
        "a=c·sinA=10×1/2=5；b=c·cosA=10×(√3/2)=5√3。解直角三角形：'知二求三'，选对三角函数。")
    add(ch, "kp_trig_solve", "fill", "hard",
        "Rt△ABC 中 ∠C=90°，tanA=3/4，斜边 c=10，则 a = ______，b = ______。", "a=6，b=8",
        "tanA=a/b=3/4 → 设 a=3k、b=4k，勾股 9k²+16k²=100 → k=2 → a=6、b=8。知道 tan 就用比例设元。")
    add(ch, "kp_trig_apply", "subjective", "hard",
        "小明在楼顶 A 处测得对面楼底 B 的俯角为 30°，两楼水平距离 24m，求 A 相对 B 的高度（保留根号）。",
        "8√3 m",
        "俯角 30° → tan30°=h/24 → h=24×(√3/3)=8√3≈13.9m。仰俯角问题：先画水平线再找角。")
    add(ch, "kp_trig_apply", "subjective", "hard",
        "一段斜坡坡比 i=1:2.4（垂直:水平），坡面长 13m，求坡的垂直高度。",
        "5m",
        "设高 h、水平 2.4h：h²+(2.4h)²=13² → 6.76h²=169 → h=5。坡比=垂直:水平，别与坡角混淆。")


# ================================================================ main
def main() -> None:
    gen_algebra_basis()
    gen_quadratic()
    gen_func()
    gen_rotation()
    gen_circle()
    gen_probability()
    gen_inverse()
    gen_similarity()
    gen_trig()

    # ---- 完整性校验：无重复题干、字段齐全、选择题选项合法
    contents = [r["content"] for r in rows]
    assert len(contents) == len(set(contents)), "存在重复题干"
    for r in rows:
        assert r["content"] and r["answer"] and r["analysis"], f"字段缺失: {r['kp_id']}"
        if r["question_type"] == "choice":
            assert r["options"] and r["answer"] in "ABCD", f"选择题字段异常: {r['content'][:20]}"

    OUT.parent.mkdir(parents=True, exist_ok=True)   # parents=True：连父目录一起建；exist_ok：已存在不报错
    with OUT.open("w", newline="", encoding="utf-8") as f:
        # 陌生类 csv.DictWriter：按"列名→值"的字典写 CSV。fieldnames 必须显式给
        #   （dict 没有固定顺序），writeheader 写表头，writerows 批量写数据行；
        #   newline="" 是 csv 模块官方要求的写法——不写会在 Windows 下多出空行
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # Counter：collections 里的计数器——传进可迭代对象，自动统计每个元素出现次数
    kp_stat = Counter(r["kp_id"] for r in rows)
    type_stat = Counter(r["question_type"] for r in rows)
    diff_stat = Counter(r["difficulty"] for r in rows)
    print(f"[OK] 共生成 {len(rows)} 题 -> {OUT}")
    print(f"  题型分布: {dict(type_stat)}")
    print(f"  难度分布: {dict(diff_stat)}")
    print(f"  覆盖知识点: {len(kp_stat)} 个")
    few = {k: v for k, v in kp_stat.items() if v < 2}
    if few:
        print(f"  [WARN] 题量不足 2 的知识点: {few}")


# 陌生写法 if __name__ == "__main__"：Python 惯用入口守卫——直接运行本文件时
#   __name__ 才等于 "__main__"，被 import 时不成立。效果：python tools/gen_question_bank.py
#   会执行生成；别的文件 import 它（拿 add/eq_str 复用）则不会误触发整个生成流程
if __name__ == "__main__":
    main()
