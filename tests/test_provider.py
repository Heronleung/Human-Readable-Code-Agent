"""Tests for the provider-neutral coding-agent protocol (P2.5)."""

from __future__ import annotations

import json
import os
import unittest

from hrca.provider import (
    FAILURE_TASK_ID,
    PROTOCOL_VERSION,
    FakeProvider,
    Provider,
    ProviderCapabilities,
    ProviderError,
    ProviderReadiness,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
    map_error,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDER_MODULE_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "src", "hrca", "provider.py")
)

# Import statements that would indicate a network or I/O dependency. The
# protocol module must stay offline; none of these may appear in its source.
_NETWORK_IMPORT_TOKENS = (
    "import socket",
    "import urllib",
    "import requests",
    "import httpx",
    "import aiohttp",
    "import http",
    "from urllib",
    "from http",
    "urlopen",
)


class ProviderConformanceMixin:
    """Shared conformance checks for any :class:`Provider` implementation.

    Subclasses provide :meth:`make_provider`; the default request factory is
    deterministic so equivalent inputs always produce equivalent results.
    """

    def make_provider(self) -> Provider:
        raise NotImplementedError

    @staticmethod
    def make_request(
        task_id: str = "P2.5",
        task: str = "plan a read-only scan",
        context: tuple = ("scanner facts", "approval gates"),
    ) -> ProviderRequest:
        return ProviderRequest(task_id=task_id, task=task, context=context)

    # -- capability discovery -------------------------------------------

    def test_protocol_version_is_one(self):
        self.assertEqual(PROTOCOL_VERSION, 1)

    def test_capability_discovery_returns_typed_capabilities(self):
        caps = self.make_provider().capabilities()
        self.assertIsInstance(caps, ProviderCapabilities)
        for field in ("structured_output", "streaming", "cancellation", "tool_calling"):
            self.assertIsInstance(getattr(caps, field), bool)
        self.assertIsInstance(caps.readiness_available, bool)
        self.assertTrue(caps.context_limit is None or isinstance(caps.context_limit, int))

    # -- readiness -------------------------------------------------------

    def test_readiness_returns_typed_result(self):
        readiness = self.make_provider().readiness()
        self.assertIsInstance(readiness, ProviderReadiness)
        self.assertIsInstance(readiness.ready, bool)
        self.assertIn(readiness.status, ("ready", "unavailable"))

    # -- deterministic structured result ---------------------------------

    def test_generate_returns_typed_result(self):
        result = self.make_provider().generate(self.make_request())
        self.assertIsInstance(result, ProviderResult)
        self.assertIsInstance(result.content, str)
        self.assertTrue(result.content)
        self.assertIsInstance(result.provider, str)
        self.assertIsInstance(result.model, str)
        self.assertIsInstance(result.usage, ProviderUsage)

    def test_generate_is_deterministic(self):
        provider = self.make_provider()
        request = self.make_request()
        first = json.dumps(provider.generate(request).to_dict(), sort_keys=True)
        second = json.dumps(provider.generate(request).to_dict(), sort_keys=True)
        self.assertEqual(first, second)

    def test_result_echoes_task_id(self):
        result = self.make_provider().generate(self.make_request(task_id="task-42"))
        self.assertEqual(result.task_id, "task-42")

    # -- explicit unsupported capabilities -------------------------------

    def test_unsupported_capabilities_are_false(self):
        caps = self.make_provider().capabilities()
        self.assertFalse(caps.streaming)
        self.assertFalse(caps.cancellation)
        self.assertFalse(caps.tool_calling)

    def test_no_structured_payload_when_structured_output_unsupported(self):
        provider = FakeProvider(structured_output=False)
        result = provider.generate(self.make_request())
        self.assertIsNone(result.structured_payload)

    # -- bounded failure behaviour ---------------------------------------

    def test_controlled_failure_is_bounded(self):
        provider = self.make_provider()
        with self.assertRaises(ProviderError) as ctx:
            provider.generate(
                ProviderRequest(task_id=FAILURE_TASK_ID, task="boom", context=())
            )
        err = ctx.exception
        self.assertIn(err.code, ("provider_failure", "unavailable"))
        self.assertNotIn("boom", err.message)

    def test_map_error_sanitizes_exception_detail(self):
        err = map_error(RuntimeError("secret-token-abc123"))
        self.assertIsInstance(err, ProviderError)
        self.assertEqual(err.code, "provider_failure")
        self.assertNotIn("secret-token-abc123", err.message)
        self.assertNotIn("secret-token-abc123", str(err))

    def test_map_error_passes_through_provider_error(self):
        original = ProviderError("unavailable")
        self.assertIs(map_error(original), original)

    def test_provider_error_to_dict_is_bounded(self):
        err = ProviderError("unavailable")
        self.assertEqual(
            err.to_dict(), {"code": "unavailable", "message": "the provider is unavailable"}
        )

    # -- no mutation -------------------------------------------------------

    def test_request_is_immutable(self):
        request = self.make_request()
        with self.assertRaises(Exception):
            request.task_id = "mutated"  # type: ignore[misc]
        self.assertEqual(request.task_id, "P2.5")

    def test_generate_does_not_mutate_request(self):
        provider = self.make_provider()
        request = self.make_request()
        before = request.to_dict()
        provider.generate(request)
        self.assertEqual(request.to_dict(), before)

    def test_result_dict_is_fresh_each_call(self):
        provider = self.make_provider()
        first = provider.generate(self.make_request()).to_dict()
        first["structured_payload"]["context"].append("injected")
        second = provider.generate(self.make_request()).to_dict()
        self.assertNotIn("injected", second["structured_payload"]["context"])

    # -- no network dependency --------------------------------------------

    def test_provider_module_has_no_network_dependency(self):
        with open(PROVIDER_MODULE_PATH, "r", encoding="utf-8") as fh:
            source = fh.read().lower()
        for token in _NETWORK_IMPORT_TOKENS:
            self.assertNotIn(token, source)


class FakeProviderConformanceTests(ProviderConformanceMixin, unittest.TestCase):
    """Run the shared conformance suite against the deterministic fake."""

    def make_provider(self) -> Provider:
        return FakeProvider()


class FakeProviderBehaviourTests(unittest.TestCase):
    def test_reports_real_capabilities(self):
        provider = FakeProvider(
            structured_output=True,
            streaming=True,
            cancellation=False,
            tool_calling=False,
            context_limit=4096,
        )
        caps = provider.capabilities()
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.streaming)
        self.assertFalse(caps.cancellation)
        self.assertFalse(caps.tool_calling)
        self.assertEqual(caps.context_limit, 4096)
        self.assertTrue(caps.readiness_available)

    def test_context_limit_none_is_supported(self):
        self.assertIsNone(FakeProvider(context_limit=None).capabilities().context_limit)

    def test_unavailable_readiness(self):
        readiness = FakeProvider(
            readiness=False, readiness_detail="offline"
        ).readiness()
        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.status, "unavailable")
        self.assertEqual(readiness.detail, "offline")

    def test_structured_payload_shape_is_deterministic(self):
        result = FakeProvider().generate(
            ProviderRequest(task_id="t", task="x", context=("a", "b"))
        )
        self.assertEqual(
            result.structured_payload,
            {"task_id": "t", "context": ["a", "b"], "context_count": 2},
        )

    def test_usage_metadata_is_deterministic(self):
        usage = FakeProvider().generate(
            ProviderRequest(task_id="T", task="alpha beta", context=("c1", "c2"))
        ).usage
        # content = "fake response for task T" -> 5 words; prompt -> 2 words.
        self.assertEqual(usage.prompt_tokens, 2)
        self.assertEqual(usage.completion_tokens, 5)
        self.assertEqual(usage.total_tokens, 7)

    def test_generate_rejects_non_request(self):
        with self.assertRaises(ProviderError) as ctx:
            FakeProvider().generate("not a request")  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "invalid_request")


class ProviderErrorSafetyTests(unittest.TestCase):
    def test_message_is_fixed_catalogue_value(self):
        self.assertEqual(ProviderError("invalid_request").message, "the request is invalid")
        self.assertEqual(
            ProviderError("unsupported_capability").message,
            "the requested capability is not supported",
        )
        self.assertEqual(ProviderError("unavailable").message, "the provider is unavailable")
        self.assertEqual(ProviderError("provider_failure").message, "the provider failed")

    def test_direct_construction_cannot_carry_arbitrary_text(self):
        # ProviderError accepts only a bounded code; a secret-like message
        # argument is rejected by the constructor signature itself.
        with self.assertRaises(TypeError):
            ProviderError("provider_failure", "secret-token-abc123")

    def test_rejects_unknown_code(self):
        with self.assertRaises(ValueError):
            ProviderError("not-a-real-code")

    def test_unknown_code_does_not_leak_supplied_string(self):
        with self.assertRaises(ValueError) as ctx:
            ProviderError("secret-token-abc123")
        self.assertNotIn("secret-token-abc123", str(ctx.exception))

    def test_unknown_non_string_code_does_not_leak_value(self):
        for bad in (42, None, ["secret"], {"key": "secret-value"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    ProviderError(bad)
                self.assertNotIn(repr(bad), str(ctx.exception))
                self.assertNotIn("secret", str(ctx.exception))

    def test_error_forms_are_secret_free(self):
        err = ProviderError("provider_failure")
        for form in (err.message, str(err), json.dumps(err.to_dict(), sort_keys=True)):
            self.assertNotIn("secret", form)
            self.assertNotIn("token", form)


class StructuredPayloadValidationTests(unittest.TestCase):
    @staticmethod
    def _result(payload):
        return ProviderResult(
            task_id="t", content="c", provider="p", model="m", structured_payload=payload
        )

    def test_accepts_json_simple_payload(self):
        payload = {
            "s": "x",
            "i": 1,
            "f": 1.5,
            "b": True,
            "n": None,
            "list": [1, "a", None, [True, 2.0]],
            "dict": {"nested": {"k": [1, 2]}},
        }
        self.assertEqual(self._result(payload).structured_payload, payload)

    def test_accepts_finite_numbers(self):
        self._result({"zero": 0, "negative": -3, "float": 0.0})
        self._result({"float": -1.25})

    def test_rejects_set(self):
        with self.assertRaises(ProviderError):
            self._result({"s": {1, 2}})

    def test_rejects_tuple(self):
        with self.assertRaises(ProviderError):
            self._result({"t": (1, 2)})

    def test_rejects_arbitrary_object(self):
        class Obj:
            pass

        with self.assertRaises(ProviderError):
            self._result({"o": Obj()})

    def test_rejects_non_string_key(self):
        with self.assertRaises(ProviderError):
            self._result({1: "x"})
        with self.assertRaises(ProviderError):
            self._result({True: "x"})

    def test_rejects_non_finite_float(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ProviderError):
                    self._result({"f": bad})

    def test_rejects_nested_non_json_value(self):
        with self.assertRaises(ProviderError):
            self._result({"l": [1, {"x": object()}]})

    def test_rejects_top_level_non_mapping(self):
        with self.assertRaises(ProviderError):
            self._result([1, 2, 3])
        with self.assertRaises(ProviderError):
            self._result("plain-string")

    def test_to_dict_deep_copies_payload(self):
        payload = {"nested": {"list": [1, 2]}}
        result = self._result(payload)
        d = result.to_dict()
        d["structured_payload"]["nested"]["list"].append(3)
        self.assertEqual(result.structured_payload["nested"]["list"], [1, 2])


class ProviderRequestContextTests(unittest.TestCase):
    def test_accepts_list_and_normalizes_to_tuple(self):
        req = ProviderRequest(task_id="t", task="x", context=["a", "b"])
        self.assertIsInstance(req.context, tuple)
        self.assertEqual(req.context, ("a", "b"))

    def test_accepts_tuple(self):
        req = ProviderRequest(task_id="t", task="x", context=("a", "b"))
        self.assertEqual(req.context, ("a", "b"))

    def test_default_context_is_empty_tuple(self):
        self.assertEqual(ProviderRequest(task_id="t", task="x").context, ())

    def test_rejects_bare_string(self):
        with self.assertRaises(ProviderError):
            ProviderRequest(task_id="t", task="x", context="abc")

    def test_rejects_non_sequence(self):
        for bad in (42, 1.5, True, None, {"a": "b"}, {1, 2}):
            with self.subTest(bad=bad):
                with self.assertRaises(ProviderError):
                    ProviderRequest(task_id="t", task="x", context=bad)

    def test_rejects_empty_string_item(self):
        with self.assertRaises(ProviderError):
            ProviderRequest(task_id="t", task="x", context=["a", ""])
        with self.assertRaises(ProviderError):
            ProviderRequest(task_id="t", task="x", context=["a", "  "])

    def test_rejects_non_string_item(self):
        for bad in (42, None, True, ["nested"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ProviderError):
                    ProviderRequest(task_id="t", task="x", context=["a", bad])


if __name__ == "__main__":
    unittest.main()
