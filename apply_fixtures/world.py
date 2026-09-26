"""Disposable apply world for the source-apply oracle (fixture harness).

This is test support, not shipped product code. It exists so the three focused
test modules share one builder and one mutation vocabulary, and so the oracle in
``apply_fixtures/manifest.json`` can name a mutation by intent rather than
describe one in prose.

Every world is built under a caller-supplied disposable root: ``build`` copies
the fixture project into it, materialises a candidate root whose *name* is
derived from its own identity, and returns the evidence and receipt mappings the
coordinator binds. Nothing here reads or writes the product repository, the
P5-X custody root, or any path outside the disposable root it is given.

Why the candidate root is assembled here rather than by ``candidate.build_candidate``
---------------------------------------------------------------------------------

A candidate manifest records the workspace it was bound to, and that workspace
identity is the SHA-256 of the *canonical root path* — which is a temp directory
at test time. So the identity cannot be a literal in a checked-in fixture. This
builder therefore assembles a manifest whose every field has the shape the P5.4
schema requires and which the product's own ``validate_candidate_manifest`` and
``validation.verify_candidate`` accept: that acceptance is the property the
coordinator depends on, and the tests assert it directly rather than assuming it.

The ``diff`` on the fixture operation is a minimal placeholder. Its only
asserted property is truthiness, which is exactly what the P5.4 manifest schema
requires of it; the fixture makes no claim about diff rendering.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from typing import Any, Dict, List, Optional

from hrca.authoring import candidate, candidate_edit, source_apply, validation
from hrca.core.identity import sha256_hex, workspace_id_for

# -- the fixed fixture vocabulary ------------------------------------------

PREDECESSOR_TEXT = (
    '"""Fixture greeting helpers."""\n'
    "\n"
    "\n"
    "def greeting(name):\n"
    '    """Return a greeting for ``name``."""\n'
    '    return "hello " + name\n'
)

CANDIDATE_TEXT = (
    '"""Fixture greeting helpers."""\n'
    "\n"
    "\n"
    "def greeting(name):\n"
    '    """Return a greeting for ``name``."""\n'
    '    return "hello, " + name\n'
)

TARGET_PATH = "greeting.py"

INTENT_DELTA_ID = "intent:" + "11" * 32
PROPOSAL_ID = "impact:" + "22" * 32
BINDING_FINGERPRINT = "bind:" + "33" * 32
PLAN_ID = "plan:" + "44" * 32
POLICY_VERSION = "1.0.0"
RESULT_ID = "result:" + "55" * 32
RUN_ID = "run:" + "66" * 32
RECORD_ID = "fixture-record-1"
BASELINE_FINGERPRINT = "77" * 32
SCAN_GENERATION = 1
SCANNER_SCHEMA_VERSION = "1.1.0"
GRAMMAR = {"implementation": "cpython", "version": "3.11"}

OTHER_INIT = '"""Fixture other package."""\n'
OTHER_FORMATTING = '"""Fixture other formatting."""\n'


def _project_files() -> Dict[str, str]:
    return {
        "__init__.py": '"""Fixture apply project."""\n',
        "formatting.py": (
            '"""Fixture formatting helpers."""\n'
            "\n"
            "\n"
            "def shout(text):\n"
            '    """Return ``text`` upper-cased."""\n'
            "    return text.upper()\n"
        ),
        "greeting.py": PREDECESSOR_TEXT,
    }


class World:
    """One disposable apply world and every mapping the coordinator binds."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.project = os.path.join(root, "project")
        self.other = os.path.join(root, "other")
        self.custody = os.path.join(root, "custody")
        self.candidate_root = ""
        self.evidence: Dict[str, Any] = {}
        self.receipt: Dict[str, Any] = {}
        self.receipt_bytes: bytes = b""
        self.apply_root = self.project
        self.predecessor_sha256 = ""
        self.candidate_sha256 = ""

    # -- convenience -------------------------------------------------------

    def target(self, root: Optional[str] = None) -> str:
        return os.path.join(root or self.apply_root, TARGET_PATH)

    def pre_image_path(self) -> str:
        return os.path.join(
            self.custody,
            "source-recovery",
            "%s.%s" % (TARGET_PATH, self.predecessor_sha256),
        )

    def record_dir(self) -> str:
        return os.path.join(self.custody, "apply-records")

    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()


def _write(path: str, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)


def build(root: str) -> World:
    """Materialise a complete disposable world beneath ``root``."""
    os.makedirs(root, exist_ok=True)
    world = World(root)

    for name, text in _project_files().items():
        os.makedirs(world.project, exist_ok=True)
        _write(os.path.join(world.project, name), text.encode("utf-8"))

    os.makedirs(world.other, exist_ok=True)
    _write(os.path.join(world.other, "__init__.py"), OTHER_INIT.encode("utf-8"))
    _write(os.path.join(world.other, "formatting.py"), OTHER_FORMATTING.encode("utf-8"))
    # The same relative path and the *same bytes* as the accepted project, so a
    # substitution test proves the root identity refuses it rather than content.
    _write(os.path.join(world.other, TARGET_PATH), PREDECESSOR_TEXT.encode("utf-8"))

    os.makedirs(world.custody, exist_ok=True)

    canonical = os.path.realpath(os.path.abspath(world.project))
    workspace_id = workspace_id_for(canonical)
    predecessor_bytes = PREDECESSOR_TEXT.encode("utf-8")
    candidate_bytes = CANDIDATE_TEXT.encode("utf-8")
    world.predecessor_sha256 = sha256_hex(predecessor_bytes)
    world.candidate_sha256 = sha256_hex(candidate_bytes)

    raw_edit: Dict[str, Any] = {
        "schema_version": candidate_edit.CANDIDATE_EDIT_SCHEMA_VERSION,
        "generator": candidate_edit.CANDIDATE_EDIT_GENERATOR,
        "intent_delta_id": INTENT_DELTA_ID,
        "proposal_id": PROPOSAL_ID,
        "binding_fingerprint": BINDING_FINGERPRINT,
        "baseline": {
            "workspace_id": workspace_id,
            "scan_generation": SCAN_GENERATION,
            "baseline_fingerprint": BASELINE_FINGERPRINT,
            "scanner_schema_version": SCANNER_SCHEMA_VERSION,
            "grammar": dict(GRAMMAR),
        },
        "operations": [
            {
                "op": candidate_edit.OP_REPLACE_FILE,
                "path": TARGET_PATH,
                "expected_sha256": world.predecessor_sha256,
                "text": CANDIDATE_TEXT,
            }
        ],
    }
    edit, reason = candidate_edit.build_edit(raw_edit)
    if reason is not None:  # pragma: no cover - the fixture is fixed
        raise AssertionError("fixture edit refused: %s" % reason)

    manifest: Dict[str, Any] = {
        "schema_version": candidate.CANDIDATE_SCHEMA_VERSION,
        "generator": candidate.CANDIDATE_GENERATOR,
        "candidate_id": "",
        "state": candidate.STATE_CANDIDATE_READY,
        "edit_id": edit["edit_id"],
        "intent_delta_id": INTENT_DELTA_ID,
        "proposal_id": PROPOSAL_ID,
        "binding_fingerprint": BINDING_FINGERPRINT,
        "baseline": dict(edit["baseline"]),
        "operations": [
            {
                "op": candidate_edit.OP_REPLACE_FILE,
                "path": TARGET_PATH,
                "before_sha256": world.predecessor_sha256,
                "before_bytes": len(predecessor_bytes),
                "after_sha256": world.candidate_sha256,
                "after_bytes": len(candidate_bytes),
                "before_final_newline": True,
                "after_final_newline": True,
                "diff": ["-    return \"hello \" + name", '+    return "hello, " + name'],
            }
        ],
        "files": [
            {
                "path": TARGET_PATH,
                "sha256": world.candidate_sha256,
                "bytes": len(candidate_bytes),
            }
        ],
        "write_scope": {
            "candidate_root_only": True,
            "accepted_repository": False,
            "git_metadata": False,
        },
        "limitations": ["fixture candidate: materialised only"],
    }
    manifest["candidate_id"] = candidate.candidate_id_for(manifest)
    root_name = candidate.candidate_root_name(manifest["candidate_id"])
    world.candidate_root = os.path.join(root, root_name)
    os.makedirs(os.path.join(world.candidate_root, "files"))
    manifest_bytes = (candidate.dumps(manifest) + "\n").encode("utf-8")
    _write(os.path.join(world.candidate_root, "candidate.json"), manifest_bytes)
    _write(os.path.join(world.candidate_root, "files", TARGET_PATH), candidate_bytes)

    review: Dict[str, Any] = {
        "schema_version": candidate.CANDIDATE_SCHEMA_VERSION,
        "generator": candidate.CANDIDATE_REVIEW_GENERATOR,
        "state": candidate.STATE_CANDIDATE_READY,
        "candidate_id": manifest["candidate_id"],
        "candidate_root_name": root_name,
        "executable": False,
        "applied": False,
        "advisory": True,
        "validated": False,
        "approved": False,
        "adopted": False,
        "reason_code": candidate.REASON_READY,
        "reason": "fixture candidate materialised",
        "binding": {
            "edit_id": edit["edit_id"],
            "proposal_id": PROPOSAL_ID,
            "binding_fingerprint": BINDING_FINGERPRINT,
        },
        "operations": [
            {
                "op": candidate_edit.OP_REPLACE_FILE,
                "path": TARGET_PATH,
                "after_sha256": world.candidate_sha256,
                "after_bytes": len(candidate_bytes),
            }
        ],
        "mutation_surface": {"accepted_source": False, "twin_state": False},
        "risks": [],
    }
    review_sha256 = sha256_hex(validation.dumps(review).encode("utf-8"))
    manifest_sha256 = sha256_hex(manifest_bytes)

    # The owner's verifier is invoked here, in the harness, exactly as the
    # adapter invokes it in the product: the coordinator binds the verdict and
    # never imports the module that owns it.
    binding, binding_reason = validation.verify_candidate(world.candidate_root, review)
    if binding_reason is not None:  # pragma: no cover - the fixture is fixed
        raise AssertionError("fixture candidate refused: %s" % binding_reason)

    world.evidence = {
        "work_package": {"run_id": RUN_ID, "record_id": RECORD_ID},
        "change": {
            "intent_delta_id": INTENT_DELTA_ID,
            "proposal_id": PROPOSAL_ID,
            "edit_id": edit["edit_id"],
            "candidate_id": manifest["candidate_id"],
            "binding_fingerprint": BINDING_FINGERPRINT,
            "plan_id": PLAN_ID,
            "policy_version": POLICY_VERSION,
        },
        "validation": {
            "result_id": RESULT_ID,
            "plan_id": PLAN_ID,
            "candidate_id": manifest["candidate_id"],
            "policy_version": POLICY_VERSION,
            "state": validation.STATE_PASSED,
            "evidence_complete": True,
        },
        "accepted_revision": {
            "workspace_id": workspace_id,
            "scan_generation": SCAN_GENERATION,
            "baseline_fingerprint": BASELINE_FINGERPRINT,
        },
        "target": {
            "path": TARGET_PATH,
            "predecessor_sha256": world.predecessor_sha256,
            "candidate_sha256": world.candidate_sha256,
            "manifest_sha256": manifest_sha256,
            "review_sha256": review_sha256,
        },
        "candidate": {
            "root": world.candidate_root,
            "manifest": manifest,
            "review": review,
            "binding": binding,
        },
        "edit": edit,
        "proposal": {
            "proposal_id": PROPOSAL_ID,
            "binding": {"binding_fingerprint": BINDING_FINGERPRINT},
            "target_scope": {
                "targets": [{"path": TARGET_PATH, "artifact_id": "artifact:file:greeting.py"}]
            },
            "evidence_bindings": [{"kind": "scanner_file", "id": TARGET_PATH}],
            "affected_facts": [],
        },
    }

    world.receipt = {
        "actor": "fixture-client",
        "decision": "accepted",
        "decided_at": "2026-09-27T00:00:00Z",
        "candidate_id": manifest["candidate_id"],
        "target_path": TARGET_PATH,
        "predecessor_sha256": world.predecessor_sha256,
        "candidate_sha256": world.candidate_sha256,
        "manifest_sha256": manifest_sha256,
        "review_sha256": review_sha256,
        "binding_fingerprint": BINDING_FINGERPRINT,
        "workspace_id": workspace_id,
        "scan_generation": SCAN_GENERATION,
        "baseline_fingerprint": BASELINE_FINGERPRINT,
        "validation_result_id": RESULT_ID,
    }
    world.receipt_bytes = (json.dumps(world.receipt, indent=1) + "\n").encode("utf-8")
    return world


def write_receipt(world: World, destination: str) -> str:
    """Write the world's receipt to a file and return the path."""
    with open(destination, "wb") as handle:
        handle.write(world.receipt_bytes)
    return destination


# -- the mutation vocabulary ----------------------------------------------

MUTATIONS: Dict[str, str] = {
    "drop_evidence_group": "remove one evidence group",
    "set_evidence": "set one dotted field inside the evidence",
    "drop_evidence_field": "remove one dotted field from the evidence",
    "replace_evidence": "replace the whole evidence with a literal",
    "set_receipt": "set one field on the receipt mapping",
    "drop_receipt_field": "remove one field from the receipt mapping",
    "reject_receipt": "record a rejection decision on the receipt",
    "rewrite_target": "change the live predecessor bytes",
    "rewrite_candidate_file": "tamper the isolated candidate file",
    "rewrite_edit_text": "change the bound edit's replacement text",
    "point_root_at_other": "aim the apply root at the other directory",
    "point_root_at_missing": "aim the apply root at a path that is not a directory",
    "move_project_root": "rename the accepted project directory",
    "make_root_a_git_tree": "place a .git entry directly beneath the apply root",
    "widen_path_absolute": "aim the bound path at an absolute path",
    "unauthorized_path": "aim the bound path at a path the proposal never scoped",
    "symlink_target": "replace the target with a symbolic link",
    "hardlink_target": "give the target a second hard link",
    "replace_target_with_directory": "make the target a directory",
    "remove_target": "delete the target",
    "binary_target": "make the target non-UTF-8 content",
    "pre_create_recovery_path": "occupy the coordinator's recovery path",
    "custody_inside_apply_root": "place the custody base inside the apply root",
    "record_claims_application": "pre-write the record claiming a target write",
    # Applied by the runner around the call, not by this module.
    "patch_replace": "replace the atomic replacement with a stand-in outcome",
    "drop_receipt": "supply no approval receipt at all",
}

# Kinds the runner applies to the call rather than to the world: a stand-in
# replacement, and the absence of a receipt. Named here so the vocabulary stays
# in one place, and so an unknown kind fails loudly everywhere else.
RUNNER_MUTATIONS = frozenset({"patch_replace", "drop_receipt"})


def _resign_edit(world: World) -> None:
    """Re-derive the bound edit's content-addressed id after an edit mutation.

    A mutation that changed the edit without re-signing it would leave an edit
    whose id does not match its own content — which the coordinator refuses as an
    invalid edit, correctly, and before any content comparison. Re-signing keeps
    the edit *valid* so the case tests the agreement it means to test.
    """
    edit = world.evidence["edit"]
    edit["edit_id"] = candidate_edit.edit_id_for(edit)


def _at(evidence: Dict[str, Any], dotted: str) -> Dict[str, Any]:
    node: Any = evidence
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[part]
    return node


def apply_mutation(world: World, mutation: Dict[str, Any]) -> None:
    """Apply one named mutation to a world, or fail loudly on an unknown kind."""
    kind = mutation.get("kind")
    if kind not in MUTATIONS:
        raise AssertionError("unknown fixture mutation: %r" % (kind,))

    if kind in RUNNER_MUTATIONS:
        # The runner turns this into a change to the call, not to the world.
        return
    if kind == "drop_evidence_group":
        world.evidence.pop(mutation["group"], None)
    elif kind == "replace_evidence":
        world.evidence = mutation["value"]
    elif kind == "set_evidence":
        _at(world.evidence, mutation["path"])[mutation["path"].split(".")[-1]] = mutation["value"]
    elif kind == "drop_evidence_field":
        node = _at(world.evidence, mutation["path"])
        node.pop(mutation["path"].split(".")[-1], None)
    elif kind == "set_receipt":
        world.receipt[mutation["field"]] = mutation["value"]
    elif kind == "drop_receipt_field":
        world.receipt.pop(mutation["field"], None)
    elif kind == "reject_receipt":
        world.receipt["decision"] = "rejected"
    elif kind == "rewrite_target":
        target = world.target()
        os.chmod(target, stat.S_IWUSR | stat.S_IRUSR)
        with open(target, "wb") as handle:
            handle.write(mutation.get("text", PREDECESSOR_TEXT + "# drifted\n").encode("utf-8"))
    elif kind == "rewrite_candidate_file":
        with open(os.path.join(world.candidate_root, "files", TARGET_PATH), "wb") as handle:
            handle.write(CANDIDATE_TEXT.encode("utf-8") + b"# tampered\n")
    elif kind == "rewrite_edit_text":
        world.evidence["edit"]["operations"][0]["text"] = CANDIDATE_TEXT + "# other\n"
        _resign_edit(world)
    elif kind == "point_root_at_other":
        world.apply_root = world.other
    elif kind == "move_project_root":
        moved = world.project + "-moved"
        os.rename(world.project, moved)
        world.project = moved
        world.apply_root = moved
    elif kind == "symlink_target":
        target = world.target()
        os.remove(target)
        os.symlink("formatting.py", target)
    elif kind == "hardlink_target":
        os.link(world.target(), os.path.join(world.project, "greeting_link.py"))
    elif kind == "replace_target_with_directory":
        target = world.target()
        os.remove(target)
        os.makedirs(target)
    elif kind == "point_root_at_missing":
        world.apply_root = os.path.join(world.root, "not-a-directory")
    elif kind == "make_root_a_git_tree":
        os.makedirs(os.path.join(world.project, ".git"), exist_ok=True)
    elif kind == "widen_path_absolute":
        world.evidence["edit"]["operations"][0]["path"] = "/tmp/greeting.py"
        world.evidence["target"]["path"] = "/tmp/greeting.py"
        _resign_edit(world)
    elif kind == "unauthorized_path":
        world.evidence["edit"]["operations"][0]["path"] = "formatting.py"
        world.evidence["target"]["path"] = "formatting.py"
        _resign_edit(world)
    elif kind == "remove_target":
        os.remove(world.target())
    elif kind == "binary_target":
        with open(world.target(), "wb") as handle:
            handle.write(b"\xff\xfe\x00\x01 not text\n")
    elif kind == "pre_create_recovery_path":
        os.makedirs(os.path.dirname(world.pre_image_path()), exist_ok=True)
        with open(world.pre_image_path(), "wb") as handle:
            handle.write(b"an unrelated artifact\n")
    elif kind == "custody_inside_apply_root":
        world.custody = os.path.join(world.project, "custody")
        os.makedirs(world.custody, exist_ok=True)
    elif kind == "record_claims_application":
        canonical, _raw = source_apply.receipt_digests(world.receipt)
        apply_id = source_apply.apply_id_for(world.evidence, canonical)
        directory = world.record_dir()
        os.makedirs(directory, exist_ok=True)
        prior = {
            "schema_version": source_apply.SCHEMA_VERSION,
            "write_surface": {"target_written": True},
        }
        with open(
            os.path.join(directory, apply_id.split(":", 1)[1] + ".json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(prior, handle)
    else:  # pragma: no cover - MUTATIONS is exhaustive
        raise AssertionError("unhandled fixture mutation: %r" % (kind,))


def target_sha256(world: World, root: Optional[str] = None) -> Optional[str]:
    try:
        return sha256_hex(world.read(world.target(root)))
    except OSError:
        return None


__all__: List[str] = [
    "World",
    "build",
    "write_receipt",
    "apply_mutation",
    "target_sha256",
    "MUTATIONS",
    "TARGET_PATH",
    "PREDECESSOR_TEXT",
    "CANDIDATE_TEXT",
]
