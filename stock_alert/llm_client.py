"""Bounded OpenAI-compatible JSON/SSE transport for user-selected LLM gateways."""
from __future__ import annotations

import copy
import json
import re
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import requests

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_STREAM_EVENT_BYTES = 16 * 1024 * 1024


class LLMError(RuntimeError):
    pass


def resolve_endpoint(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if any(c.isspace() or ord(c) < 32 for c in value):
        raise LLMError("API 地址不能含空白或控制字符")
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # validate malformed ports without changing caller's URL
    except ValueError as exc:
        raise LLMError("API 地址格式或端口无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LLMError("Agent API 地址必须是有效的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password or parsed.fragment:
        raise LLMError("API 地址不能含用户名、密码或 # 片段；密钥请填入独立密钥栏")
    if any(re.search(r"key|token|secret|authorization|password", k, re.I) for k, _ in parse_qsl(parsed.query)):
        raise LLMError("不要把密钥放进 API 地址查询参数")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1/chat/completions"
    elif not path.endswith(("/chat/completions", "/responses")):
        if path.endswith(("/models", "/messages")):
            raise LLMError("请填写兼容接口的基础地址或 /chat/completions、/responses 地址")
        path += "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def origin(value):
    parsed = urlsplit(value)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def safe_text(value, key="", limit=260):
    text = str(value or "")
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def error_message(payload, key=""):
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return safe_text(error.get("message") or error.get("code") or "服务商返回错误", key)
    return safe_text(error, key)


def text_parts(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def extract_text(payload):
    if not isinstance(payload, dict):
        raise LLMError("Agent API 响应格式不受支持")
    if payload.get("status") in {"failed", "incomplete", "cancelled"}:
        raise LLMError("模型响应失败或未完成；可减少资讯数量、关闭思考或增加输出上限")
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        if first.get("finish_reason") in {"length", "max_tokens", "content_filter"}:
            raise LLMError("模型输出被截断或过滤；请回到产业方向选择并减少本次同时深挖的方向，或提高单方向输出上限。没有自动重试，避免重复计费")
        message = first.get("message") or {}
        if isinstance(message, dict):
            if message.get("refusal"):
                raise LLMError("模型拒绝了此次请求，请调整问题后重试")
            content = text_parts(message.get("content"))
            if content.strip():
                return content.strip()
            if message.get("reasoning_content") or message.get("reasoning"):
                raise LLMError("接口仅返回思考过程，没有最终答案；请关闭思考或增加输出上限")
    output = payload.get("output_text")
    if isinstance(output, str) and output.strip():
        return output.strip()
    output = payload.get("output")
    if isinstance(output, list):
        text = "\n".join(text_parts(item.get("content")) for item in output if isinstance(item, dict) and item.get("type", "message") == "message")
        if text.strip():
            return text.strip()
    raise LLMError("Agent API 响应中没有可读取的最终文本")


def _stream_payload(body, key):
    texts, reasoning, final, finish = [], False, None, None
    terminal = False
    for block in re.split(r"\r?\n\r?\n", body):
        if len(block.encode("utf-8")) > MAX_STREAM_EVENT_BYTES:
            raise LLMError("API 单个流式事件异常过大，已停止读取")
        values = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        if not values:
            continue  # keep-alive comments and event/id fields
        value = "\n".join(values)
        if value.strip() == "[DONE]":
            terminal = True
            continue
        try:
            payload = json.loads(value)
        except ValueError as exc:
            raise LLMError("接口流式数据不完整或格式错误，未把部分输出当作成功") from exc
        if not isinstance(payload, dict):
            continue
        if payload.get("error") or payload.get("type") in {"error", "response.failed", "response.incomplete"}:
            raise LLMError(error_message(payload, key) or "模型流式响应失败或未完成")
        event = payload.get("type", "")
        if event == "response.output_text.delta":
            texts.append(text_parts(payload.get("delta")))
        elif event == "response.completed":
            final = payload.get("response")
            terminal = True
        for choice in payload.get("choices", []) if isinstance(payload.get("choices"), list) else []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta, dict):
                texts.append(text_parts(delta.get("content")))
                reasoning = reasoning or bool(delta.get("reasoning_content") or delta.get("reasoning"))
                if delta.get("refusal"):
                    raise LLMError("模型拒绝了此次请求")
            finish = choice.get("finish_reason") or finish
            if choice.get("finish_reason") is not None:
                terminal = True
    if not terminal:
        raise LLMError("流式连接未正常结束，不能确认结果完整；没有自动重试，避免重复计费")
    if isinstance(final, dict):
        if not final.get("output") and not final.get("output_text"):
            final["output_text"] = "".join(texts)
        return final
    return {"choices": [{"finish_reason": finish, "message": {
        "content": "".join(texts), "reasoning_content": "present" if reasoning else ""}}]}


def _decode_sse_response(response, key, deadline):
    """Parse SSE incrementally, retaining final text but not private reasoning events."""
    state = {"text": "", "reasoning": False, "finish": None, "terminal": False,
             "responses": False, "model": None}
    pending = bytearray()
    data_lines: list[bytes] = []
    wire_bytes = 0

    def append_text(value):
        chunk = text_parts(value)
        if not chunk:
            return
        current = state["text"]
        # A few gateways send cumulative content despite naming the field delta.
        if chunk == current:
            return
        state["text"] = chunk if current and chunk.startswith(current) else current + chunk
        if len(state["text"].encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise LLMError("模型最终文本过大，已停止读取；请减少主题或降低单次深挖方向数")

    def consume_event():
        if not data_lines:
            return
        raw = b"\n".join(data_lines)
        data_lines.clear()
        if len(raw) > MAX_STREAM_EVENT_BYTES:
            raise LLMError("API 单个流式事件异常过大，已停止读取")
        if raw.strip() == b"[DONE]":
            state["terminal"] = True
            return
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise LLMError("接口流式数据不完整或格式错误，未把部分输出当作成功") from exc
        if not isinstance(payload, dict):
            return
        if payload.get("error") or payload.get("type") in {"error", "response.failed", "response.incomplete"}:
            raise LLMError(error_message(payload, key) or "模型流式响应失败或未完成")
        state["model"] = payload.get("model") or state["model"]
        event = payload.get("type", "")
        if event == "response.output_text.delta":
            state["responses"] = True
            append_text(payload.get("delta"))
        elif event == "response.completed":
            state["responses"] = True
            final = payload.get("response") if isinstance(payload.get("response"), dict) else {}
            state["model"] = final.get("model") or state["model"]
            try:
                append_text(extract_text(final))
            except LLMError:
                if not state["text"]:
                    raise
            state["terminal"] = True
        for choice in payload.get("choices", []) if isinstance(payload.get("choices"), list) else []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta, dict):
                append_text(delta.get("content"))
                state["reasoning"] = state["reasoning"] or bool(delta.get("reasoning_content") or delta.get("reasoning"))
                if delta.get("refusal"):
                    raise LLMError("模型拒绝了此次请求")
            state["finish"] = choice.get("finish_reason") or state["finish"]
            if choice.get("finish_reason") is not None:
                state["terminal"] = True

    for part in response.iter_content(chunk_size=4096):
        if time.monotonic() > deadline:
            raise LLMError("API 请求超过总时限；没有自动重试，避免重复计费")
        if not part:
            continue
        if isinstance(part, str):
            part = part.encode("utf-8")
        wire_bytes += len(part)
        if wire_bytes > MAX_STREAM_BYTES:
            raise LLMError("API 流式传输异常过大，已停止读取；思考过程可能过长，请减少主题或关闭思考")
        pending.extend(part)
        while b"\n" in pending:
            line, _, tail = pending.partition(b"\n")
            pending = bytearray(tail)
            line = line.rstrip(b"\r")
            if not line:
                consume_event()
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
            if state["terminal"]:
                break
        if state["terminal"]:
            break
    if pending:
        line = bytes(pending).rstrip(b"\r")
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
    if not state["terminal"]:
        consume_event()
    if not state["terminal"]:
        raise LLMError("流式连接未正常结束，不能确认结果完整；没有自动重试，避免重复计费")
    if state["responses"]:
        return {"status": "completed", "model": state["model"], "output_text": state["text"]}
    return {"model": state["model"], "choices": [{"finish_reason": state["finish"], "message": {
        "content": state["text"], "reasoning_content": "present" if state["reasoning"] else ""}}]}


def decode_response(response, key, deadline):
    content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
    if "text/event-stream" in content_type and callable(getattr(response, "iter_content", None)):
        return _decode_sse_response(response, key, deadline), "sse"
    buffer = bytearray()
    pending = bytearray()
    terminal = False
    stream_detected = False
    iterator = getattr(response, "iter_content", None)
    parts = iterator(chunk_size=8192) if callable(iterator) else [response.content]
    for part in parts:
        if time.monotonic() > deadline:
            raise LLMError("API 请求超过总时限；没有自动重试，避免重复计费")
        if not part:
            continue
        if isinstance(part, str):
            part = part.encode("utf-8")
        if re.search(rb"(?m)^data:\s*", bytes(buffer[-512:]) + part[:512]):
            stream_detected = True
        size_limit = MAX_STREAM_BYTES if stream_detected else MAX_RESPONSE_BYTES
        if len(buffer) + len(part) > size_limit:
            message = "API 流式传输异常过大，已停止读取" if stream_detected else "API 响应过大，已停止读取"
            raise LLMError(message)
        buffer.extend(part)
        pending.extend(part)
        if b"\n" not in part:
            continue  # Avoid rescanning a growing single-line JSON body for every byte.
        while b"\n" in pending:
            line, _, tail = pending.partition(b"\n")
            pending = bytearray(tail)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            stream_detected = True
            data = line[5:].strip()
            if data == b"[DONE]":
                terminal = True
                break
            try:
                item = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(item, dict):
                choices = item.get("choices")
                terminal = bool(item.get("error") or item.get("type") in {"response.completed", "response.failed", "response.incomplete", "error"}
                                or any(isinstance(c, dict) and c.get("index", 0) == 0 and c.get("finish_reason") is not None
                                       for c in (choices if isinstance(choices, list) else [])))
                if terminal:
                    break
        if terminal:
            break  # Some gateways keep the HTTP connection open after their final SSE event.
    try:
        body = buffer.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise LLMError("API 响应不是有效 UTF-8 文本") from exc
    if body.lower().startswith(("<!doctype html", "<html")) or "text/html" in content_type:
        raise LLMError("API 返回网页 HTML，而非模型结果；请检查 API 路径或网关登录/拦截页面")
    if "text/event-stream" in content_type or re.search(r"(?m)^data:\s*", body):
        return _stream_payload(body, key), "sse"
    try:
        return json.loads(body), "json"
    except ValueError as exc:
        raise LLMError("API 没有返回有效 JSON 或 SSE 流") from exc


def compatibility_payload(settings, payload):
    payload = copy.deepcopy(payload)
    responses = urlsplit(settings["api_url"]).path.rstrip("/").endswith("/responses")
    payload["stream"] = settings.get("stream_mode", "auto") != "off"
    limit = settings.get("max_output_tokens", 8192)
    payload["max_output_tokens" if responses else "max_tokens"] = limit
    mode = settings.get("thinking_mode", "auto")
    flash = settings["model"].split("/")[-1].startswith("deepseek-v4-flash")
    if mode != "auto" or flash:
        thinking = "disabled" if mode == "auto" else mode
        if responses:
            payload["reasoning"] = {"effort": "none" if thinking == "disabled" else "high"}
        else:
            payload["thinking"] = {"type": thinking}
    return payload


def request_completion(settings, payload, request_post=None):
    post = request_post or requests.post
    url = resolve_endpoint(settings["api_url"])
    key = settings.get("api_key", "")
    if key and urlsplit(url).scheme != "https" and urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise LLMError("携带密钥的远程 API 必须使用 HTTPS；本机模型可使用 HTTP")
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    outgoing = compatibility_payload({**settings, "api_url": url}, payload)
    deadline = time.monotonic() + settings["request_timeout_seconds"]
    adjustments = []
    for attempt in range(2):
        response = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError("API 请求已超过总时限")
            response = post(url, json=outgoing, headers=headers, timeout=(min(10, remaining), remaining),
                            stream=True, allow_redirects=False)
            status = getattr(response, "status_code", 200)
            if 300 <= status < 400:
                raise LLMError("API 返回重定向；为避免密钥或分析数据被转发，请填写最终接口地址")
            try:
                result, response_format = decode_response(response, key, deadline)
            except LLMError as exc:
                if status >= 400:
                    raise LLMError(f"API HTTP {status}：{str(exc)}") from exc
                raise
            detail = error_message(result, key)
            if status >= 400:
                lowered = detail.lower()
                unsupported = any(word in lowered for word in ("unsupported", "not support", "unknown", "unrecognized", "not allowed", "only", "不支持", "未知"))
                # Only explicit parameter rejections are retried, at most once.
                if attempt == 0 and status in {400, 422} and unsupported:
                    changed = False
                    if "temperature" in lowered and "temperature" in outgoing:
                        outgoing.pop("temperature")
                        adjustments.append("服务商不支持 temperature，已省略")
                        changed = True
                    if "max_tokens" in lowered and "max_tokens" in outgoing:
                        outgoing["max_completion_tokens"] = outgoing.pop("max_tokens")
                        adjustments.append("输出上限参数改用 max_completion_tokens")
                        changed = True
                    if "stream" in lowered and settings.get("stream_mode", "auto") == "auto" and outgoing.get("stream"):
                        outgoing["stream"] = False
                        adjustments.append("服务商不支持流式，改用 JSON 响应")
                        changed = True
                    if changed:
                        continue
                hint = {401:"密钥无效或已过期",403:"模型或接口访问被拒绝",404:"API 路径或模型不存在",
                        408:"服务商处理超时",429:"额度不足或触发限流",502:"服务商上游异常",503:"服务商暂不可用",504:"网关等待上游超时"}.get(status,"服务商拒绝请求")
                raise LLMError(f"API HTTP {status}：{hint}" + (f"；{detail}" if detail else ""))
            if detail:
                raise LLMError("API 返回错误：" + detail)
            # Some injected transports use raise_for_status without status_code.
            response.raise_for_status()
            text = extract_text(result)
            if key:
                text = text.replace(key, "[REDACTED]")
            return text, {"api_url": url, "response_format": response_format,
                          "adjustments": adjustments, "returned_model": result.get("model") if isinstance(result, dict) else None}
        except requests.exceptions.Timeout:
            raise LLMError(f"API 等待超过时限（设置 {settings['request_timeout_seconds']} 秒）；可增加超时、减少资讯或关闭思考。没有自动重试，以免重复计费") from None
        except requests.exceptions.SSLError:
            raise LLMError("API TLS 证书校验失败；请检查地址或证书，不要关闭证书校验") from None
        except requests.exceptions.ProxyError:
            raise LLMError("API 代理连接失败，请检查当前系统/环境代理") from None
        except requests.RequestException:
            raise LLMError("API 网络连接失败或流式连接中断；没有自动重试，以免重复计费") from None
        except LLMError:
            raise
        except Exception:
            raise LLMError("API 传输或响应格式异常，请检查服务商兼容性") from None
        finally:
            if response is not None and callable(getattr(response, "close", None)):
                response.close()
    raise LLMError("服务商仍不接受请求参数，请调整设置")
