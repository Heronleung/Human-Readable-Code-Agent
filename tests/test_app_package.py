"""Tests for the P4.3 document-driven app-package contract and validator."""

from __future__ import annotations

import copy
import unittest

from hrca import app_package


def _package(**overrides):
    package = app_package.quotation_reference_package()
    package.update(overrides)
    return package


class ReferencePackageTests(unittest.TestCase):
    def test_reference_package_is_valid(self):
        self.assertIsNone(app_package.validate_package(app_package.quotation_reference_package()))

    def test_reference_package_has_expected_shape(self):
        package = app_package.quotation_reference_package()
        self.assertEqual(package["runtime"], app_package.RUNNER_IDENTITY)
        self.assertEqual(package["handler"], "quotation_rules.evaluate")
        names = {f["name"] for f in package["form"]}
        self.assertEqual(names, {"subtotal", "member", "region"})
        result_names = {f["name"] for f in package["result"]}
        self.assertEqual(result_names, {"discount", "shipping_fee", "regional_fee", "total"})


class ValidatorTests(unittest.TestCase):
    def test_rejects_non_mapping(self):
        self.assertEqual(app_package.validate_package(None), app_package.REASON_NOT_MAPPING)
        self.assertEqual(app_package.validate_package([]), app_package.REASON_NOT_MAPPING)

    def test_rejects_oversized(self):
        package = _package(title="x" * (app_package.MAX_PACKAGE_BYTES + 10))
        self.assertEqual(app_package.validate_package(package), app_package.REASON_TOO_LARGE)

    def test_rejects_script_field(self):
        package = _package(script="import os; os.system('evil')")
        self.assertEqual(app_package.validate_package(package), app_package.REASON_UNKNOWN_KEYS)

    def test_rejects_html_field(self):
        package = _package(html="<script>alert(1)</script>")
        self.assertEqual(app_package.validate_package(package), app_package.REASON_UNKNOWN_KEYS)

    def test_rejects_command_field(self):
        package = _package(command="rm -rf /")
        self.assertEqual(app_package.validate_package(package), app_package.REASON_UNKNOWN_KEYS)

    def test_rejects_mount_field(self):
        package = _package(mounts=[{"src": "/", "dst": "/host"}])
        self.assertEqual(app_package.validate_package(package), app_package.REASON_UNKNOWN_KEYS)

    def test_rejects_environment_field(self):
        package = _package(environment={"PATH": "/evil"})
        self.assertEqual(app_package.validate_package(package), app_package.REASON_UNKNOWN_KEYS)

    def test_rejects_dependencies_field(self):
        package = _package(dependencies=["requests"])
        self.assertEqual(app_package.validate_package(package), app_package.REASON_UNKNOWN_KEYS)

    def test_rejects_runtime_mismatch(self):
        package = _package(runtime="other-runner:v9")
        self.assertEqual(app_package.validate_package(package), app_package.REASON_RUNTIME_MISMATCH)

    def test_rejects_wrong_schema_version(self):
        package = _package(schema_version="9.9.9")
        self.assertEqual(
            app_package.validate_package(package), app_package.REASON_UNSUPPORTED_SCHEMA
        )

    def test_rejects_unsupported_package_id(self):
        package = _package(package_id="other-package")
        self.assertEqual(
            app_package.validate_package(package), app_package.REASON_UNSUPPORTED_PACKAGE
        )

    def test_rejects_path_escaping_package_id(self):
        package = _package(package_id="../../etc/passwd")
        self.assertEqual(
            app_package.validate_package(package), app_package.REASON_UNSUPPORTED_PACKAGE
        )

    def test_rejects_script_like_handler(self):
        package = _package(handler="os.system")
        self.assertEqual(
            app_package.validate_package(package), app_package.REASON_UNSUPPORTED_HANDLER
        )

    def test_rejects_shell_metacharacter_handler(self):
        package = _package(handler="quotation_rules.evaluate; rm -rf /")
        self.assertEqual(
            app_package.validate_package(package), app_package.REASON_UNSUPPORTED_HANDLER
        )

    def test_rejects_expr_field_type(self):
        package = _package(form=[{"name": "x", "type": "expr", "expr": "1+1"}])
        self.assertEqual(app_package.validate_package(package), app_package.REASON_INVALID_FORM)

    def test_rejects_code_payload_on_field(self):
        form = copy.deepcopy(app_package.quotation_reference_package()["form"])
        form[0]["code"] = "os.system('x')"
        package = _package(form=form)
        self.assertEqual(app_package.validate_package(package), app_package.REASON_INVALID_FORM)


class FormInputTests(unittest.TestCase):
    def _valid_input(self):
        return {"subtotal": "200.00", "member": True, "region": "west"}

    def test_accepts_valid_input(self):
        self.assertIsNone(
            app_package.validate_form_input(
                app_package.quotation_reference_package(), self._valid_input()
            )
        )

    def test_accepts_numeric_subtotal(self):
        value = self._valid_input()
        value["subtotal"] = 200
        self.assertIsNone(
            app_package.validate_form_input(app_package.quotation_reference_package(), value)
        )

    def test_rejects_missing_required_field(self):
        value = self._valid_input()
        del value["member"]
        self.assertEqual(
            app_package.validate_form_input(app_package.quotation_reference_package(), value),
            "missing required field",
        )

    def test_rejects_negative_subtotal(self):
        value = self._valid_input()
        value["subtotal"] = "-1.00"
        self.assertEqual(
            app_package.validate_form_input(app_package.quotation_reference_package(), value),
            "value below minimum",
        )

    def test_rejects_blank_subtotal(self):
        value = self._valid_input()
        value["subtotal"] = ""
        self.assertEqual(
            app_package.validate_form_input(app_package.quotation_reference_package(), value),
            "invalid decimal",
        )

    def test_rejects_unknown_input_key(self):
        value = self._valid_input()
        value["_secret"] = "abc"
        self.assertEqual(
            app_package.validate_form_input(app_package.quotation_reference_package(), value),
            "input has unknown fields",
        )

    def test_rejects_invalid_choice(self):
        value = self._valid_input()
        value["region"] = "nope"
        self.assertEqual(
            app_package.validate_form_input(app_package.quotation_reference_package(), value),
            "invalid choice",
        )

    def test_rejects_non_boolean_member(self):
        value = self._valid_input()
        value["member"] = "yes"
        self.assertEqual(
            app_package.validate_form_input(app_package.quotation_reference_package(), value),
            "invalid boolean",
        )


class ResultValidationTests(unittest.TestCase):
    def test_accepts_valid_result(self):
        result = {"discount": "10.00", "shipping_fee": "0.00", "regional_fee": "0.00", "total": "190.00"}
        self.assertIsNone(
            app_package.validate_result(app_package.quotation_reference_package(), result)
        )

    def test_rejects_missing_result_field(self):
        result = {"discount": "10.00"}
        self.assertEqual(
            app_package.validate_result(app_package.quotation_reference_package(), result),
            "missing result field",
        )

    def test_rejects_unknown_result_field(self):
        result = {"discount": "10.00", "shipping_fee": "0.00", "regional_fee": "0.00", "total": "190.00", "_extra": "x"}
        self.assertEqual(
            app_package.validate_result(app_package.quotation_reference_package(), result),
            "result has unknown fields",
        )


if __name__ == "__main__":
    unittest.main()
