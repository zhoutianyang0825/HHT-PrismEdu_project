"""结构化输出适配层：让 DeepSeek / 通义 / OpenAI 都能稳定吐 Pydantic 结构。

★ 为什么要这一层：
    LangChain 的 with_structured_output() 默认走 json_schema（response_format），
    但 DeepSeek 的 OpenAI 兼容层不支持这种 response_format：

        Error code: 400 - This response_format type is unavailable now

    → 每次调用都抛异常 → 上层 except 兜底 → 静默降级成关键词匹配。
      ★ 最阴的是"不报错"：异常被 except 吃掉，链路照跑，
        只是意图分类悄悄变成了 52.9% 准确率的关键词规则。


    本模块的策略：按可用性从强到弱依次尝试，拿到第一个能用的就返回：
      ① method="json_mode"      —— provider 支持 response_format=json_object 时
      ② method="function_calling" —— provider 支持 tool_choice 时
      ③ 手写 JSON 约束 + 本地解析  —— 兜底：只用普通 chat 接口，
         prompt 里写死 JSON 格式，拿到文本自己 json.loads + 校验

    三条路都产出同一个 Pydantic 实例，上层调用方无感知。
"""

import json
import re
from typing import Type, TypeVar, get_args, get_origin

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def _json_sample(annotation, depth: int = 0) -> str | None:
    """给一个类型注解生成 JSON 示例片段；认不出的类型返回 None。

    得到的数据：字段的类型注解，如 list[GeneratedQuestion] / str / str | None
    要做的操作：
        list[X]  → 递归 X，外面套 []
        嵌套 Pydantic 模型 → 展开成对象
        其它（str/int/bool/Union/None…）→ 返回 None，由调用方用字符串占位
    返回：JSON 片段字符串，或 None

    为什么需要它：
        兜底路径的 prompt 原来只列了顶层字段名，嵌套模型（如 {"questions": [生成题]}）
        的内层字段模型根本看不到 —— 它会自己编字段名（把 content 写成 question），
        于是 pydantic 校验直接失败。展开嵌套结构后它才知道该叫什么。
    """
    if depth > 4:                      # 防御自引用模型，别无限递归
        return None

    origin = get_origin(annotation)
    if origin in (list, tuple):
        args = get_args(annotation)
        if not args:
            return None
        inner = _json_sample(args[0], depth + 1)
        return f"[{inner}]" if inner is not None else None

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _model_json_sample(annotation, depth + 1)

    return None


def _model_json_sample(model: Type[BaseModel], depth: int = 0) -> str:
    """把一个 Pydantic 模型展开成多行 JSON 示例（含嵌套字段）。"""
    pad = "  " * depth
    pad_inner = "  " * (depth + 1)
    lines = []

    for name, field in model.model_fields.items():
        sample = _json_sample(field.annotation, depth + 1)
        if sample is None:
            desc = (field.description or "值").replace("\n", " ")
            sample = json.dumps(f"<{desc}>", ensure_ascii=False)
        lines.append(f'{pad_inner}"{name}": {sample}')

    return "{\n" + ",\n".join(lines) + f"\n{pad}}}"


def _parse_loose_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON 对象。

    要做的操作：
      1. 去掉 ```json ... ``` 代码围栏
      2. 直接 json.loads
      3. 失败则找第一个 { 到最后一个 } 的片段再试
    返回：解析出的 dict；失败返回 None
    """
    if not text:
        return None

    t = text.strip()
    # 去掉代码围栏
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)

    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # 退一步：截取最外层 {...}
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(t[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def invoke_structured(
    llm,
    schema: Type[T],
    messages: list[tuple[str, str]],
    temperature: float | None = None,
) -> T:
    """让 LLM 按 Pydantic schema 输出结构化结果（多策略自动降级）。

    得到的数据：llm（ChatModel 实例）、schema（目标 Pydantic 类）、
                messages（原始提示消息列表）

    要做的操作：
      1. 先试 provider 原生的结构化能力（json_mode → function_calling）
      2. 都不行 → 走"手写 JSON 约束 + 本地解析"兜底
      3. 三条路都失败 → 抛异常（由调用方决定怎么降级）

    返回：schema 的实例

    为什么最后兜底也要自己解析：
        provider 不支持 → 整条链路会静默降级，静默降级会导致意图识别准确率骤降，所以不许静默降级，。
    """
    # ---- 路径 1/2：provider 原生结构化 ----
    for method in ("json_mode", "function_calling"):
        try:
            return llm.with_structured_output(schema, method=method).invoke(messages)
        except Exception:
            continue


    # ---- 路径 3：手写 JSON 约束 + 本地解析 ----
    # 用 _model_json_sample 展开嵌套模型：只列顶层字段名不够，
    # 内层模型（如 questions 数组里的元素）看不到字段名就会自己乱编
    fields = _model_json_sample(schema)

    strict_msgs = [
        ("system",
         "你是一个只输出 JSON 的接口。**只输出一个 JSON 对象**，不要输出任何解释、markdown 代码围栏或者其它文字。\n"
         "★ 字段名必须严格照抄下面的写法，不要改名、不要用同义词替换、不要增删字段。\n"
         f"JSON 字段格式：\n{fields}"),
        *messages,
    ]
    kwargs = {"temperature": temperature} if temperature is not None else {}
    # 解析关键字参数 **kwargs
    resp = llm.invoke(strict_msgs, **kwargs)

    raw = resp.content if hasattr(resp, "content") else str(resp)
    obj = _parse_loose_json(raw)

    if obj is None:
        raise ValueError(f"模型输出无法解析为 JSON：{str(raw)[:200]}")

    try:
        return schema.model_validate(obj)
    
    except ValidationError as e:
        raise ValueError(f"模型输出不符合 schema：{obj}；{e}") from e
