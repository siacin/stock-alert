from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from stock_alert.llm_client import (LLMError, compatibility_payload, decode_response, extract_text,
                                    request_completion, resolve_endpoint)
from stock_alert.news_agent import NewsAgentError, NewsAgentService, normalize_settings


class Response:
    def __init__(self, body, status=200, content_type="application/json"):
        self.content = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
        self.status_code = status
        self.headers = {"Content-Type":content_type}
        self.closed = False
    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), 7): yield self.content[i:i+7]
    def raise_for_status(self): pass
    def close(self): self.closed = True


def event(data):
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def chat(content="OK", finish="stop"):
    return {"choices":[{"index":0,"message":{"content":content},"finish_reason":finish}]}


class LLMClientTests(unittest.TestCase):
    def settings(self, **kwargs):
        return normalize_settings({"api_url":"https://llm.example", "model":"deepseek-v4-flash", "api_key":"test-secret", **kwargs})

    def test_base_url_variants_and_preserved_prefix(self):
        cases = {
            "https://llm.example":"https://llm.example/v1/chat/completions",
            "https://llm.example/":"https://llm.example/v1/chat/completions",
            "https://llm.example/v1/":"https://llm.example/v1/chat/completions",
            "https://llm.example/api/v1":"https://llm.example/api/v1/chat/completions",
            "https://llm.example/v1/responses/":"https://llm.example/v1/responses",
            "https://llm.example/chat/completions":"https://llm.example/chat/completions",
            "http://127.0.0.1:11434/v1":"http://127.0.0.1:11434/v1/chat/completions",
            "https://llm.example/openai/deployments/demo/chat/completions?api-version=2025-01-01":"https://llm.example/openai/deployments/demo/chat/completions?api-version=2025-01-01",
        }
        for value, expected in cases.items():
            with self.subTest(value=value): self.assertEqual(resolve_endpoint(value), expected)

    def test_invalid_or_credential_bearing_urls_rejected(self):
        for url in ("file:///tmp/api", "https://u:p@llm.example/v1", "https://llm.example/#key", "https://llm.example?api_key=secret", "https://llm.example:bad", "https://llm.example/has space", "https://llm.example/v1/models"):
            with self.subTest(url=url), self.assertRaises(LLMError): resolve_endpoint(url)

    def test_changed_origin_cannot_reuse_old_key(self):
        current=self.settings()
        with self.assertRaises(NewsAgentError): normalize_settings({"api_url":"https://another.example"}, current)
        self.assertEqual(normalize_settings({"api_url":"https://llm.example/v1/responses"},current)["api_key"],"test-secret")
        self.assertEqual(normalize_settings({"api_url":"https://another.example", "api_key":"new-secret"},current)["api_key"],"new-secret")

    def test_settings_validation_and_optional_temperature(self):
        self.assertIsNone(self.settings(temperature=None)["temperature"])
        self.assertEqual(self.settings(request_timeout_seconds=300)["request_timeout_seconds"],300)
        for change in ({"request_timeout_seconds":301},{"max_output_tokens":1},{"thinking_mode":"guess"},{"stream_mode":"guess"},{"temperature":float('nan')},{"api_key":"line\nvalue"}):
            with self.assertRaises(NewsAgentError): self.settings(**change)

    def test_chat_json_content_and_closed_transport(self):
        response=Response(chat())
        post=Mock(return_value=response)
        text,info=request_completion(self.settings(),{"model":"deepseek-v4-flash","messages":[]},post)
        self.assertEqual(text,"OK")
        self.assertEqual(info["response_format"],"json")
        self.assertTrue(response.closed)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(post.call_args.kwargs["json"]["thinking"],{"type":"disabled"})

    def test_chat_sse_chunks_unicode_reasoning_not_returned(self):
        body=": heartbeat\n\n"+event({"choices":[{"index":0,"delta":{"reasoning_content":"private reasoning"}}]})
        body+=event({"choices":[{"index":0,"delta":{"content":"连"}}]})
        body+=event({"choices":[{"index":0,"delta":{"content":[{"type":"text","text":"接成功"}]},"finish_reason":"stop"}]})+"data: [DONE]\n\n"
        text,info=request_completion(self.settings(),{"model":"x","messages":[]},Mock(return_value=Response(body,content_type="text/event-stream")))
        self.assertEqual(text,"连接成功")
        self.assertEqual(info["response_format"],"sse")

    def test_large_stream_reasoning_is_discarded_without_consuming_final_text_limit(self):
        body=event({"choices":[{"index":0,"delta":{"reasoning_content":"private"*200}}]})
        body+=event({"choices":[{"index":0,"delta":{"content":"OK"},"finish_reason":"stop"}]})
        with patch("stock_alert.llm_client.MAX_RESPONSE_BYTES",64):
            text,_=request_completion(self.settings(),{},Mock(return_value=Response(body,content_type="text/event-stream")))
        self.assertEqual(text,"OK")

    def test_mislabeled_large_sse_uses_bounded_stream_limit(self):
        body=event({"choices":[{"index":0,"delta":{"reasoning_content":"private"*200}}]})
        body+=event({"choices":[{"index":0,"delta":{"content":"OK"},"finish_reason":"stop"}]})
        with patch("stock_alert.llm_client.MAX_RESPONSE_BYTES",64):
            text,info=request_completion(self.settings(),{},Mock(return_value=Response(body,content_type="application/json")))
        self.assertEqual(text,"OK")
        self.assertEqual(info["response_format"],"sse")

    def test_cumulative_stream_deltas_are_not_duplicated(self):
        body=event({"choices":[{"index":0,"delta":{"content":"A"}}]})
        body+=event({"choices":[{"index":0,"delta":{"content":"AB"}}]})
        body+=event({"choices":[{"index":0,"delta":{"content":"ABC"},"finish_reason":"stop"}]})
        text,_=request_completion(self.settings(),{},Mock(return_value=Response(body,content_type="text/event-stream")))
        self.assertEqual(text,"ABC")

    def test_done_event_without_trailing_newline_is_processed(self):
        body=event({"choices":[{"index":0,"delta":{"content":"OK"}}]})+"data: [DONE]"
        text,_=request_completion(self.settings(),{},Mock(return_value=Response(body,content_type="text/event-stream")))
        self.assertEqual(text,"OK")

    def test_chat_final_finish_marker_without_done_is_accepted(self):
        body=event({"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]})
        text,_=request_completion(self.settings(),{},Mock(return_value=Response(body)))
        self.assertEqual(text,"OK")

    def test_gateway_left_open_after_sse_completion_is_not_waited_on(self):
        response=Response("")
        def body(chunk_size=1):
            yield event({"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}).encode()
            raise AssertionError("Must not read beyond the terminal event")
        response.iter_content=body
        self.assertEqual(request_completion(self.settings(),{},Mock(return_value=response))[0],"OK")
        self.assertTrue(response.closed)

    def test_unfinished_and_truncated_sse_rejected(self):
        for body in (event({"choices":[{"delta":{"content":"partial"}}]}), event({"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]})+"data: [DONE]\n\n", "data: {broken\n\ndata: [DONE]\n\n"):
            with self.assertRaises(LLMError): request_completion(self.settings(),{},Mock(return_value=Response(body)))

    def test_responses_sse_and_multiple_blocks(self):
        body=event({"type":"response.output_text.delta","delta":"Hello"})
        body+=event({"type":"response.completed","response":{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"Hello"},{"type":"output_text","text":" world"}]}]}})
        settings=self.settings(api_url="https://llm.example/v1/responses")
        text,_=request_completion(settings,{"model":"x","input":"test"},Mock(return_value=Response(body,content_type="text/event-stream")))
        self.assertEqual(text,"Hello world")
        payload=compatibility_payload(settings,{"model":"x"})
        self.assertIn("max_output_tokens",payload)
        self.assertEqual(payload["reasoning"],{"effort":"none"})

    def test_html_explained_and_bom_json_accepted(self):
        with self.assertRaisesRegex(LLMError,"HTML"):
            request_completion(self.settings(),{},Mock(return_value=Response("<!doctype html><html>login</html>",content_type="text/html")))
        response=Response("\ufeff"+json.dumps(chat()))
        self.assertEqual(request_completion(self.settings(),{},Mock(return_value=response))[0],"OK")

    def test_http_error_redaction_and_no_auth_retry(self):
        response=Response({"error":{"message":"bad test-secret and Bearer other-secret"}},401)
        post=Mock(return_value=response)
        with self.assertRaises(LLMError) as caught: request_completion(self.settings(),{},post)
        self.assertIn("401",str(caught.exception))
        self.assertNotIn("test-secret",str(caught.exception))
        self.assertNotIn("other-secret",str(caught.exception))
        self.assertEqual(post.call_count,1)

    def test_redirect_cannot_forward_credentials(self):
        response=Response({},302)
        post=Mock(return_value=response)
        with self.assertRaisesRegex(LLMError,"重定向"): request_completion(self.settings(),{},post)
        self.assertEqual(post.call_count,1)
        self.assertTrue(response.closed)

    def test_explicit_unsupported_temperature_is_retried_once(self):
        responses=[Response({"error":{"message":"unsupported temperature"}},400),Response(chat())]
        captured=[]
        def post(*args,**kwargs):
            captured.append(dict(kwargs["json"]))
            return responses[len(captured)-1]
        _,info=request_completion(self.settings(),{"model":"x","temperature":.2},post)
        self.assertIn("temperature",captured[0]); self.assertNotIn("temperature",captured[1])
        self.assertEqual(len(info["adjustments"]),1)
        self.assertTrue(all(r.closed for r in responses))

    def test_max_tokens_can_be_renamed_for_compatible_models(self):
        post=Mock(side_effect=[Response({"error":{"message":"max_tokens is not supported; use max_completion_tokens"}},400),Response(chat())])
        request_completion(self.settings(),{},post)
        self.assertIn("max_completion_tokens",post.call_args.kwargs["json"])

    def test_auto_stream_can_fall_back_but_explicit_stream_does_not(self):
        post=Mock(side_effect=[Response({"error":{"message":"stream is not supported"}},400),Response(chat())])
        request_completion(self.settings(),{},post)
        self.assertFalse(post.call_args.kwargs["json"]["stream"])
        post=Mock(return_value=Response({"error":{"message":"stream is not supported"}},400))
        with self.assertRaises(LLMError): request_completion(self.settings(stream_mode="on"),{},post)
        self.assertEqual(post.call_count,1)

    def test_timeout_does_not_retry_or_leak_transport_details(self):
        post=Mock(side_effect=requests.Timeout("private-proxy-address test-secret"))
        with self.assertRaises(LLMError) as error: request_completion(self.settings(),{},post)
        self.assertEqual(post.call_count,1)
        self.assertNotIn("test-secret",str(error.exception))
        self.assertNotIn("private-proxy",str(error.exception))

    def test_response_limit_and_deadline(self):
        response=Response(chat())
        with self.assertRaises(LLMError): decode_response(response,"",time.monotonic()-1)
        with patch("stock_alert.llm_client.MAX_RESPONSE_BYTES",8), self.assertRaises(LLMError):
            request_completion(self.settings(),{},Mock(return_value=response))

    def test_reasoning_only_and_incomplete_responses_not_success(self):
        for payload in ({"choices":[{"message":{"reasoning_content":"thoughts"}}]}, {"status":"incomplete","output_text":"partial"}):
            with self.assertRaises(LLMError): extract_text(payload)

    def test_probe_does_not_save_settings_or_send_watchlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            post=Mock(return_value=Response(chat()))
            service=NewsAgentService(Path(tmp)/"config.json",request_post=post)
            service.save_settings(self.settings())
            before=service.settings_path.read_bytes()
            result=service.test_connection({"request_timeout_seconds":240})
            self.assertTrue(result["ok"])
            self.assertFalse(result["saved"])
            self.assertEqual(before,service.settings_path.read_bytes())
            outgoing=post.call_args.kwargs["json"]
            self.assertEqual(outgoing["max_tokens"],256)
            self.assertNotIn("watchlist",json.dumps(outgoing))
            self.assertNotIn("test-secret",json.dumps(result))


if __name__ == "__main__": unittest.main()
