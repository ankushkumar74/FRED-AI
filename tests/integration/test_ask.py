"""The single orchestration path used by the CLI and the browser demo.

Worth testing directly rather than through either shell: if `Ask.answer` is
correct, both surfaces are correct, and the website stops being a separate thing
that could quietly diverge from what the eval suite scores.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ask import Ask, parse_grain, parse_time_range
from services.semantic.models import TimeGrain

pytest.importorskip("duckdb")

METRICS = ROOT / "configs" / "metrics" / "ecommerce.yaml"
TODAY = date(2026, 8, 2)


@pytest.fixture(scope="module")
def fred(tmp_path_factory: pytest.TempPathFactory) -> Ask:
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_demo import generate  # type: ignore[import-not-found]

    db = tmp_path_factory.mktemp("askfx") / "demo.duckdb"
    generate(db)
    return Ask(metrics_path=METRICS, database=db, today=TODAY)


class TestAnswering:
    def test_single_value(self, fred: Ask) -> None:
        answer = fred.answer("revenue last month")
        assert answer.status == "answered"
        assert answer.metric_key == "gross_revenue"
        assert len(answer.rows) == 1
        assert float(answer.rows[0]["value"]) > 0
        assert answer.sql and "fct_orders" in answer.sql

    def test_breakdown_by_dimension(self, fred: Ask) -> None:
        answer = fred.answer("revenue by channel last month")
        assert answer.dimensions == ["channel"]
        assert len(answer.rows) == 5
        assert answer.rows[0]["value"] >= answer.rows[-1]["value"], "should be sorted by value"

    def test_cross_entity_ratio(self, fred: Ask) -> None:
        answer = fred.answer("CAC by channel last month")
        assert answer.status == "answered"
        assert answer.metric_key == "cac"
        assert set(answer.tables) == {"fct_marketing_spend", "fct_orders"}

    def test_trend_gets_a_grain(self, fred: Ask) -> None:
        answer = fred.answer("weekly revenue over the last 8 weeks")
        assert answer.grain == "week"
        assert len(answer.rows) > 1

    def test_certification_is_surfaced(self, fred: Ask) -> None:
        """Users need to know whether a definition is vouched for."""
        assert fred.answer("revenue last month").certified is True


class TestRestraint:
    def test_fan_out_is_refused(self, fred: Ask) -> None:
        answer = fred.answer("revenue by product category")
        assert answer.status == "refused"
        assert "double-count" in (answer.message or "")

    def test_unshared_dimension_is_refused(self, fred: Ask) -> None:
        answer = fred.answer("CAC by device")
        assert answer.status == "refused"
        assert "fabricated" in (answer.message or "")

    def test_undefined_metric_asks(self, fred: Ask) -> None:
        answer = fred.answer("what is our conversion rate")
        assert answer.status == "clarify"
        assert "conversion" in (answer.message or "")

    def test_ambiguous_question_asks(self, fred: Ask) -> None:
        assert fred.answer("how much did we make in July").status == "clarify"

    def test_empty_question(self, fred: Ask) -> None:
        assert fred.answer("   ").status == "clarify"

    def test_diagnostic_states_its_limit(self, fred: Ask) -> None:
        """The product must not invent a cause before the evidence engine exists."""
        answer = fred.answer("why did revenue drop last week")
        assert answer.status == "answered"
        assert answer.diagnostic is True
        assert answer.limitation and "not yet why" in answer.limitation


class TestSerialization:
    def test_answer_is_json_safe(self, fred: Ask) -> None:
        """The browser bridge serialises this; a date would blow up json.dumps."""
        payload = fred.answer("weekly revenue over the last 8 weeks").to_dict()
        text = json.dumps(payload)
        assert '"status": "answered"' in text
        assert isinstance(payload["rows"][0]["period"], str)

    def test_every_status_serialises(self, fred: Ask) -> None:
        for question in (
            "revenue last month",
            "revenue by product category",
            "what is our conversion rate",
        ):
            json.dumps(fred.answer(question).to_dict())

    def test_catalogue_is_json_safe(self, fred: Ask) -> None:
        catalogue = fred.catalogue()
        assert len(catalogue) == 13
        json.dumps(catalogue)


class TestParsing:
    @pytest.mark.parametrize(
        "question,expected_label",
        [
            ("revenue last week", "last week"),
            ("revenue last month", "last month"),
            ("revenue this year", "year to date"),
            ("revenue last 6 weeks", "the last 6 weeks"),
            ("revenue", "last month"),
        ],
    )
    def test_time_phrases(self, question: str, expected_label: str) -> None:
        _, _, label = parse_time_range(question, TODAY)
        assert label == expected_label

    def test_last_week_is_a_complete_week(self) -> None:
        window, _, _ = parse_time_range("revenue last week", TODAY)
        assert (window.end - window.start).days == 6
        assert window.end < TODAY or window.end == date(2026, 8, 2)

    @pytest.mark.parametrize(
        "question,grain",
        [("daily orders", TimeGrain.DAY), ("weekly revenue", TimeGrain.WEEK),
         ("monthly revenue", TimeGrain.MONTH), ("revenue trend", TimeGrain.WEEK),
         ("revenue last month", None)],
    )
    def test_grain_phrases(self, question: str, grain: TimeGrain | None) -> None:
        assert parse_grain(question) == grain
