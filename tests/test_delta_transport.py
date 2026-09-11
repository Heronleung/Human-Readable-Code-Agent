"""Deterministic tests for the P4.8 rule-delta interpretation transport.

No real credential, socket or network is used: the HTTP POST and the credential
getter are injected fakes. The tests prove one-attempt behaviour (no retry),
fixed origin/model/auth, thinking-disabled, prose-wrapped-JSON rejection,
redirect/arbitrary-origin rejection, bounded request/output enforcement, and
that the credential and raw response never leak.
"""

from __future__ import annotations

import json
import unittest

from hrca import delta_transport, provider, rule_delta, rule_delta_interpret


def _valid_content():
    payload = {
        "schema_version": rule_delta_interpret.RULE_DELTA_INTERPRET_SCHEMA_VERSION,
        "outcome": rule_delta_interpret.OUTCOME_DELTA,
        "delta": {
            "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
            "result_kind": rule_delta.RESULT_KIND_QUOTATION,
            "changes": [
                {
                    "operation": rule_delta.OPERATION_SET_PARAMETER,
                    "rule_id": rule_delta.RESULT_KIND_QUOTATION,
                    "parameter_id": "member_discount_rate",
                    "value": "0.10",
                }
            ],
        },
    }
    return json.dumps(payload, separators=(",", ":"))


def _chat_body(content=_valid_content(), usage=None):
    body = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def _request(requirement="Members get 10% off."):
    return provider.ProviderRequest(
        task_id="delta:abc",
        task=rule_delta_interpret.build_instruction(),
        context=(requirement,),
    )


class _CapturingPost:
    def __init__(self, status=200, body=None, side_effect=None):
        self.calls = 0
        self.status = status
        self.body = body if body is not None else _chat_body()
        self.side_effect = side_effect
        self.last_url = None
        self.last_headers = None
        self.last_body = None

    def __call__(self, url, body, headers, timeout):
        self.calls += 1
        self.last_url = url
        self.last_headers = headers
        self.last_body = body
        if self.side_effect is not None:
            raise self.side_effect
        return self.status, self.body


class OneAttemptTests(unittest.TestCase):
    def test_timeout_makes_exactly_one_attempt(self):
        post = _CapturingPost(side_effect=TimeoutError())
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        with self.assertRaises(delta_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertEqual(post.calls, 1)

    def test_network_error_makes_exactly_one_attempt(self):
        post = _CapturingPost(side_effect=OSError("boom"))
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        with self.assertRaises(delta_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "network_denied")
        self.assertEqual(post.calls, 1)


class OriginModelAuthTests(unittest.TestCase):
    def test_posts_to_exact_fixed_origin(self):
        post = _CapturingPost()
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        transport.generate(_request())
        self.assertEqual(post.last_url, delta_transport.ENDPOINT_URL)

    def test_uses_allowlisted_model(self):
        post = _CapturingPost()
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        transport.generate(_request())
        sent = json.loads(post.last_body.decode("utf-8"))
        self.assertEqual(sent["model"], "deepseek-v4-flash")

    def test_disables_thinking(self):
        post = _CapturingPost()
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        transport.generate(_request())
        sent = json.loads(post.last_body.decode("utf-8"))
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertEqual(sent["response_format"], {"type": "json_object"})

    def test_sends_bearer_auth_and_no_key_in_result(self):
        post = _CapturingPost()
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-super-secret-value", http_post=post
        )
        result = transport.generate(_request())
        self.assertTrue(
            post.last_headers["Authorization"].startswith("Bearer ")
        )
        self.assertIn("sk-super-secret-value", post.last_headers["Authorization"])
        self.assertNotIn("sk-super-secret-value", result.content)
        self.assertNotIn(
            "sk-super-secret-value",
            json.dumps(result.structured_payload or {}),
        )


class CredentialTests(unittest.TestCase):
    def test_missing_credential_never_reaches_network(self):
        post = _CapturingPost()
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: None, http_post=post
        )
        with self.assertRaises(delta_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "credential_missing")
        self.assertEqual(post.calls, 0)


class SuccessTests(unittest.TestCase):
    def test_valid_response_returns_normalized_payload(self):
        post = _CapturingPost(
            body=_chat_body(
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            )
        )
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        result = transport.generate(_request())
        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(result.model, "deepseek-v4-flash")
        self.assertEqual(
            result.structured_payload["outcome"],
            rule_delta_interpret.OUTCOME_DELTA,
        )
        self.assertEqual(result.usage.total_tokens, 15)

    def test_content_is_sanitized_not_raw(self):
        post = _CapturingPost()
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        result = transport.generate(_request())
        parsed = json.loads(result.content)
        self.assertIn("outcome", parsed)
        self.assertNotIn("choices", parsed)


class RejectionTests(unittest.TestCase):
    def test_prose_wrapped_json_is_rejected(self):
        # A markdown code fence (prose-wrapped JSON) must fail closed — unlike
        # the advisory transport, the strict data-only contract never tolerates
        # a fence.
        fenced = "```json\n" + _valid_content() + "\n```"
        post = _CapturingPost(body=_chat_body(content=fenced))
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        with self.assertRaises(delta_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "invalid_output")

    def test_redirect_is_rejected_not_followed(self):
        # A 3xx status is a bounded failure; the transport never follows it to
        # another origin and never issues a second request.
        post = _CapturingPost(status=302, body=b"redirect")
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        with self.assertRaises(delta_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "provider_failure")
        self.assertEqual(post.calls, 1)
        self.assertEqual(post.last_url, delta_transport.ENDPOINT_URL)

    def test_schema_mismatch_raises_invalid_output(self):
        bad = json.dumps(
            {"schema_version": "9.9.9"}, separators=(",", ":")
        )
        post = _CapturingPost(body=_chat_body(content=bad))
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        with self.assertRaises(delta_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "invalid_output")

    def test_non_delta_outcome_is_valid_output(self):
        # A clarification outcome is valid structured output (not an error).
        clarification = json.dumps(
            {
                "schema_version": rule_delta_interpret.RULE_DELTA_INTERPRET_SCHEMA_VERSION,
                "outcome": "clarification_required",
                "clarification_questions": ["which rule?"],
            },
            separators=(",", ":"),
        )
        post = _CapturingPost(body=_chat_body(content=clarification))
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        result = transport.generate(_request())
        self.assertEqual(
            result.structured_payload["outcome"], "clarification_required"
        )


class RequestBoundTests(unittest.TestCase):
    def test_oversized_request_is_rejected_before_network(self):
        post = _CapturingPost()
        transport = delta_transport.DeltaInterpretProvider(
            credential_getter=lambda: "sk-test", http_post=post
        )
        huge = "x" * (rule_delta_interpret.MAX_REQUEST_BYTES + 100)
        with self.assertRaises(delta_transport.TransportError) as ctx:
            transport.generate(_request(requirement=huge))
        self.assertEqual(ctx.exception.code, "over_limit")
        self.assertEqual(post.calls, 0)


class CapabilityTests(unittest.TestCase):
    def test_capabilities_declare_no_streaming_cancellation_or_tools(self):
        transport = delta_transport.DeltaInterpretProvider(credential_getter=lambda: None)
        caps = transport.capabilities()
        self.assertTrue(caps.structured_output)
        self.assertFalse(caps.streaming)
        self.assertFalse(caps.cancellation)
        self.assertFalse(caps.tool_calling)


if __name__ == "__main__":
    unittest.main()
