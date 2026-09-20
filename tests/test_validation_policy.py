"""The code-owned validation command policy (P5.5a).

A check is the only thing a caller can name, and every operational value behind
it comes from a module that already owns it. These tests hold the registry to
the accepted package set, to the runner's own constants, and to the refusal of
every runtime surface a caller might hope exists.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import unittest

from hrca import app_package, container_runner, delta_verifier, rule_delta, validation_policy

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
SRC = os.path.join(REPO, "src")
MANIFEST = os.path.join(REPO, "fixtures", "validation", "manifest.json")


def _corpus() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


class RegistryTests(unittest.TestCase):
    def test_the_policy_version(self):
        self.assertEqual("1.0.0", validation_policy.POLICY_VERSION)

    def test_the_check_set_is_exactly_the_three_reviewed_checks(self):
        self.assertEqual(
            [
                "check:late_return_fee",
                "check:quotation_alternate",
                "check:quotation_reference",
            ],
            list(validation_policy.CHECK_IDS),
        )
        self.assertEqual(
            validation_policy.CHECK_IDS, tuple(sorted(validation_policy.POLICY))
        )

    def test_every_check_record_is_complete_and_canonical(self):
        for check_id in validation_policy.CHECK_IDS:
            with self.subTest(check=check_id):
                record = validation_policy.check_record(check_id)
                self.assertEqual({"check_id"} | set(validation_policy.CHECK_FIELDS),
                                 set(record))
                self.assertEqual(check_id, record["check_id"])

    def test_every_check_names_an_accepted_package_and_handler(self):
        for check_id in validation_policy.CHECK_IDS:
            with self.subTest(check=check_id):
                package = validation_policy.package_for(check_id)
                self.assertIsNotNone(package)
                self.assertIn(
                    package["package_id"], app_package.ALLOWED_PACKAGE_IDS
                )
                self.assertIn(package["handler"], app_package.ALLOWED_HANDLERS)

    def test_every_check_input_satisfies_its_own_package_form(self):
        for check_id in validation_policy.CHECK_IDS:
            with self.subTest(check=check_id):
                package = validation_policy.package_for(check_id)
                record = validation_policy.check_record(check_id)
                self.assertIsNone(
                    app_package.validate_form_input(package, record["form_input"])
                )

    def test_the_policy_contradicts_nothing(self):
        self.assertIsNone(validation_policy.validate_policy())

    def test_an_unknown_check_has_no_package(self):
        self.assertIsNone(validation_policy.package_for("check:not_a_check"))

    def test_the_inputs_are_the_code_owned_protected_inputs(self):
        reference = validation_policy.POLICY[validation_policy.CHECK_QUOTATION_REFERENCE]
        self.assertEqual(
            delta_verifier.protected_inputs(rule_delta.RESULT_KIND_QUOTATION)[0],
            reference["form_input"],
        )
        late = validation_policy.POLICY[validation_policy.CHECK_LATE_RETURN_FEE]
        self.assertEqual(
            delta_verifier.protected_inputs(rule_delta.RESULT_KIND_LATE_RETURN_FEE)[0],
            late["form_input"],
        )


class RunnerAgreementTests(unittest.TestCase):
    """The declared tokens must be the runner's own, not a parallel copy."""

    def test_the_declared_timeout_is_the_runner_s(self):
        for check_id in validation_policy.CHECK_IDS:
            with self.subTest(check=check_id):
                self.assertEqual(
                    container_runner.RUNNER_TIMEOUT_SECONDS,
                    validation_policy.POLICY[check_id]["timeout_seconds"],
                )

    def test_the_declared_network_policy_is_the_runner_s(self):
        for check_id in validation_policy.CHECK_IDS:
            with self.subTest(check=check_id):
                self.assertEqual(
                    container_runner.RUNNER_NETWORK,
                    validation_policy.POLICY[check_id]["network_policy"],
                )
                self.assertEqual("none", validation_policy.NETWORK_POLICY_NONE)

    def test_the_declared_artifact_is_the_runner_s_output_file(self):
        for check_id in validation_policy.CHECK_IDS:
            with self.subTest(check=check_id):
                self.assertEqual(
                    container_runner._OUTPUT_FILENAME,
                    validation_policy.POLICY[check_id]["expected_artifact"],
                )
                self.assertEqual(
                    container_runner._OUTPUT_FILENAME,
                    validation_policy.RESULT_ARTIFACT_FILENAME,
                )

    def test_the_declared_resource_profile_and_credential_policy_are_bounded_tokens(self):
        for check_id in validation_policy.CHECK_IDS:
            with self.subTest(check=check_id):
                entry = validation_policy.POLICY[check_id]
                self.assertEqual(
                    validation_policy.RESOURCE_PROFILE_DEFAULT, entry["resource_profile"]
                )
                self.assertEqual(
                    validation_policy.CREDENTIAL_POLICY_NONE, entry["credential_policy"]
                )
                for value in (entry["resource_profile"], entry["credential_policy"],
                              entry["network_policy"]):
                    self.assertLessEqual(len(value), 32)
                    self.assertNotIn("/", value)

    def test_the_working_directory_is_declared_image_owned(self):
        # The runner never passes --workdir; the image's own WORKDIR is what
        # applies. The contract says so instead of pretending to set one.
        self.assertEqual(
            "image_owned", validation_policy.WORKING_DIRECTORY_IMAGE_OWNED
        )
        argv = container_runner.ContainerRunner().build_command(
            handler="quotation_rules.evaluate",
            input_payload={"subtotal": "1.00", "member": False, "region": "west"},
            container_name="x",
            input_dir="/tmp/in",
            output_dir="/tmp/out",
        )
        self.assertNotIn("--workdir", argv)
        self.assertNotIn("-w", argv)

    def test_no_check_carries_parameters(self):
        for check_id in validation_policy.CHECK_IDS:
            with self.subTest(check=check_id):
                self.assertIsNone(validation_policy.POLICY[check_id]["parameters"])


class ResolveChecksTests(unittest.TestCase):
    def test_none_selects_every_check(self):
        check_ids, reason = validation_policy.resolve_checks(None)
        self.assertIsNone(reason)
        self.assertEqual(list(validation_policy.CHECK_IDS), check_ids)

    def test_a_subset_is_accepted_and_ordered_canonically(self):
        check_ids, reason = validation_policy.resolve_checks(
            ["check:quotation_reference", "check:late_return_fee"]
        )
        self.assertIsNone(reason)
        self.assertEqual(
            ["check:late_return_fee", "check:quotation_reference"], check_ids
        )

    def test_an_unknown_check_is_refused(self):
        for requested in (["check:nope"], ["nope"], [7]):
            with self.subTest(requested=requested):
                check_ids, reason = validation_policy.resolve_checks(requested)
                self.assertIsNone(check_ids)
                self.assertEqual(validation_policy.REASON_UNKNOWN_CHECK, reason)

    def test_a_duplicate_is_refused(self):
        _check_ids, reason = validation_policy.resolve_checks(
            ["check:late_return_fee", "check:late_return_fee"]
        )
        self.assertEqual(validation_policy.REASON_DUPLICATE_CHECK, reason)

    def test_an_empty_list_is_refused(self):
        _check_ids, reason = validation_policy.resolve_checks([])
        self.assertEqual(validation_policy.REASON_NO_CHECKS, reason)

    def test_too_many_checks_are_refused(self):
        _check_ids, reason = validation_policy.resolve_checks(
            ["check:late_return_fee"] * (validation_policy.MAX_CHECKS + 1)
        )
        self.assertEqual(validation_policy.REASON_TOO_MANY_CHECKS, reason)

    def test_a_non_list_is_refused(self):
        for requested in ("check:late_return_fee", {}, 7):
            with self.subTest(requested=requested):
                _check_ids, reason = validation_policy.resolve_checks(requested)
                self.assertEqual(validation_policy.REASON_BAD_CHECK_LIST, reason)


class OverrideKeyTests(unittest.TestCase):
    def test_every_named_runtime_surface_has_its_own_sentence(self):
        corpus = _corpus()
        for key, sentence in sorted(corpus["override_rejections"].items()):
            with self.subTest(key=key):
                self.assertEqual(sentence, validation_policy.OVERRIDE_KEYS[key])

    def test_the_sentences_are_bounded_and_path_free(self):
        for key, sentence in sorted(validation_policy.OVERRIDE_KEYS.items()):
            with self.subTest(key=key):
                self.assertLess(len(sentence), 120)
                self.assertNotIn("/", sentence)
                self.assertNotIn("\\", sentence)

    def test_the_obvious_runtime_surfaces_are_all_covered(self):
        for key in (
            "argv", "command", "cmd", "shell", "entrypoint", "executable",
            "image", "mount", "mounts", "env", "environment", "timeout",
            "resources", "memory", "cpus", "network", "credential", "secrets",
            "user", "privileged", "cwd", "workdir", "handler", "package_id",
        ):
            with self.subTest(key=key):
                self.assertIn(key, validation_policy.OVERRIDE_KEYS)

    def test_no_sentence_invents_a_surface_that_exists(self):
        # The contract never offers to *set* one of these; every sentence is a
        # refusal, so a reader cannot mistake it for a setting.
        for sentence in validation_policy.OVERRIDE_KEYS.values():
            self.assertIn("may not", sentence)


class PurityTests(unittest.TestCase):
    """The policy is a table, not a dispatcher: it cannot run anything."""

    _FORBIDDEN = frozenset(
        {
            "os", "subprocess", "socket", "shutil", "tempfile", "pathlib",
            "urllib", "http", "ssl", "ctypes", "runpy", "platform", "uuid",
            "hrca.container_runner", "hrca.validation", "hrca.validation_plan",
            "hrca.boundary", "hrca.client", "hrca.client_core", "hrca.workspace",
            "hrca.memory", "hrca.twin_store", "hrca.provider",
            "hrca.credential_store",
        }
    )

    def test_the_policy_imports_nothing_that_could_run_or_reach(self):
        with open(
            os.path.join(SRC, "hrca", "validation_policy.py"), encoding="utf-8"
        ) as fh:
            tree = ast.parse(fh.read())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules.add(("hrca." if node.level else "") + node.module)
        for name in sorted(modules):
            with self.subTest(imported=name):
                self.assertNotIn(name, self._FORBIDDEN)

    def test_the_policy_opens_nothing_and_evaluates_nothing(self):
        with open(
            os.path.join(SRC, "hrca", "validation_policy.py"), encoding="utf-8"
        ) as fh:
            tree = ast.parse(fh.read())
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for forbidden in ("open", "exec", "eval", "compile", "__import__"):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, calls)


if __name__ == "__main__":
    unittest.main()
