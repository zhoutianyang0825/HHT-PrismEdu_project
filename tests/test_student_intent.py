"""学生端意图路由的回归测试（五类意图 + 引导满轮转讲解）。

钉住的坑（2026-10-25 老大加 guide_scaffold 意图时踩到）：
    新增一类意图不是只改 INTENTS 常量——它要同时出现在 5 个地方，
    漏任何一处都会出问题，且**不一定报错**：

      ① app/graphs/student/nodes.py   INTENTS 常量（LLM 认得的合法值集合）
      ② app/graphs/student/nodes.py   分类 system prompt 里的意图名
         ★ 写错字（如 guide__scaffold 双下划线）→ LLM 返回的串不在 INTENTS 里
           → 被判非法 → 静默落进关键词兜底 → 意图整个失效，不报错！
      ③ app/graphs/student/nodes.py   _fallback_intent 关键词兜底
         （LLM 挂了/超时时靠它；漏了 → "这题考什么" 会落 off_topic）
      ④ app/graphs/student/graph.py   route_by_intent 路由表
         ★ 漏了 → KeyError，图跑到那一步直接崩
      ⑤ app/graphs/student/state.py   intent 的 Literal 类型标注

    另一个语义坑：guide_scaffold 和 explain 的判定顺序。
        "我没有思路" 同时含"思路"和"没有"，若 explain 先判，
        会被当成"要完整讲解"直接跳过引导给答案——正好和产品意图相反。
"""

from langchain_core.messages import AIMessage, HumanMessage

from app.graphs.student.graph import route_after_guide, route_by_intent
from app.graphs.student.nodes import INTENTS, _fallback_intent, classify_intent


def test_prompt_intent_names_match_intents_constant():
    """★ 死因钉子：分类 prompt 里出现的意图名，必须和 INTENTS 完全一致。

    老大第一次加 guide_scaffold 时，prompt 里写成了 guide__scaffold（双下划线），
    LLM 返回的串不在 INTENTS 里 → 被判非法 → 静默落进关键词兜底，
    新意图等于没生效，而且**一行报错都没有**。
    """
    from unittest.mock import patch

    class _FakeResult:
        intent = "guide_scaffold"
        confidence = 0.9

    captured = {}

    def fake_llm(temperature=0.7, streaming=False):
        class _W:
            def with_structured_output(self, _schema):
                return self

            def invoke(self, msgs):
                captured["system"] = msgs[0][1]
                return _FakeResult()

        return _W()

    with patch("app.graphs.student.nodes.get_llm", fake_llm):
        classify_intent({
            "student_id": 1,
            "question_id": 12,
            "messages": [HumanMessage(content="这题考什么")],
        })

    system = captured["system"]
    for intent in INTENTS:
        assert intent in system, f"prompt 里缺意图名 {intent}"
    # 反向：prompt 里出现的 "xxx=..." 形式的意图名，必须都在 INTENTS 里
    import re
    for name in re.findall(r"([a-z_]+)\s*=", system):
        if name in ("ask_answer", "guide_scaffold", "submit_answer", "explain", "off_topic"):
            assert name in INTENTS, f"prompt 里的意图名 {name} 不在 INTENTS 里"


def test_route_table_covers_all_intents():
    """★ 路由表必须覆盖 INTENTS 里每一个意图，漏一个就 KeyError。"""
    for intent in INTENTS:
        node = route_by_intent({"intent": intent})
        assert node, f"{intent} 没有路由目标"


def test_intents_has_five_classes():
    """意图是五类：求答案/要引导/提交作答/要讲解/闲聊。"""
    assert set(INTENTS) == {
        "ask_answer", "guide_scaffold", "submit_answer", "explain", "off_topic",
    }


def test_guide_scaffold_intent_routes_to_guide_node():
    """「这题考什么」类输入 → 必须进引导节点，不是直接给讲解。"""
    assert route_by_intent({"intent": "guide_scaffold"}) == "guide_scaffold"


def test_explain_intent_routes_to_explain_node():
    """明确要完整讲解 → 进讲解节点。"""
    assert route_by_intent({"intent": "explain"}) == "explain_concept"


def test_ask_answer_also_routes_to_guide():
    """「这题答案是多少」→ 依然先引导不给答案（引导式教学底线）。"""
    assert route_by_intent({"intent": "ask_answer"}) == "guide_scaffold"


def test_fallback_covers_guide_phrases():
    """LLM 挂了时的关键词兜底：这些说法必须落 guide_scaffold。"""
    for text in ["这题考什么", "没有思路", "我没思路", "给我点提示", "给我点思路",
                 "不会做", "从哪下手", "没头绪"]:
        assert _fallback_intent(text) == "guide_scaffold", f"{text} 兜底判错了"


def test_fallback_guide_beats_explain():
    """★ 顺序坑：guide_scaffold 必须排在 explain 前面。

    "我没有思路" 含"思路"，若 explain 先判就会直接给完整答案，
    跳过引导——与"先触发引导"的产品意图相反。
    """
    assert _fallback_intent("我没有思路") == "guide_scaffold"
    assert _fallback_intent("思路是什么") == "guide_scaffold"


def test_fallback_still_distinguishes_explain():
    """明确要完整讲解的措辞仍要落 explain。"""
    for text in ["讲解一下", "详细讲讲", "为什么这么做", "解析一下"]:
        assert _fallback_intent(text) == "explain", f"{text} 兜底判错了"


def test_fallback_unchanged_other_classes():
    """其余三类兜底不受影响。"""
    assert _fallback_intent("这题答案是多少") == "ask_answer"
    assert _fallback_intent("x^2-6x+5=0") == "submit_answer"
    assert _fallback_intent("你好") == "off_topic"


def test_route_after_guide_force_explain_goes_to_explain_node():
    """★ 引导满 3 轮 → 必须转 explain_concept（给带 RAG 出处的完整解答）。

    不能回 classify_intent：同一句"还是不会"会被再次分类成 guide_scaffold
    → 再进引导 → 又满轮 → 无限循环烧 token。
    """
    assert route_after_guide({"force_explain": True, "messages": []}) == "explain_concept"


def test_route_after_guide_message_types():
    """另外两条分支：末条 AIMessage→respond；末条 HumanMessage→重新分类。"""
    assert route_after_guide({"messages": [AIMessage(content="讲解")]}) == "respond"
    assert route_after_guide({"messages": [HumanMessage(content="我不会")]}) == "classify_intent"
