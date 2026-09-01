"""NEWS2 against Appendix C's published totals.

These are the load-bearing numbers in the whole utility: NEWS2 feeds Urgency directly (.35),
Deterioration Risk through its slope (.30 of Clinical Benefit), and the severity band inside
Waiting/Delay. If the band tables are wrong, three components are wrong and nothing downstream
can be trusted.
"""

from __future__ import annotations

import pytest

from allocation.features.news2 import score_reading
from allocation.ingest.fixtures import ER_VITALS, OT_VITALS, WARD_VITALS


@pytest.mark.parametrize(
    ("reading", "expected", "label"),
    [
        (ER_VITALS[0], 8, "ER 11:00 — C.2 publishes 8"),
        (ER_VITALS[2], 16, "ER 12:55 — C.2 publishes 16"),
        (OT_VITALS[0], 2, "OT 11:30 — C.3: ventilated, oxygen 2, everything else 0"),
        (OT_VITALS[1], 2, "OT 12:50 — C.3"),
        (WARD_VITALS[0], 7, "Ward 10:30 — C.4 publishes 7"),
        (WARD_VITALS[1], 9, "Ward 12:50 — C.4 publishes 9"),
    ],
)
def test_news2_totals(reading, expected, label, config):
    score = score_reading(reading, config.threshold("news2_bands"))
    assert score.points == expected, label


def test_missing_parameter_is_reported_not_zeroed(config):
    """OT is anaesthetised, so consciousness is unassessable.

    Absent must show up as reduced coverage, not as a GCS of 15 scoring zero points. The
    distinction is the whole reason NEWS2 returns coverage at all.
    """
    score = score_reading(OT_VITALS[0], config.threshold("news2_bands"))
    assert "gcs" in score.missing
    assert score.coverage < 1.0


def test_er_has_full_coverage(config):
    """ER's oxygen flag is inferred from a pharmacy order, so all seven parameters are present.

    When migration 092 lands this stops depending on the inference. Until then, a patient with
    no oxygen order and no flag scores 6 of 7 — which is correct, and is not 'on room air'.
    """
    score = score_reading(ER_VITALS[2], config.threshold("news2_bands"))
    assert score.missing == ()
    assert score.coverage == 1.0
