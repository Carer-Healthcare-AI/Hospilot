"""Smoke tests that do not need an LLM API key."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from ask import extract_sql, is_cannot_answer, run_sql, schema_text, validate_sql
from init_db import init_db

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "hospital.db"


class TestSqlGuards(unittest.TestCase):
    def test_extract_sql_fence(self):
        raw = "Sure.\n```sql\nSELECT 1 AS n\n```\n"
        self.assertEqual(extract_sql(raw), "SELECT 1 AS n")

    def test_cannot_answer(self):
        ok, reason = is_cannot_answer("CANNOT_ANSWER: no satisfaction scores")
        self.assertTrue(ok)
        self.assertIn("satisfaction", reason.lower())

    def test_reject_write(self):
        self.assertIsNotNone(validate_sql("DELETE FROM beds"))
        self.assertIsNone(validate_sql("SELECT COUNT(*) FROM beds"))


class TestSeedAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db(DB, force=True)

    def setUp(self):
        self.conn = sqlite3.connect(DB)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()

    def test_icu_free(self):
        _, rows = run_sql(
            self.conn,
            "SELECT COUNT(*) AS free_icu_beds FROM beds "
            "WHERE ward_type='ICU' AND status='free' AND is_active=1",
        )
        self.assertEqual(rows[0]["free_icu_beds"], 4)

    def test_short_staffed_night(self):
        _, rows = run_sql(
            self.conn,
            "SELECT ward_code, role, shortfall FROM v_staffing_gaps "
            "WHERE shift='night' AND roster_date='2026-08-13' "
            "ORDER BY shortfall DESC, ward_code",
        )
        codes = {(r["ward_code"], r["role"], r["shortfall"]) for r in rows}
        self.assertEqual(
            codes,
            {
                ("ICU-A", "nurse", 2),
                ("GEN-2E", "nurse", 2),
                ("PED-1", "nurse", 1),
                ("ER", "doctor", 1),
            },
        )

    def test_schema_text_mentions_views(self):
        text = schema_text(self.conn)
        self.assertIn("v_staffing_gaps", text)
        self.assertIn("beds", text)


if __name__ == "__main__":
    unittest.main()
