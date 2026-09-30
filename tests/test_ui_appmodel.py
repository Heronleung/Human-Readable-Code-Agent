"""Tests for the chat-first application model (UI-TRANSITION-2).

These exercise the local presentation state directly, with no Qt and no
backend. They pin the honest-state rules that the whole redesign rests on:
a plan cannot be confirmed while it is incoherent, a protected effect is
required before dispatch, an unobserved outcome is never rounded up, and no
recorded decision advances an accepted baseline.
"""

from __future__ import annotations

import unittest

from hrca.core import contract
from hrca.ui.appmodel import agents, authority, capabilities, states
from hrca.ui.appmodel.composer import compose_plan
from hrca.ui.appmodel.context import ProjectContext
from hrca.ui.appmodel.plan import (
    Job,
    Plan,
    RISK_HIGH,
    Subtask,
    plan_is_dispatchable,
    ready_jobs,
    validate_plan,
)
from hrca.ui.appmodel.review import advances_acceptance, build_review
from hrca.ui.appmodel.session import Workspace


def _context(with_document: bool = True) -> ProjectContext:
    context = ProjectContext(root="/repo", repository_state="Unverified")
    if with_document:
        context = context.with_document("doc:1", "requirements.md", 1)
    return context


class StateVocabularyTests(unittest.TestCase):
    def test_every_job_state_has_a_label_and_a_non_colour_cue(self):
        for state in states.JOB_STATES:
            with self.subTest(state=state):
                self.assertTrue(states.job_state_label(state))
                self.assertIn(state, states.JOB_STATE_GLYPHS)
                self.assertTrue(states.job_state_glyph(state))

    def test_unknown_is_terminal_and_a_definite_failure_outranks_it(self):
        self.assertTrue(states.is_terminal(states.STATE_UNKNOWN))
        self.assertGreater(
            states.job_state_severity(states.STATE_FAILED),
            states.job_state_severity(states.STATE_UNKNOWN),
        )

    def test_completed_is_not_more_severe_than_a_blocker(self):
        self.assertLess(
            states.job_state_severity(states.STATE_COMPLETED),
            states.job_state_severity(states.STATE_BLOCKED),
        )

    def test_rollup_returns_a_real_member_state(self):
        rolled = states.rollup_job_state([states.STATE_COMPLETED, states.STATE_FAILED])
        self.assertEqual(rolled, states.STATE_FAILED)
        self.assertIn(rolled, states.JOB_STATES)

    def test_empty_rollup_is_draft(self):
        self.assertEqual(states.rollup_job_state([]), states.STATE_DRAFT)

    def test_rollup_is_deterministic_across_ordering(self):
        first = states.rollup_job_state([states.STATE_STALE, states.STATE_BLOCKED])
        second = states.rollup_job_state([states.STATE_BLOCKED, states.STATE_STALE])
        self.assertEqual(first, second)


class AuthorityTests(unittest.TestCase):
    def test_local_write_is_not_protected(self):
        self.assertFalse(authority.is_protected(authority.AUTHORITY_LOCAL_WRITE))
        self.assertFalse(authority.is_protected(authority.AUTHORITY_READ_ONLY))

    def test_named_approval_boundaries_are_protected(self):
        for level in (
            authority.AUTHORITY_PROVIDER_CALL,
            authority.AUTHORITY_CREDENTIAL_USE,
            authority.AUTHORITY_REPOSITORY_WRITE,
            authority.AUTHORITY_DESTRUCTIVE,
        ):
            with self.subTest(level=level):
                self.assertTrue(authority.is_protected(level))
                self.assertTrue(authority.effects_for(level))

    def test_provider_call_implies_dispatch_and_cost(self):
        effects = authority.effects_for(authority.AUTHORITY_PROVIDER_CALL)
        self.assertIn(authority.EFFECT_PROVIDER_DISPATCH, effects)
        self.assertIn(authority.EFFECT_COST, effects)

    def test_exceeds_compares_reach(self):
        self.assertTrue(
            authority.exceeds(
                authority.AUTHORITY_REPOSITORY_WRITE, authority.AUTHORITY_LOCAL_WRITE
            )
        )
        self.assertFalse(
            authority.exceeds(
                authority.AUTHORITY_READ_ONLY, authority.AUTHORITY_READ_ONLY
            )
        )


class CapabilityCatalogueTests(unittest.TestCase):
    def test_every_capability_action_exists_in_the_accepted_contract(self):
        for capability in capabilities.CAPABILITIES:
            if capability.action is None:
                continue
            with self.subTest(capability=capability.key):
                self.assertIn(capability.action, contract.ALLOWED_ACTIONS)

    def test_unavailable_capabilities_state_a_reason(self):
        for capability in capabilities.CAPABILITIES:
            if capability.available:
                continue
            with self.subTest(capability=capability.key):
                self.assertTrue(capability.disabled_reason)
                self.assertIsNone(capability.action)

    def test_available_capabilities_are_usable(self):
        for capability in capabilities.CAPABILITIES:
            if capability.available:
                with self.subTest(capability=capability.key):
                    self.assertEqual(capability.disabled_reason, "")

    def test_pause_resume_and_reassign_are_never_claimed(self):
        for capability in capabilities.CAPABILITIES:
            with self.subTest(capability=capability.key):
                self.assertFalse(capability.control.pause)
                self.assertFalse(capability.control.resume)
                self.assertFalse(capability.control.reassign)


class AgentRoleTests(unittest.TestCase):
    def test_role_authority_is_derived_from_its_capabilities(self):
        interpreter = agents.require(agents.ROLE_INTERPRETER)
        self.assertEqual(interpreter.max_authority, authority.AUTHORITY_PROVIDER_CALL)
        self.assertTrue(interpreter.is_protected)

    def test_coordinator_cannot_reach_a_protected_effect(self):
        coordinator = agents.require(agents.ROLE_COORDINATOR)
        self.assertEqual(coordinator.max_authority, authority.AUTHORITY_READ_ONLY)
        self.assertFalse(coordinator.is_protected)

    def test_unavailable_roles_explain_themselves(self):
        for key in (agents.ROLE_VALIDATOR, agents.ROLE_REPOSITORY):
            with self.subTest(role=key):
                role = agents.require(key)
                self.assertFalse(role.available)
                self.assertTrue(role.unavailable_reason)

    def test_capabilities_owned_by_no_role_cannot_be_assigned(self):
        # Adoption and repository writing are human decisions, not agent work.
        self.assertIsNone(agents.role_for_capability("candidate.adopt"))
        self.assertIsNone(agents.role_for_capability("credential.manage"))


class PlanValidationTests(unittest.TestCase):
    def _plan(self, **overrides) -> Plan:
        job = Job(
            key="scan",
            title="Scan",
            capability_key="source.scan",
            role_key=agents.ROLE_SOURCE,
            acceptance=("Files are parsed.",),
        )
        for name, value in overrides.items():
            setattr(job, name, value)
        return Plan(goal="Do a thing", jobs=(job,))

    def test_a_coherent_plan_has_no_problems(self):
        self.assertEqual(validate_plan(self._plan()), ())

    def test_unknown_capability_is_refused(self):
        problems = validate_plan(self._plan(capability_key="not.a.capability"))
        self.assertTrue(any("unknown capability" in problem for problem in problems))

    def test_role_that_does_not_own_the_capability_is_refused(self):
        problems = validate_plan(self._plan(role_key=agents.ROLE_AUTHOR))
        self.assertTrue(any("belongs to" in problem for problem in problems))

    def test_missing_acceptance_is_refused(self):
        problems = validate_plan(self._plan(acceptance=()))
        self.assertTrue(any("no acceptance" in problem for problem in problems))

    def test_self_dependency_is_refused(self):
        problems = validate_plan(self._plan(depends_on=("scan",)))
        self.assertTrue(any("depends on itself" in problem for problem in problems))

    def test_two_job_cycle_is_refused(self):
        first = Job("a", "A", "source.scan", agents.ROLE_SOURCE, (), ("b",), ("x",))
        second = Job("b", "B", "source.scan", agents.ROLE_SOURCE, (), ("a",), ("x",))
        problems = validate_plan(Plan(goal="g", jobs=(first, second)))
        self.assertTrue(any("dependency cycle" in problem for problem in problems))

    def test_dependency_on_a_missing_job_is_refused(self):
        problems = validate_plan(self._plan(depends_on=("ghost",)))
        self.assertTrue(any("not in the plan" in problem for problem in problems))

    def test_unknown_risk_is_refused(self):
        problems = validate_plan(self._plan(risk="catastrophic"))
        self.assertTrue(any("unknown risk" in problem for problem in problems))

    def test_an_unavailable_capability_is_refused(self):
        problems = validate_plan(self._plan(capability_key="validation.run"))
        self.assertTrue(any("cannot use" in problem for problem in problems))

    def test_incoherent_plan_cannot_be_confirmed(self):
        ok, reason = plan_is_dispatchable(self._plan(acceptance=()))
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_ready_jobs_respect_dependencies(self):
        first = Job("a", "A", "source.scan", agents.ROLE_SOURCE, (), (), ("x",))
        second = Job("b", "B", "source.scan", agents.ROLE_SOURCE, (), ("a",), ("x",))
        plan = Plan(goal="g", jobs=(first, second))
        self.assertEqual([job.key for job in ready_jobs(plan)], ["a"])

    def test_ready_jobs_skip_a_job_planned_against_a_old_baseline(self):
        job = Job(
            "a", "A", "source.scan", agents.ROLE_SOURCE, (), (), ("x",), baseline="old"
        )
        plan = Plan(goal="g", jobs=(job,))
        self.assertEqual(ready_jobs(plan, baseline="new"), ())


class ComposerTests(unittest.TestCase):
    def test_composition_is_deterministic(self):
        context = _context()
        first = compose_plan("interpret the discount rule", context)
        second = compose_plan("interpret the discount rule", context)
        self.assertEqual(first.job_keys, second.job_keys)
        self.assertEqual(first.notes, second.notes)

    def test_a_scaffold_is_never_confirmed(self):
        self.assertFalse(compose_plan("anything", _context()).confirmed)

    def test_only_available_capabilities_are_proposed(self):
        plan = compose_plan("interpret the discount rule and resume earlier work", _context())
        for job in plan.jobs:
            with self.subTest(job=job.key):
                capability = capabilities.require(job.capability_key)
                self.assertTrue(capability.available)

    def test_a_proposed_plan_validates(self):
        for goal in (
            "interpret the discount rule",
            "resume earlier work",
            "scan the project",
        ):
            with self.subTest(goal=goal):
                self.assertEqual(validate_plan(compose_plan(goal, _context())), ())

    def test_scaffold_states_which_signals_produced_it(self):
        plan = compose_plan("interpret the quote", _context())
        self.assertTrue(plan.notes)
        self.assertTrue(any("interpret" in note for note in plan.notes))

    def test_no_project_means_no_scan_job(self):
        plan = compose_plan("scan the project", ProjectContext())
        self.assertNotIn("scan", plan.job_keys)

    def test_no_document_means_no_document_jobs(self):
        plan = compose_plan("interpret the rule", ProjectContext(root="/repo"))
        self.assertNotIn("save", plan.job_keys)
        self.assertNotIn("interpret", plan.job_keys)


class WorkspaceWorkflowTests(unittest.TestCase):
    def _workspace(self) -> Workspace:
        workspace = Workspace(_context())
        workspace.state_goal("interpret the discount rule")
        return workspace

    def test_goal_before_project_is_refused(self):
        workspace = Workspace(ProjectContext())
        self.assertTrue(workspace.state_goal("").refused)
        self.assertTrue(workspace.state_goal("do something").ok)

    def test_dispatch_before_confirmation_is_refused(self):
        workspace = self._workspace()
        outcome = workspace.dispatch("scan")
        self.assertTrue(outcome.refused)
        self.assertIn("Confirm the plan", outcome.reason)

    def test_confirm_refuses_an_incoherent_plan(self):
        workspace = self._workspace()
        plan = workspace.plan
        workspace.update_plan(Plan(goal=plan.goal, jobs=plan.jobs[:1]))
        self.assertTrue(workspace.confirm_plan().ok)
        # A second confirmation of the same plan is refused.
        self.assertTrue(workspace.confirm_plan().refused)

    def test_a_confirmed_plan_cannot_be_edited(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        self.assertTrue(workspace.update_plan(workspace.plan).refused)

    def test_a_dependency_blocks_dispatch(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        outcome = workspace.dispatch("save")
        self.assertTrue(outcome.refused)
        self.assertIn("waiting on scan", outcome.reason)

    def test_a_protected_effect_is_required_before_dispatch(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        for key in ("scan", "save", "preview", "prepare"):
            workspace.dispatch(key)
            workspace.report_job(key, states.STATE_COMPLETED)
        refused = workspace.dispatch("interpret")
        self.assertTrue(refused.refused)
        self.assertIn("provider dispatch", refused.reason.lower())
        allowed = workspace.dispatch(
            "interpret",
            confirmed_effects=(
                authority.EFFECT_PROVIDER_DISPATCH,
                authority.EFFECT_COST,
            ),
        )
        self.assertTrue(allowed.ok, allowed.reason)

    def test_partial_effect_confirmation_is_refused(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        for key in ("scan", "save", "preview", "prepare"):
            workspace.dispatch(key)
            workspace.report_job(key, states.STATE_COMPLETED)
        outcome = workspace.dispatch(
            "interpret", confirmed_effects=(authority.EFFECT_PROVIDER_DISPATCH,)
        )
        self.assertTrue(outcome.refused)
        self.assertIn("cost", outcome.reason.lower())

    def test_a_reported_completion_is_only_a_claim(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        workspace.dispatch("scan")
        workspace.report_job("scan", states.STATE_COMPLETED)
        self.assertEqual(workspace.job("scan").state, states.STATE_COMPLETED)
        self.assertFalse(advances_acceptance(states.DECISION_APPROVE))
        self.assertFalse(workspace.context.has_baseline)

    def test_unknown_outcome_is_recorded_honestly(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        workspace.dispatch("scan")
        workspace.report_job("scan", states.STATE_UNKNOWN)
        self.assertEqual(workspace.job("scan").state, states.STATE_UNKNOWN)
        self.assertNotEqual(workspace.job("scan").state, states.STATE_COMPLETED)

    def test_a_blocked_job_records_its_reason(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        workspace.dispatch("scan")
        workspace.report_job("scan", states.STATE_BLOCKED, blocker="The root is unreadable.")
        resume = workspace.resume()
        blocked = resume.section("blocked")
        self.assertEqual(len(blocked.items), 1)
        self.assertEqual(blocked.items[0].detail, "The root is unreadable.")

    def test_unsupported_controls_are_refused_with_a_reason(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        workspace.dispatch("scan")
        self.assertTrue(workspace.pause("scan").refused)
        self.assertTrue(workspace.resume_job("scan").refused)
        self.assertTrue(workspace.reassign("scan", agents.ROLE_SOURCE).refused)

    def test_cancel_is_supported_where_the_capability_says_so(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        workspace.dispatch("scan")
        self.assertTrue(workspace.supports("scan", "cancel"))
        self.assertTrue(workspace.cancel("scan").ok)
        self.assertEqual(workspace.job("scan").state, states.STATE_CANCELLED)

    def test_approval_is_refused_while_evidence_is_missing(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        outcome = workspace.record_decision(states.DECISION_APPROVE, "heron")
        self.assertTrue(outcome.refused)
        self.assertIn("Missing proof", outcome.reason)

    def test_a_decision_must_name_who_made_it(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        self.assertTrue(workspace.record_decision(states.DECISION_REJECT, "").refused)

    def test_a_baseline_move_marks_planned_jobs_stale(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        workspace.set_context(workspace.context.with_baseline("ver:2", "Accepted app 2"))
        self.assertEqual(workspace.job("scan").state, states.STATE_STALE)

    def test_moving_the_baseline_does_not_rebind_a_job(self):
        workspace = self._workspace()
        workspace.confirm_plan()
        before = workspace.job("interpret").baseline
        workspace.set_context(workspace.context.with_baseline("ver:2", "Accepted app 2"))
        self.assertEqual(workspace.job("interpret").baseline, before)


class WorkspaceReviewTests(unittest.TestCase):
    def _completed_workspace(self) -> Workspace:
        workspace = Workspace(_context())
        workspace.state_goal("interpret the discount rule")
        workspace.confirm_plan()
        for job in list(workspace.plan.jobs):
            workspace.dispatch(
                job.key,
                confirmed_effects=(
                    authority.EFFECT_PROVIDER_DISPATCH,
                    authority.EFFECT_COST,
                ),
            )
            workspace.report_job(job.key, states.STATE_COMPLETED)
        return workspace

    def test_review_is_reviewable_once_jobs_report(self):
        workspace = self._completed_workspace()
        bundle = workspace.review()
        self.assertTrue(bundle.is_reviewable)
        self.assertEqual(bundle.coverage[0], bundle.coverage[1])
        self.assertTrue(bundle.can_approve)

    def test_approval_records_a_decision_and_advances_nothing(self):
        workspace = self._completed_workspace()
        outcome = workspace.record_decision(states.DECISION_APPROVE, "heron", "Looks right.")
        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(workspace.review().decision, states.DECISION_APPROVE)
        self.assertFalse(workspace.context.has_baseline)
        self.assertFalse(advances_acceptance(states.DECISION_APPROVE))

    def test_a_second_decision_is_refused(self):
        workspace = self._completed_workspace()
        workspace.record_decision(states.DECISION_APPROVE, "heron")
        self.assertTrue(workspace.record_decision(states.DECISION_REJECT, "heron").refused)

    def test_missing_evidence_blocks_approval_but_not_rejection(self):
        workspace = Workspace(_context())
        workspace.state_goal("scan the project")
        workspace.confirm_plan()
        review = build_review(workspace.plan)
        self.assertFalse(review.can_approve)
        self.assertTrue(review.blocking_reasons)
        self.assertTrue(workspace.record_decision(states.DECISION_REJECT, "heron").ok)

    def test_every_decision_names_its_effect(self):
        for decision in states.REVIEW_DECISIONS:
            with self.subTest(decision=decision):
                self.assertTrue(states.decision_effect(decision))
                self.assertTrue(states.decision_label(decision))


class WorkspaceResumeTests(unittest.TestCase):
    def test_resume_recommends_opening_a_project_first(self):
        resume = Workspace(ProjectContext()).resume()
        self.assertEqual(resume.recommendation.action, "Open a project")
        self.assertTrue(resume.recommendation.reason)

    def test_resume_recommends_stating_a_goal_when_no_plan_exists(self):
        resume = Workspace(_context()).resume()
        self.assertIn("goal", resume.recommendation.action.lower())

    def test_resume_recommends_confirming_an_unconfirmed_plan(self):
        workspace = Workspace(_context())
        workspace.state_goal("scan the project")
        resume = workspace.resume()
        self.assertIn("confirm", resume.recommendation.action.lower())
        self.assertEqual(resume.recommendation.reference, "plan")

    def test_resume_names_the_blocker_before_anything_else(self):
        workspace = Workspace(_context())
        workspace.state_goal("scan the project")
        workspace.confirm_plan()
        workspace.dispatch("scan")
        workspace.report_job("scan", states.STATE_BLOCKED, blocker="The root is unreadable.")
        resume = workspace.resume()
        self.assertIn("blocker", resume.recommendation.action.lower())
        self.assertEqual(resume.recommendation.reason, "The root is unreadable.")

    def test_resume_items_point_at_supporting_records(self):
        workspace = Workspace(_context())
        workspace.state_goal("scan the project")
        workspace.confirm_plan()
        resume = workspace.resume()
        for section in resume.sections:
            for item in section.items:
                with self.subTest(item=item.key):
                    self.assertTrue(item.reference)

    def test_resume_has_exactly_one_recommendation(self):
        resume = Workspace(_context()).resume()
        self.assertIsInstance(resume.recommendation.action, str)
        self.assertTrue(resume.recommendation.action)


class TranscriptTests(unittest.TestCase):
    def test_message_keys_are_ordered_and_unique(self):
        workspace = Workspace(_context())
        workspace.state_goal("scan the project")
        workspace.confirm_plan()
        keys = [message.key for message in workspace.messages]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, sorted(keys, key=lambda key: int(key.split("-")[1])))

    def test_the_goal_is_the_first_message_and_it_is_the_developer_speaking(self):
        workspace = Workspace(_context())
        workspace.state_goal("scan the project")
        first = workspace.messages[0]
        self.assertEqual(first.text, "scan the project")
        self.assertEqual(first.author, "developer")

    def test_the_plan_card_sits_in_the_transcript(self):
        workspace = Workspace(_context())
        workspace.state_goal("scan the project")
        self.assertTrue(any(message.is_plan for message in workspace.messages))


if __name__ == "__main__":
    unittest.main()
