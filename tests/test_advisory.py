"""Deterministic tests for the P4.2b advisory hosted-planning domain.

Every test here is offline and pure: no credential, no network, no filesystem.
It proves context allowlisting, secret rejection, byte/item limits, disclosure
manifest construction, schema validation, determinism, and the
deterministic-versus-provider authority mapping.
"""

from __future__ import annotations

import unittest

from hrca import advisory


def _ready_proposal():
    return {
        "state": "ready",
        "executable": False,
        "applied": False,
        "target_scope": {"entities": ["app.service"], "artifacts": ["app/service.py"]},
        "preserved_constraints": [
            {"entity_id": None, "invariant": "verified source is never rewritten"}
        ],
        "plan_steps": [
            {
                "step": 1,
                "description": "replace the purpose description of X in app.service.",
            }
        ],
        "risks": [{"level": "low", "description": "documentation-only intent"}],
        "clarifications": [],
    }


def _ready_delta():
    return {
        "intent": "user_authored",
        "entries": [
            {
                "operation": "replace_description",
                "owning_entity_id": "app.service",
                "intent_class": "documentation_intent",
                "known_dependencies": [],
                "known_callers": [],
            }
        ],
    }


def _store():
    return {
        "artifacts": [
            {"kind": "module", "locator": "app.service", "path": "app/service.py"}
        ]
    }


def _valid_payload():
    return {
        "schema_version": advisory.ADVISORY_SCHEMA_VERSION,
        "clarification_needs": ["confirm the target"],
        "impact": "documentation only",
        "assumptions": ["the baseline is current"],
        "risks": ["none"],
        "plan_suggestions": [{"step": 1, "description": "review the purpose text"}],
    }


class SecretLikeTests(unittest.TestCase):
    def test_detects_api_key_assignment(self):
        self.assertTrue(advisory.is_secret_like('api_key = "sk-abcdefghijklmnopqrstuvwxyz"'))

    def test_detects_private_key_marker(self):
        self.assertTrue(advisory.is_secret_like("-----BEGIN PRIVATE KEY-----"))

    def test_detects_token_mapping(self):
        self.assertTrue(advisory.is_secret_like('{"token": "abcdefghijklmnopqrst"}'))

    def test_ignores_ordinary_source(self):
        self.assertFalse(advisory.is_secret_like("def handle(self, name):\n    return name.upper()\n"))


class EntityAnchorTests(unittest.TestCase):
    def _blocks(self):
        return [
            {
                "block_type": "entity",
                "payload": {"locator": "app.service"},
                "source_anchors": [
                    {"file": "app/service.py", "lineno": 1, "end_lineno": 5}
                ],
            }
        ]

    def test_returns_anchor(self):
        anchors = advisory.entity_anchors(self._blocks(), ["app.service"])
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["locator"], "app.service")
        self.assertEqual(anchors[0]["file"], "app/service.py")

    def test_ignores_unknown_entity(self):
        self.assertEqual(advisory.entity_anchors(self._blocks(), ["other"]), [])


class SliceExcerptTests(unittest.TestCase):
    def test_slice_labels_and_bounds_lines(self):
        content = "a\nb\nc\nd\ne\nf\n"
        excerpt = advisory.slice_excerpt(
            file="m.py", lineno=2, end_lineno=4, content=content
        )
        self.assertTrue(excerpt.startswith("m.py:2-4\n"))
        self.assertIn("b", excerpt)
        self.assertIn("d", excerpt)
        self.assertNotIn("e\nf", excerpt)


class BuildContextTests(unittest.TestCase):
    def _excerpts(self):
        return {"app/service.py": ["app/service.py:1-5\nclass Service:\n    pass\n"]}

    def test_produces_disclosure_and_token(self):
        context, err = advisory.build_context(
            delta=_ready_delta(),
            proposal=_ready_proposal(),
            store=_store(),
            excerpts=self._excerpts(),
            denials=[],
        )
        self.assertIsNone(err)
        self.assertIsNotNone(context)
        self.assertTrue(context["advisory_token"].startswith("advisory:"))
        self.assertTrue(context["disclosure"]["one_attempt"])
        self.assertEqual(context["disclosure"]["provider_id"], "deepseek")
        kinds = {item["kind"] for item in context["disclosure"]["items"]}
        self.assertIn("intent", kinds)
        self.assertIn("proposal", kinds)
        self.assertIn("source_excerpt", kinds)

    def test_is_deterministic(self):
        kwargs = dict(
            delta=_ready_delta(),
            proposal=_ready_proposal(),
            store=_store(),
            excerpts=self._excerpts(),
            denials=[],
        )
        first, _ = advisory.build_context(**kwargs)
        second, _ = advisory.build_context(**kwargs)
        self.assertEqual(advisory.dumps(first), advisory.dumps(second))

    def test_rejects_secret_like_excerpt(self):
        context, err = advisory.build_context(
            delta=_ready_delta(),
            proposal=_ready_proposal(),
            store=_store(),
            excerpts={"app/service.py": ['api_key = "sk-abcdefghijklmnopqrstuvwxyz"']},
            denials=[],
        )
        self.assertIsNone(context)
        self.assertEqual(err, advisory.DENY_SECRET_LIKE)

    def test_rejects_binary_excerpt(self):
        context, err = advisory.build_context(
            delta=_ready_delta(),
            proposal=_ready_proposal(),
            store=_store(),
            excerpts={"app/service.py": ["binary\x00content"]},
            denials=[],
        )
        self.assertIsNone(context)
        self.assertEqual(err, advisory.DENY_BINARY)

    def test_rejects_denials(self):
        context, err = advisory.build_context(
            delta=_ready_delta(),
            proposal=_ready_proposal(),
            store=_store(),
            excerpts=self._excerpts(),
            denials=[advisory.DENY_OUTSIDE_ROOT],
        )
        self.assertIsNone(context)
        self.assertEqual(err, advisory.DENY_OUTSIDE_ROOT)

    def test_rejects_missing_anchor(self):
        context, err = advisory.build_context(
            delta=_ready_delta(),
            proposal=_ready_proposal(),
            store=_store(),
            excerpts={},
            denials=[],
        )
        self.assertIsNone(context)
        self.assertEqual(err, advisory.DENY_MISSING_ANCHOR)

    def test_rejects_over_limit_items(self):
        excerpts = {f"f{i}.py": [f"line {i}"] for i in range(40)}
        context, err = advisory.build_context(
            delta=_ready_delta(),
            proposal=_ready_proposal(),
            store=_store(),
            excerpts=excerpts,
            denials=[],
        )
        self.assertIsNone(context)
        self.assertEqual(err, advisory.DENY_OVER_LIMIT)

    def test_rejects_over_limit_bytes(self):
        excerpts = {"app/service.py": ["x" * (advisory.MAX_REQUEST_BYTES + 100)]}
        context, err = advisory.build_context(
            delta=_ready_delta(),
            proposal=_ready_proposal(),
            store=_store(),
            excerpts=excerpts,
            denials=[],
        )
        self.assertIsNone(context)
        self.assertEqual(err, advisory.DENY_OVER_LIMIT)


class ProviderRequestTests(unittest.TestCase):
    def test_request_uses_token_as_task_id(self):
        context, _ = advisory.build_context(
            delta=_ready_delta(),
            proposal=_ready_proposal(),
            store=_store(),
            excerpts={"app/service.py": ["app/service.py:1-1\nclass Service:\n"]},
            denials=[],
        )
        request = advisory.build_provider_request(context)
        self.assertEqual(request["task_id"], context["advisory_token"])
        self.assertTrue(request["task"])
        self.assertTrue(request["context"])


class PayloadValidationTests(unittest.TestCase):
    def test_accepts_valid_payload(self):
        self.assertIsNone(advisory.validate_advisory_payload(_valid_payload()))

    def test_rejects_missing_field(self):
        payload = _valid_payload()
        del payload["impact"]
        self.assertEqual(advisory.validate_advisory_payload(payload), "missing impact")

    def test_rejects_wrong_schema_version(self):
        payload = _valid_payload()
        payload["schema_version"] = "9.9.9"
        self.assertEqual(
            advisory.validate_advisory_payload(payload), "unsupported schema_version"
        )

    def test_rejects_non_sequential_plan_suggestions(self):
        payload = _valid_payload()
        payload["plan_suggestions"] = [{"step": 2, "description": "out of order"}]
        self.assertEqual(
            advisory.validate_advisory_payload(payload), "invalid plan_suggestions"
        )

    def test_normalize_drops_unknown_keys(self):
        payload = _valid_payload()
        payload["secret_field"] = "should be dropped"
        normalized = advisory.normalize_payload(payload)
        self.assertNotIn("secret_field", normalized)
        self.assertIn("impact", normalized)


class AuthorityTests(unittest.TestCase):
    def test_assemble_marks_provider_suggested(self):
        result = advisory.assemble_result(
            state=advisory.STATE_READY,
            provider_id="deepseek",
            model="deepseek-v4-flash",
            advisory_token="advisory:abc",
            sent=True,
            deterministic={"proposal": _ready_proposal()},
            provider_suggested={"impact": "documentation only"},
            usage={"total_tokens": 10},
            limitations=[],
        )
        self.assertEqual(result["state"], advisory.STATE_READY)
        self.assertEqual(result["provider_suggested"]["impact"], "documentation only")
        self.assertEqual(result["deterministic"]["proposal"]["state"], "ready")

    def test_assemble_preserves_deterministic_on_failure(self):
        result = advisory.assemble_result(
            state=advisory.STATE_TIMEOUT,
            provider_id="deepseek",
            model="deepseek-v4-flash",
            advisory_token="advisory:abc",
            sent=True,
            deterministic={"proposal": _ready_proposal()},
            provider_suggested=None,
            usage=None,
            limitations=["timeout"],
        )
        self.assertEqual(result["state"], advisory.STATE_TIMEOUT)
        self.assertIsNone(result["provider_suggested"])
        self.assertEqual(result["deterministic"]["proposal"]["state"], "ready")

    def test_all_normalized_states_are_known(self):
        for state in (
            advisory.STATE_READY,
            advisory.STATE_CREDENTIAL_MISSING,
            advisory.STATE_PROVIDER_UNAVAILABLE,
            advisory.STATE_NETWORK_DENIED,
            advisory.STATE_TIMEOUT,
            advisory.STATE_RATE_LIMITED,
            advisory.STATE_QUOTA_EXCEEDED,
            advisory.STATE_INVALID_OUTPUT,
            advisory.STATE_CONTEXT_REJECTED,
            advisory.STATE_OVER_LIMIT,
            advisory.STATE_CANCEL_REQUESTED,
            advisory.STATE_STALE_RESPONSE,
            advisory.STATE_PROVIDER_FAILURE,
        ):
            self.assertIn(state, advisory.ADVISORY_STATES)


if __name__ == "__main__":
    unittest.main()
