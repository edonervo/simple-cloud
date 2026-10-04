"""Tests for cloudsync.guards — table-driven, one case per refusal rule.

No fake rclone is involved: `evaluate` is a pure function of `Evidence`, so each guardrail is
exercised by stating the facts a dry run would have produced. That is deliberate — the guardrails
are the thing that stops data being lost, and they should be testable without standing up a
simulation of rclone first.
"""

import unittest

from cloudsync import guards
from cloudsync.guards import evaluate


def evidence(**overrides):
    """Evidence for a healthy mirror plan, with only the varied fields stated."""
    return guards.evidence_for(
        type(
            "StubPair",
            (),
            {
                "name": "documents",
                "local": "/mnt/c/edo/Documents",
                "remote": "edo-remote:Documents",
                "mode": "mirror",
                "max_delete": 25,
                "max_shrink_percent": 10,
                "min_files": 1,
                "protect": (),
                "approve_deletes": "always",
            },
        )(),
        **overrides
    )


def rules(finding_list):
    return sorted(finding.rule for finding in finding_list)


class CleanPlanTests(unittest.TestCase):
    def test_a_plan_with_nothing_to_do_is_clean(self):
        self.assertEqual(evaluate(evidence(transfers=3)), [])

    def test_a_transfer_with_no_deletions_needs_no_approval(self):
        # R12 gates deletions, not writes. A run that only uploads new files must not hold.
        sources = tuple(f"f{i}" for i in range(100))

        found = evaluate(evidence(transfers=100, source_files=sources))

        self.assertEqual(found, [])


class FatalTests(unittest.TestCase):
    def test_a_fatal_dry_run_is_never_gated_and_returns_immediately(self):
        # W6: the stats of an aborted run describe a partial run, so every other number here is
        # untrustworthy. Only the fatal finding is reported, so nothing else can be acted on.
        found = evaluate(
            evidence(fatal=True, errors=5, deletes=("a",), source_files=())
        )

        self.assertEqual(rules(found), ["W6"])


class ErrorAndSourceTests(unittest.TestCase):
    def test_an_rclone_error_holds(self):
        found = evaluate(evidence(errors=2))

        self.assertEqual(rules(found), ["W1"])
        self.assertIn("2 error", str(found[0]))

    def test_an_empty_source_holds(self):
        # W4: the shape of an unmounted or mis-pointed directory.
        found = evaluate(evidence(source_files=()))

        self.assertEqual(rules(found), ["W4"])

    def test_min_files_above_the_source_count_holds(self):
        found = evaluate(evidence(source_files=("a",), min_files=5))

        self.assertEqual(rules(found), ["W4"])

    def test_a_shrinking_source_holds(self):
        # W3: from 100 to 50 files is a 50% fall, above the allowed 10%.
        found = evaluate(
            evidence(source_files=tuple(f"f{i}" for i in range(50)), last_source_count=100)
        )

        self.assertEqual(rules(found), ["W3"])

    def test_a_small_shrink_within_the_tolerance_does_not_hold(self):
        found = evaluate(
            evidence(source_files=tuple(f"f{i}" for i in range(95)), last_source_count=100)
        )

        self.assertEqual(rules(found), [])

    def test_a_growing_source_never_holds_on_w3(self):
        found = evaluate(
            evidence(source_files=tuple(f"f{i}" for i in range(200)), last_source_count=100)
        )

        self.assertEqual(rules(found), [])


class ProtectTests(unittest.TestCase):
    def test_deleting_a_protected_file_holds(self):
        # R6
        found = evaluate(
            evidence(deletes=("report.pdf",), protect=("*.pdf",), source_files=("a.txt",))
        )

        self.assertIn("R6", rules(found))
        self.assertIn("report.pdf", "\n".join(str(finding) for finding in found))

    def test_the_protect_glob_matches_a_nested_path(self):
        # `*.pdf` must mean "any PDF at any depth", which is what someone writing it intends.
        found = evaluate(
            evidence(deletes=("2026/q1/report.pdf",), protect=("*.pdf",), source_files=("a",))
        )

        self.assertIn("R6", rules(found))

    def test_a_scoped_glob_does_not_match_elsewhere(self):
        self.assertTrue(guards.matches_any("docs/a.pdf", "docs/*.pdf"))
        self.assertFalse(guards.matches_any("other/docs/a.pdf", "docs/*.pdf"))

    def test_deleting_an_unprotected_file_does_not_hold_on_r6(self):
        found = evaluate(
            evidence(deletes=("notes.txt",), protect=("*.pdf",), source_files=("a",))
        )

        self.assertNotIn("R6", rules(found))


class BudgetTests(unittest.TestCase):
    def test_exceeding_the_delete_budget_holds(self):
        # R1: rclone enforces this too, but checking here makes the refusal a clear hold rather
        # than a fatal mid-run.
        found = evaluate(
            evidence(
                deletes=tuple(f"d{i}" for i in range(26)),
                max_delete=25,
                destination_count=1000,
                source_files=("a",),
            )
        )

        self.assertIn("R1", rules(found))

    def test_a_delete_at_the_budget_does_not_hold_on_r1(self):
        found = evaluate(
            evidence(
                deletes=tuple(f"d{i}" for i in range(25)),
                max_delete=25,
                destination_count=1000,
                source_files=("a",),
            )
        )

        self.assertNotIn("R1", rules(found))

    def test_a_mass_delete_holds_even_when_under_budget(self):
        # R16: 50 deletions against a 100-object destination is half of it, far above 10%. This
        # is the signature of the source being unmounted or mis-pointed, where rclone reads the
        # whole destination as disposable.
        found = evaluate(
            evidence(
                deletes=tuple(f"d{i}" for i in range(50)),
                max_delete=1000,
                destination_count=100,
                source_files=("a",),
            )
        )

        self.assertIn("R16", rules(found))
        self.assertNotIn("R1", rules(found))

    def test_a_mass_delete_check_is_skipped_when_the_destination_is_empty(self):
        # Nothing to lose: an empty destination cannot be the source-of-truth being unmounted.
        found = evaluate(
            evidence(deletes=("d",), destination_count=0, max_delete=1000, source_files=("a",))
        )

        self.assertNotIn("R16", rules(found))


class ApprovalTests(unittest.TestCase):
    def test_any_deletion_needs_approval_by_default(self):
        # R12: the budget bounds how much a bug can do; it authorises nothing.
        found = evaluate(evidence(deletes=("one.txt",), source_files=("a",)))

        self.assertEqual(rules(found), ["R12"])

    def test_the_permissive_policy_skips_the_approval_hold(self):
        found = evaluate(
            evidence(deletes=("one.txt",), approve_deletes="over_budget", source_files=("a",))
        )

        self.assertEqual(rules(found), [])

    def test_the_permissive_policy_still_honours_the_budget(self):
        found = evaluate(
            evidence(
                deletes=tuple(f"d{i}" for i in range(30)),
                approve_deletes="over_budget",
                max_delete=25,
                destination_count=1000,
                source_files=("a",),
            )
        )

        self.assertEqual(rules(found), ["R1"])

    def test_an_empty_plan_with_no_deletions_never_needs_approval(self):
        self.assertEqual(evaluate(evidence()), [])


class CaseCollisionTests(unittest.TestCase):
    def test_a_case_only_collision_holds(self):
        # R15: Google Drive is case-insensitive, so one would silently replace the other.
        found = evaluate(evidence(source_files=("Report.pdf", "report.pdf")))

        self.assertEqual(rules(found), ["R15"])
        self.assertIn("Report.pdf", str(found[0]))

    def test_a_nested_case_collision_is_also_found(self):
        found = evaluate(evidence(source_files=("a/Notes.txt", "a/notes.txt")))

        self.assertEqual(rules(found), ["R15"])

    def test_distinct_names_do_not_hold(self):
        self.assertEqual(evaluate(evidence(source_files=("a.txt", "b.txt"))), [])

    def test_case_collisions_groups_the_offenders(self):
        groups = guards.case_collisions(("A.txt", "a.txt", "b.txt", "B.txt", "c.txt"))

        self.assertEqual(len(groups), 2)
        self.assertIn(("A.txt", "a.txt"), groups)


class CopyModeTests(unittest.TestCase):
    def test_a_copy_job_reporting_a_deletion_holds(self):
        # R5: copy cannot delete, so a delete here means the argv is not what this code built.
        found = evaluate(evidence(mode="copy", deletes=("a",), source_files=("a",)))

        self.assertEqual(rules(found), ["R5"])

    def test_a_copy_job_with_nothing_to_delete_is_clean(self):
        found = evaluate(evidence(mode="copy", transfers=10))

        self.assertEqual(rules(found), [])

    def test_a_copy_job_is_not_subject_to_the_delete_budget(self):
        found = evaluate(
            evidence(mode="copy", max_delete=0, deletes=("a",), source_files=("a",))
        )

        self.assertEqual(rules(found), ["R5"])


class CombinedTests(unittest.TestCase):
    def test_several_guards_can_fire_together_and_all_are_reported(self):
        found = evaluate(
            evidence(
                errors=1,
                deletes=("report.pdf",),
                protect=("*.pdf",),
                destination_count=100,
                source_files=("report.pdf",),
            )
        )

        self.assertEqual(rules(found), ["R12", "R6", "W1"])

    def test_summarise_renders_one_line_per_finding(self):
        text = guards.summarise(
            evaluate(evidence(errors=1, source_files=("a",), deletes=("b",)))
        )

        self.assertIn("  - W1:", text)
        self.assertIn("  - R12:", text)
        self.assertEqual(len(text.splitlines()), 2)

    def test_summarise_is_empty_for_a_clean_plan(self):
        self.assertEqual(guards.summarise([]), "")

    def test_any_of_finds_a_rule(self):
        found = evaluate(evidence(errors=1))

        self.assertTrue(guards.any_of(found, ("W1", "R1")))
        self.assertFalse(guards.any_of(found, ("R1",)))


if __name__ == "__main__":
    unittest.main()
