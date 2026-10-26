"""结构化输出适配层的回归测试（app/core/structured.py）。

钉住的坑（2026-10-25 老大问"意图识别准确率太低"时挖出来的）：
    with_structured_output() 默认走 response_format=json_schema，
    **DeepSeek 的 OpenAI 兼容层不支持**，每次调用都 400：

        Error code: 400 - This response_format type is unavailable now

    而调用方的写法是 try/except → 异常被吃掉 → 静默降级成关键词兜底。
    ★ 最阴的是"不报错"：整条链路照跑，接口照返回 200，
      只是意图分类悄悄变成了 52.9% 准确率的关键词规则，
      而且日志里什么异常都看不到（except 里没 print）。

    实测影响面：三个节点全部中招
      - classify_intent   （意图分类）→ 退化到 52.9% 关键词匹配
      - llm_grade_subjective（主观题批改）→ 一直显示"LLM 批改服务暂不可用"
      - error_attribution （错因归因）→ 一直退默认错因"方法不会"
"""

import json

import pytest
from pydantic import BaseModel, Field

from app.core.structured import _parse_loose_json, invoke_structured


class _Demo(BaseModel):
    intent: str = Field(description="意图名")
    score: int = Field(ge=0, le=100, description="分数")


class _FakeLLM:
    """假 LLM：记录收到的 prompt，按脚本返回不同形态的输出。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def with_structured_output(self, schema, method=None):
        self.calls.append(("structured", method))
        return self

    def invoke(self, messages, **kwargs):
        self.calls.append(("invoke", messages))
        if not self.outputs:
            raise RuntimeError("no more outputs")
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        # 已经校验好的 Pydantic 实例原样返回（模拟 provider 原生结构化成功）
        if isinstance(out, BaseModel):
            return out
        return _FakeResp(out)


class _FakeResp:
    def __init__(self, content):
        self.content = content


# =====================================================================
# JSON 抠取：模型的各种脏输出都要能救回来
# =====================================================================
def test_parse_plain_json():
    assert _parse_loose_json('{"intent": "explain"}') == {"intent": "explain"}


def test_parse_json_with_code_fence():
    """模型爱加 ```json 围栏。"""
    assert _parse_loose_json('```json\n{"intent": "explain"}\n```') == {"intent": "explain"}


def test_parse_json_with_surrounding_text():
    """前后有解释文字，截取最外层 {}。"""
    got = _parse_loose_json('好的，结果是：{"intent": "explain"} 希望有帮助')
    assert got == {"intent": "explain"}


def test_parse_garbage_returns_none():
    assert _parse_loose_json("完全不是 JSON") is None
    assert _parse_loose_json("") is None
    assert _parse_loose_json(None) is None


# =====================================================================
# 降级链：原生能力不可用时，必须自己把 JSON 抠出来
# =====================================================================
def test_falls_back_to_manual_json_when_provider_rejects_schema():
    """★ 核心场景：provider 不支持 json_schema（DeepSeek 就是这样）。

    预期：前两次原生尝试都失败，第三次手写 JSON 约束成功拿到结果。
    """
    llm = _FakeLLM([
        RuntimeError("This response_format type is unavailable now"),  # json_mode
        RuntimeError("Thinking mode does not support this tool_choice"),  # function_calling
        '{"intent": "explain", "score": 80}',                            # 手写约束后的普通输出
    ])
    out = invoke_structured(llm, _Demo, [("human", "这题考什么")])
    assert out.intent == "explain"
    assert out.score == 80


def test_uses_native_json_mode_when_available():
    """provider 支持时走原生路径，不再自己抠 JSON。"""
    llm = _FakeLLM([_Demo(intent="explain", score=80)])
    out = invoke_structured(llm, _Demo, [("human", "x")])
    assert out.intent == "explain"
    # 第一次尝试就是 json_mode
    assert llm.calls[0] == ("structured", "json_mode")


def test_falls_back_to_function_calling_when_json_mode_fails():
    """json_mode 不支持但 function_calling 支持时，走第二条路。"""
    llm = _FakeLLM([
        RuntimeError("json_object not supported"),
        _Demo(intent="guide_scaffold", score=60),
    ])
    out = invoke_structured(llm, _Demo, [("human", "x")])
    assert out.intent == "guide_scaffold"
    methods = [c[1] for c in llm.calls if c[0] == "structured"]
    assert methods == ["json_mode", "function_calling"]


def test_raises_when_all_paths_fail():
    """三条路都失败 → 抛异常（由调用方决定降级），不能静默返回垃圾。"""
    llm = _FakeLLM([
        RuntimeError("a"),
        RuntimeError("b"),
        RuntimeError("c"),
    ])
    with pytest.raises(Exception):
        invoke_structured(llm, _Demo, [("human", "x")])


def test_schema_violation_raises():
    """模型输出不符合 schema → 抛异常，不要半吊子放行。"""
    llm = _FakeLLM([
        RuntimeError("a"),
        RuntimeError("b"),
        '{"intent": "explain", "score": 999}',   # score 超过 le=100
    ])
    with pytest.raises(Exception):
        invoke_structured(llm, _Demo, [("human", "x")])


# =====================================================================
# ★ 回归钉子：项目里不允许再出现裸的 with_structured_output
# =====================================================================
def test_no_bare_with_structured_output_in_project():
    """★ 防复发：任何节点都不许直接调 with_structured_output。

    必须走 invoke_structured（自带 provider 兼容降级），
    否则 DeepSeek 环境下会再次静默退化到关键词匹配。

    用 AST 精确判断"真的调用了"，不靠字符串匹配（docstring 里提到这个词不算）。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for py in (root / "app").rglob("*.py"):
        # 排除适配层自身（里面必须用原生 API）
        if py.name == "structured.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name == "with_structured_output":
                offenders.append(f"{py.relative_to(root)}:{node.lineno}")

    assert not offenders, "发现裸调 with_structured_output（会静默降级）：\n" + "\n".join(offenders)


# =====================================================================
# 嵌套 schema 展开（2026-10-26 补题库时踩到）
# =====================================================================
def test_nested_schema_is_expanded_in_fallback_prompt():
    """★ 兜底路径的 prompt 必须展开嵌套模型的内层字段。

    踩坑现场：给知识点批量出题时定义了
        class GeneratedBatch(BaseModel): questions: list[GeneratedQuestion]
    兜底 prompt 原来只列顶层字段（questions），模型根本看不到
    GeneratedQuestion 里有什么字段，于是自己编了个 question 代替 content，
    pydantic 校验直接失败，一整批题全丢。
    """
    from pydantic import BaseModel, Field

    from app.core.structured import _model_json_sample

    class _Inner(BaseModel):
        content: str = Field(description="题干")
        answer: str = Field(description="答案")

    class _Outer(BaseModel):
        questions: list[_Inner] = Field(description="题目列表")

    text = _model_json_sample(_Outer)

    assert '"questions": [' in text, "列表字段没被展开成数组"
    assert '"content"' in text and '"answer"' in text, "内层字段名必须出现在提示里"
    assert "题干" in text, "内层字段的说明（description）也要带出来"


def test_flat_schema_prompt_unchanged():
    """扁平 schema（意图分类/批改/归因）的提示格式不能被改坏。"""
    from pydantic import BaseModel, Field

    from app.core.structured import _model_json_sample

    class _Flat(BaseModel):
        intent: str = Field(description="五类之一")

    text = _model_json_sample(_Flat)

    assert '"intent"' in text
    assert "五类之一" in text
