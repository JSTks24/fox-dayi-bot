"""AI 响应解析 —— 纯函数，零状态、零外部依赖。"""

import ast
import json
from typing import Any


def stringify_ai_content(content: Any) -> str:
    """把 OpenAI/Gemini 返回的 content 尽量转成可读文本。"""
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                part_text = getattr(part, "text", None)
                if part_text is not None:
                    parts.append(str(part_text))

        joined = "\n".join([p for p in parts if p]).strip()
        return joined if joined else str(content)

    return str(content)


def parse_ai_json_response(content: Any) -> dict[str, Any] | None:
    """尽可能稳健地解析 AI 返回的 JSON 文本。"""
    raw_text = stringify_ai_content(content).strip()

    if not raw_text:
        return None

    candidates = [raw_text]

    # 兼容 ```json ... ``` 包裹
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        if len(lines) >= 2:
            inner_lines = lines[1:]
            if inner_lines and inner_lines[-1].strip().startswith("```"):
                inner_lines = inner_lines[:-1]
            stripped = "\n".join(inner_lines).strip()
            if stripped:
                candidates.append(stripped)

    # 尝试从文本中抽取最外层 JSON 对象
    for text in list(candidates):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            extracted = text[start:end + 1].strip()
            if extracted and extracted not in candidates:
                candidates.append(extracted)

    for text in candidates:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"results": parsed}
        except json.JSONDecodeError:
            pass

        # 兼容 AI 返回 Python 风格字典（单引号）
        try:
            parsed_py = ast.literal_eval(text)
            if isinstance(parsed_py, dict):
                return parsed_py
            if isinstance(parsed_py, list):
                return {"results": parsed_py}
        except (SyntaxError, ValueError):
            pass

    preview = raw_text[:500].replace("\n", "\\n")
    print(f"⚠️ [Unanswered] AI 返回内容无法解析为 JSON，预览: {preview}")
    return None
