"""Display only top-level user-facing strings, never raw JSON or reasoning tokens."""

import json

from .store import AgentError


def public_text(partial):
    decoder = json.JSONDecoder()
    i = 0
    found = {}

    def space(pos):
        while pos < len(partial) and partial[pos].isspace():
            pos += 1
        return pos

    i = space(i)
    if i >= len(partial) or partial[i] != "{":
        return found
    i += 1
    while i < len(partial):
        i = space(i)
        try:
            key, i = decoder.raw_decode(partial, i)
            i = space(i)
            if partial[i] != ":":
                return found
            i = space(i + 1)
            if key in {"summary", "answer"} and partial[i] == '"':
                # Decode complete characters, including split JSON escapes; keep incomplete escapes hidden.
                start = i
                try:
                    value, end = decoder.raw_decode(partial, start)
                except ValueError:
                    tail = partial[start + 1 :]
                    for cut in range(len(tail), max(-1, len(tail) - 7), -1):
                        try:
                            value = json.loads('"' + tail[:cut] + '"')
                            found[key] = value.encode("utf-8", "ignore").decode("utf-8")
                            return found
                        except ValueError:
                            continue
                    return found
                found[key] = value.encode("utf-8", "ignore").decode("utf-8")
                i = end
            else:
                _, i = decoder.raw_decode(partial, i)
            i = space(i)
            if i >= len(partial) or partial[i] != ",":
                return found
            i += 1
        except (ValueError, IndexError, TypeError):
            return found
    return found


def collect_stream(lines, on_preview):
    content, request_id, usage, size, done, finish = "", "", {}, 0, False, None
    for line in lines:
        size += len(line)
        if size > 2_000_000:
            raise AgentError("PROVIDER_OUTPUT_INVALID", "模型回复过长，本轮已停止", 502)
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        try:
            item = json.loads(data)
            if not isinstance(item, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            raise AgentError("CALL_OUTCOME_UNKNOWN", "模型流中断，保留片段且不自动重试", 502) from exc
        if item.get("error"):
            raise AgentError("CALL_OUTCOME_UNKNOWN", "模型回复中断，已保留片段，不自动重试", 502)
        request_id = item.get("id") or request_id
        usage = item.get("usage") or usage
        for choice in item.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta", {}).get("content")
            if isinstance(delta, str):
                content += delta
                visible = public_text(content)
                if visible:
                    on_preview(visible)
    if not done:
        raise AgentError("CALL_OUTCOME_UNKNOWN", "模型连接中断，回复尚未完成；不会自动重试", 502)
    if finish != "stop":
        raise AgentError("MODEL_SCHEMA_INVALID", "模型回复未完整结束，不能执行后续动作", 502)
    return {"id": request_id, "usage": usage, "choices": [{"message": {"content": content}}]}
