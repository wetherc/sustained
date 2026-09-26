"""Tests for the vocabulary and the tuples an impact report is written in."""

import json
import unittest

from sustained.impact.model import (
    Blocks,
    Confidence,
    Evidence,
    Finding,
    Hold,
    ImpactReport,
    MigrationImpact,
    Severity,
    Shape,
    StatementImpact,
    TableImpact,
    Work,
)


class RankedEnumTestCase(unittest.TestCase):
    def test_members_rank_in_declared_order(self):
        self.assertLess(Blocks.NOTHING, Blocks.DDL)
        self.assertLess(Blocks.WRITES, Blocks.READS_AND_WRITES)
        self.assertLessEqual(Hold.BRIEF, Hold.BRIEF)
        self.assertGreater(Severity.DANGER, Severity.WARN)
        self.assertGreaterEqual(Evidence.OBSERVED, Evidence.CATALOG)
        self.assertLess(Confidence.UNKNOWN, Confidence.KNOWN)

    def test_unknown_work_ranks_as_the_heaviest(self):
        self.assertEqual(max(Work), Work.UNKNOWN)
        self.assertEqual(max([Work.CATALOG, Work.REWRITE, Work.SCAN]), Work.REWRITE)

    def test_members_of_different_enums_do_not_compare(self):
        for compare in (
            lambda: Blocks.DDL < Work.SCAN,
            lambda: Blocks.DDL <= Work.SCAN,
            lambda: Blocks.DDL > Work.SCAN,
            lambda: Blocks.DDL >= Work.SCAN,
        ):
            with self.assertRaises(TypeError):
                compare()

    def test_members_print_and_serialize_as_their_names(self):
        self.assertEqual(str(Blocks.READS_AND_WRITES), "reads_and_writes")
        self.assertEqual(f"{Work.INDEX_BUILD}", "index_build")
        self.assertEqual(json.dumps([Severity.WARN]), '["warn"]')
        self.assertEqual(Blocks("writes"), Blocks.WRITES)


class ReportTestCase(unittest.TestCase):
    def statement(self, sql, *severities):
        return StatementImpact(
            sql,
            Shape("create_index", "t"),
            (
                TableImpact(
                    "t", "SHARE", Blocks.WRITES, Work.INDEX_BUILD, Hold.STATEMENT
                ),
            ),
            tuple(Finding(f"r{i}", s, "m") for i, s in enumerate(severities)),
            Evidence.STATIC,
            Confidence.KNOWN,
        )

    def test_statement_severity_is_the_worst_finding(self):
        self.assertEqual(
            self.statement("a", Severity.INFO, Severity.DANGER).severity,
            Severity.DANGER,
        )
        self.assertIsNone(self.statement("a").severity)

    def test_report_collects_statements_and_findings_in_run_order(self):
        first = self.statement("a", Severity.WARN)
        second = self.statement("b", Severity.DANGER, Severity.WARN)
        report = ImpactReport(
            "postgres",
            (12,),
            Evidence.STATIC,
            (
                MigrationImpact(
                    "m1",
                    True,
                    (first,),
                    findings=(Finding("window", Severity.INFO, "held"),),
                ),
                MigrationImpact("m2", True, (second,)),
            ),
        )
        self.assertEqual(report.statements, (first, second))
        self.assertEqual(
            [f.rule for f in report.findings], ["r0", "window", "r0", "r1"]
        )
        self.assertEqual(report.count(Severity.WARN), 2)
        self.assertEqual(report.count(Severity.DANGER), 1)
        self.assertEqual(report.count(Severity.INFO), 1)

    def test_shape_knows_whether_it_was_understood(self):
        self.assertTrue(Shape("create_index").known)
        self.assertFalse(Shape("unknown").known)


if __name__ == "__main__":
    unittest.main()
