"""Deterministic tests for the P4.2b DeepSeek advisory transport.

No real credential, socket or network is used: the HTTP POST and the credential
getter are injected fakes. The tests prove one-attempt behaviour (no retry),
bounded request/output enforcement, HTTP-status normalization, schema
validation, and that the credential and raw response never leak.
"""

from __future__ import annotations

import ast
import json
import os
import unittest

from hrca.integrations import advisory_contract, deepseek_transport, provider


def _valid_content():
    payload = {
        "schema_version": advisory_contract.ADVISORY_SCHEMA_VERSION,
        "clarification_needs": [],
        "impact": "documentation only",
        "assumptions": [],
        "risks": [],
        "plan_suggestions": [{"step": 1, "description": "review the text"}],
    }
    return json.dumps(payload, separators=(",", ":"))


def _chat_body(content=_valid_content(), usage=None):
    body = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def _request(context=("ctx",)):
    return provider.ProviderRequest(
        task_id="advisory:abc", task="produce an advisory", context=context
    )


class _FakeTransportProvider:
    """A transport double that counts its ``generate`` calls."""

    def __init__(self):
        self.calls = 0


class OneAttemptTests(unittest.TestCase):
    def test_timeout_makes_exactly_one_attempt(self):
        calls = {"n": 0}

        def http_post(url, body, headers, timeout):
            calls["n"] += 1
            raise TimeoutError()

        transport = deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: "sk-test",
            http_post=http_post,
        )
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertEqual(calls["n"], 1)

    def test_network_error_makes_exactly_one_attempt(self):
        calls = {"n": 0}

        def http_post(url, body, headers, timeout):
            calls["n"] += 1
            raise OSError("boom")

        transport = deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: "sk-test", http_post=http_post
        )
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "network_denied")
        self.assertEqual(calls["n"], 1)


class CredentialTests(unittest.TestCase):
    def test_missing_credential_never_reaches_network(self):
        calls = {"n": 0}

        def http_post(url, body, headers, timeout):
            calls["n"] += 1
            return 200, _chat_body()

        transport = deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: None, http_post=http_post
        )
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "credential_missing")
        self.assertEqual(calls["n"], 0)

    def test_credential_never_appears_in_result(self):
        def http_post(url, body, headers, timeout):
            # Capture the headers to assert the credential is only sent there.
            self.assertIn("Authorization", headers)
            return 200, _chat_body()

        transport = deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: "sk-super-secret-value",
            http_post=http_post,
        )
        result = transport.generate(_request())
        self.assertNotIn("sk-super-secret-value", result.content)
        self.assertNotIn(
            "sk-super-secret-value", json.dumps(result.structured_payload or {})
        )


class SuccessTests(unittest.TestCase):
    def test_valid_response_returns_normalized_payload(self):
        transport = deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: "sk-test",
            http_post=lambda url, body, headers, timeout: (
                200,
                _chat_body(
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                ),
            ),
        )
        result = transport.generate(_request())
        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(result.model, "deepseek-flash")
        self.assertEqual(result.structured_payload["impact"], "documentation only")
        self.assertEqual(result.usage.total_tokens, 15)

    def test_content_is_sanitized_not_raw(self):
        # The raw model output is replaced with the sanitized advisory JSON, so
        # a raw response (and any source it echoed) is never retained.
        transport = deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: "sk-test",
            http_post=lambda url, body, headers, timeout: (200, _chat_body()),
        )
        result = transport.generate(_request())
        parsed = json.loads(result.content)
        self.assertIn("impact", parsed)
        self.assertNotIn("choices", parsed)


class FailureMappingTests(unittest.TestCase):
    def _transport_with_status(self, status):
        return deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: "sk-test",
            http_post=lambda url, body, headers, timeout: (status, b"{}"),
        )

    def test_maps_401_to_credential_missing(self):
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            self._transport_with_status(401).generate(_request())
        self.assertEqual(ctx.exception.code, "credential_missing")

    def test_maps_402_to_quota_exceeded(self):
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            self._transport_with_status(402).generate(_request())
        self.assertEqual(ctx.exception.code, "quota_exceeded")

    def test_maps_429_to_rate_limited(self):
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            self._transport_with_status(429).generate(_request())
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_maps_500_to_provider_unavailable(self):
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            self._transport_with_status(500).generate(_request())
        self.assertEqual(ctx.exception.code, "provider_unavailable")

    def test_maps_400_to_context_rejected(self):
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            self._transport_with_status(400).generate(_request())
        self.assertEqual(ctx.exception.code, "context_rejected")


class OutputValidationTests(unittest.TestCase):
    def test_invalid_json_raises_invalid_output(self):
        transport = deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: "sk-test",
            http_post=lambda url, body, headers, timeout: (
                200,
                b'{"choices": [{"message": {"content": "not json"}}]}',
            ),
        )
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "invalid_output")

    def test_schema_mismatch_raises_invalid_output(self):
        bad = json.dumps({"schema_version": "9.9.9"}, separators=(",", ":"))
        transport = deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: "sk-test",
            http_post=lambda url, body, headers, timeout: (
                200,
                _chat_body(content=bad),
            ),
        )
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            transport.generate(_request())
        self.assertEqual(ctx.exception.code, "invalid_output")


class RequestBoundTests(unittest.TestCase):
    def test_oversized_request_is_rejected_before_network(self):
        calls = {"n": 0}

        def http_post(url, body, headers, timeout):
            calls["n"] += 1
            return 200, _chat_body()

        transport = deepseek_transport.DeepSeekProvider(
            credential_getter=lambda: "sk-test", http_post=http_post
        )
        huge = "x" * (advisory_contract.MAX_REQUEST_BYTES + 100)
        with self.assertRaises(deepseek_transport.TransportError) as ctx:
            transport.generate(_request(context=(huge,)))
        self.assertEqual(ctx.exception.code, "over_limit")
        self.assertEqual(calls["n"], 0)


class CapabilityTests(unittest.TestCase):
    def test_capabilities_declare_no_streaming_cancellation_or_tools(self):
        transport = deepseek_transport.DeepSeekProvider(credential_getter=lambda: None)
        caps = transport.capabilities()
        self.assertTrue(caps.structured_output)
        self.assertFalse(caps.streaming)
        self.assertFalse(caps.cancellation)
        self.assertFalse(caps.tool_calling)


_TRANSPORT_SOURCE = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "src", "hrca", "integrations", "deepseek_transport.py",
    )
)


def _imported_names(path):
    """Return every module and symbol name the module at ``path`` imports."""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[-1])
            for alias in node.names:
                names.add(alias.name)
    return names


class ContractSeamTests(unittest.TestCase):
    """The transport takes its payload contract from the provider seam (B-R2).

    It used to reach into :mod:`hrca.twin.advisory` for the schema version, the
    bounds and the payload validator, which made the one module that opens a
    socket and reads a credential depend on a capability. This holds the seam
    from the transport's side; the transitive form of the same rule is held in
    :mod:`tests.test_architecture`, where the module graph is already available.
    """

    def test_the_transport_no_longer_imports_the_twin_package(self):
        imported = _imported_names(_TRANSPORT_SOURCE)
        self.assertNotIn("twin", imported)
        self.assertNotIn("advisory", imported)

    def test_the_transport_imports_the_provider_payload_contract(self):
        self.assertIn("advisory_contract", _imported_names(_TRANSPORT_SOURCE))

    def test_the_transport_uses_the_canonical_serializer_not_a_local_copy(self):
        from hrca.core import storage

        self.assertIs(storage.dumps, deepseek_transport.dumps)

    def test_the_contract_owns_every_symbol_the_transport_takes_from_it(self):
        # Every symbol the transport reads off the contract is the contract's
        # own definition, not a re-export it happens to see.
        with open(_TRANSPORT_SOURCE, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        used = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "advisory_contract"
        }
        self.assertTrue(used)
        for name in sorted(used):
            with self.subTest(name=name):
                self.assertIn(name, advisory_contract.__all__)
                self.assertIn(name, vars(advisory_contract))

    def test_the_contract_is_the_reason_the_transport_bounds_itself(self):
        # The bounds the transport defaults to are the contract's, so a provider
        # answer can never be accepted against a bound the transport invented.
        provider_instance = deepseek_transport.DeepSeekProvider()
        self.assertEqual(
            advisory_contract.TIMEOUT_SECONDS, provider_instance._timeout
        )
        self.assertEqual(
            advisory_contract.MAX_REQUEST_BYTES, provider_instance._max_request_bytes
        )
        self.assertEqual(
            advisory_contract.MAX_CONTEXT_ITEMS, provider_instance._max_context_items
        )
        self.assertEqual(
            advisory_contract.MAX_OUTPUT_TOKENS, provider_instance._max_output_tokens
        )


if __name__ == "__main__":
    unittest.main()
