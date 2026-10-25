# HHT棱镜课堂 / PrismEdu · 问题定位案例（SSE 流中途崩流）

- **案例编号**：CASE-001
- **发生日期**：2026-10-25
- **涉及文件**：`app/api/student.py::_stream_graph_events`、`app/graphs/student/nodes.py::respond`
- **一句话结论**：LangGraph 的 `stream_mode="updates"` 在**节点返回 `{}` 或 `None`** 时，吐出来的 chunk 是 `{"节点名": None}`；流式包装里对解包出来的 `diff` 直接 `.get()`，于是 `AttributeError: 'NoneType' object has no attribute 'get'`——**正文已经发出去一半，流却断了**。

## 一、现象

- 接口：`POST /api/student/answer/stream`（SSE）
- 前端看到的帧序列：

```
data: {"type": "node", "node": "safety_check"}
data: {"type": "node", "node": "classify_intent"}
data: {"type": "message", "content": "教材库暂无相关片段。下面按配方法通法讲解：…"}   ← 讲解正文已发出
data: {"type": "error", "message": "AttributeError: 'NoneType' object has no attribute 'get'"}
```

- **症状指纹**：**"讲解正文发到一半，流断了"**——这是"崩在出口节点"的典型signature（message 帧已 yield，随后处理下一个 chunk 时炸）。
- **重要提醒**：SSE 的 error 帧**只有异常类型 + 消息，没有堆栈**。真正的 traceback 在**后端终端**（uvicorn 打印）。

## 二、定位三步法（可复用到任何"SSE/流式接口报错"）

### 第 1 步：看后端终端的 traceback

- SSE 帧里没有堆栈，**完整堆栈只在 uvicorn 那一侧**。这一步决定后面所有定位的方向。
- 没有堆栈时的次优手段：先按"最可能出错的行"读流式包装代码，找**没做 None 保护的 `.get()`**。

### 第 2 步：找没做保护的 `.get()`

本例全局只有一处是裸的（其余分支都有 `isinstance(diff, dict)` 保护）：

```python
node_name, diff = next(iter(chunk.items()))     # diff 可能是 None
if node_name == "respond" and diff.get("messages"):   # ← 裸 .get()，炸在这里
```

### 第 3 步：写最小实验复现数据形态（关键，别靠猜）

用一个小 LangGraph 脚本，把"节点可能返回什么"穷举一遍，打印每种情况下 chunk 的形状：

```python
from typing import TypedDict
from langgraph.graph import END, START, StateGraph

class S(TypedDict):
    messages: list

def n_empty(state: S) -> dict: return {}      # 空 dict
def n_none(state: S) -> dict:  return None     # None
def n_msg(state: S) -> dict:   return {"messages": ["hi"]}

g = StateGraph(S)
g.add_node("empty", n_empty); g.add_node("none", n_none); g.add_node("msg", n_msg)
g.add_edge(START, "empty"); g.add_edge("empty", "none")
g.add_edge("none", "msg");   g.add_edge("msg", END)

for chunk in g.compile().stream({"messages": []}, stream_mode="updates"):
    print(chunk, "| diff 是 None 吗:", next(iter(chunk.values())) is None)
```

**实测输出**：

```
{'empty': None} | diff 是 None 吗: True      ← 节点 return {} 时，diff 就是 None
{'none': None}  | diff 是 None 吗: True      ← return None 同理
{'msg': {'messages': ['hi']}} | diff 是 None 吗: False
```

**结论**：节点返回 `{}` 或 `None` → chunk 是 `{"节点名": None}` → `diff` 是 `None` → 裸 `.get()` 必炸。

## 三、根因：两个知识点叠加

### 知识点 1｜LangGraph `interrupt` 是"挂起"，不是"走完"

`interrupt()` 会让**节点函数栈停在那一行**，后面的 `return` 不执行，图的状态停在当前节点等人 resume。只有节点**正常 return** 时，图才沿边继续往下走。

所以在引导链路上，`respond` **不是每轮都会执行**：

```
t1  guide_scaffold: attempt_count=0 < 3 → 生成本层提示
t2  ★ interrupt(...) → 函数栈停在这一行，图停在 guide_scaffold
    SSE 发 interrupt 帧，本次请求结束 → respond 根本没执行
t3  学生作答 → Command(resume=…) → 从 interrupt 那行继续
    → return {messages:[HumanMessage(学生回答)], attempt_count:0+1=1}
t4  route_after_guide：末条是 HumanMessage → 回 classify_intent 重新分类
t5  第2轮 attempt_count=1 < 3 → 又 interrupt → resume 后 =2
t6  第3轮 attempt_count=2 < 3 → 又 interrupt → resume 后 =3
t7  第4次进入 guide_scaffold: attempt_count=3 >= MAX_ATTEMPTS(3) ★走"满轮"分支
    → LLM 出完整讲解 → return {messages:[AIMessage(讲解终稿)]}   ← 这次是正常 return
t8  route_after_guide：末条是 AIMessage → 走 respond
    → respond 见 messages[-1] 已是 AIMessage → return {} → chunk={'respond':None} → 崩在这里
```

> 结论：**前 3 轮 respond 一次都没跑过**；只有"终稿已产出"那一刻（讲解路径 / 引导第 4 次进入出终稿）才第一次走到 respond，而它 `return {}`（不重复发话）正是崩点。

### 知识点 2｜`updates` 模式会把"空更新"翻译成 `None`

节点返回 `{}` 时，本质是"这一轮没有状态变化"，LangGraph 在 `updates` 模式里就用 `None` 表示 → `{"respond": None}`。

## 四、修复

```python
# 修复前（裸 .get()，diff 为 None 时 AttributeError）
if node_name == "respond" and diff.get("messages"):

# 修复后（先判类型，None/空则不发帧、继续下一个 chunk）
if node_name == "respond":
    if not isinstance(diff, dict) or not diff.get("messages"):
        continue
    last = diff["messages"][-1]
    content = last.content if isinstance(last, AIMessage) else str(last)
    yield _sse({"type": "message", "content": content})
```

**为什么 `continue` 而不是补一个兜底消息**：respond 是出口节点，本轮没有增量就是没有增量，不该凭空造一条消息；不发帧、前端自然等下一条即可。

## 五、回归测试（`tests/test_student_stream.py`，3 条）

用假图复刻真实 chunk 形状，钉住三条行为：

| 用例 | 断言 |
|---|---|
| `{'respond': None}` | 流里**不出现 error 帧**、讲解正文照发、end 帧照发 |
| `{'respond': {'messages': [AIMessage]}}` | 照旧发一条 message 帧 |
| 普通节点返回 `None` | 只发 node 进度帧，不报错 |

```python
class _FakeGraph:
    def __init__(self, chunks): self.chunks = chunks
    def stream(self, graph_input, config, stream_mode=None):
        for c in self.chunks: yield c
```

## 六、可复用的通用规则

1. **流式接口报错，先找后端终端的 traceback**——前端只给异常类型和消息，堆栈不在浏览器里。
2. **遍历 LangGraph `updates` chunk 时，`diff` 可能是 `None`**：节点返回 `{}`/`None` 就是 `{"节点名": None}`。一律先 `isinstance(diff, dict)` 再 `.get()`。
3. **症状指纹比错误文案更有用**："正文发了一半就断" ⇒ 崩在出口/收尾节点 ⇒ 先怀疑最后一个 chunk 的解包。
4. **别靠猜，写最小实验复现数据形态**——20 行小脚本打印每个 chunk 的形状，比读 200 行源码更快定案。
5. **补一条回归测试**，把"chunk 形状 → 期望行为"钉死，这类坑才不会第二次出现。

## 七、对外讲解时的讲法（可直接照搬）

- **类比**：`interrupt` 像银行叫号——柜员把单子递出来说"填完再回来"，流程停在这一步不往下走；只有"办完了正常出结果"才走到下一关（respond）。
- **一句话**："空更新在 `updates` 模式里会被翻译成 `None`，代码里对解包结果裸调 `.get()`，就炸了。"
- **现场演示**：跑第 2 步那段 20 行脚本，三行输出把机制讲透（`{'empty': None}` 那行最关键）。
- **收尾**：凡是解包 LangGraph chunk 的地方，先判类型再取值——这是一条能带走的通用规则。
