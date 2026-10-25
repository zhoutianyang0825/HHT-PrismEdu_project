"""学生端 SSE 流式包装的回归测试。

钉住的坑（2026-10-25 线上实际踩到）：
    LangGraph 的 stream_mode="updates" 在节点返回 {} 或 None 时，吐出来的 chunk 是
    {"节点名": None} —— 也就是说 chunk.values() 拿到的 diff 可能是 None。
    respond 节点在"讲解终稿已产出"这条路径上正是 return {}，
    于是流式包装里那句裸的 diff.get("messages") 会炸：
        AttributeError: 'NoneType' object has no attribute 'get'
    前端只会看到一条 type=error 帧，讲解正文发到一半就断。

测试做法：用假图复刻真实的 chunk 形状（重点是 {'respond': None}），断言流里不出现 error 帧。
"""

from langchain_core.messages import AIMessage

from app.api.student import _stream_graph_events


class _FakeGraph:
    """假图：按预设 chunk 列表产出 stream 事件（只实现包装器用到的那一个方法）。"""

    def __init__(self, chunks):
        self.chunks = chunks

    def stream(self, graph_input, config, stream_mode=None):
        for c in self.chunks:
            yield c


def _frames(chunks):
    """跑一遍流式包装，返回所有 SSE 帧字符串。"""
    graph = _FakeGraph(chunks)
    config = {"configurable": {"thread_id": "t1"}}
    return list(_stream_graph_events(graph, {"messages": []}, config))


def test_respond_none_does_not_break_stream():
    """讲解路径：explain_concept 出了正文，respond 返回 {} → chunk 是 {'respond': None}，不能炸。"""
    frames = _frames([
        {"safety_check": None},
        {"classify_intent": {"intent": "explain"}},
        {"explain_concept": {"messages": [AIMessage(content="讲解正文")]}},
        {"respond": None},
    ])
    assert not any('"type": "error"' in f for f in frames), frames
    assert any("讲解正文" in f for f in frames), frames
    assert any('"type": "end"' in f for f in frames), frames


def test_respond_with_message_yields_message_frame():
    """判分路径：respond 正常返回 messages → 照旧发一条 message 帧。"""
    frames = _frames([
        {"grade_submit": {"is_correct": True}},
        {"respond": {"messages": [AIMessage(content="✓ 正确！")]}},
    ])
    assert not any('"type": "error"' in f for f in frames), frames
    assert any("✓ 正确！" in f for f in frames), frames
    assert any('"type": "end"' in f for f in frames), frames


def test_node_returning_none_only_yields_node_frame():
    """普通节点返回 None（很常见）→ 只发进度帧，不报错。"""
    frames = _frames([
        {"safety_check": None},
        {"classify_intent": {"intent": "off_topic"}},
    ])
    assert not any('"type": "error"' in f for f in frames), frames
    assert any('"node": "safety_check"' in f for f in frames), frames
