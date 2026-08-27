from __future__ import annotations

import pytest

from finance_app.charts import (
    budget_marker_position, condensed_stacked_series, is_over_budget, legend_options,
    linear_net_worth_trend,
    net_worth_options,
    liability_balance_options,
    liability_payment_options, stacked_area_options,
)


def test_stacked_area_uses_raw_values_in_one_opaque_stack() -> None:
    options = stacked_area_options({
        "months": ["2026-07", "2026-08"],
        "series": [
            {"name": "Groceries", "values": [10_000, 12_000]},
            {"name": "Shopping", "values": [4_000, 2_500]},
            {"name": "Travel", "values": [0, 8_000]},
        ],
    })

    series = options["series"]
    assert [item["data"] for item in series] == [
        [100.0, 120.0],
        [40.0, 25.0],
        [0.0, 80.0],
    ]
    assert {item["stack"] for item in series} == {"spending"}
    assert {item["stackStrategy"] for item in series} == {"all"}
    assert all(item["areaStyle"]["opacity"] == 1 for item in series)
    assert all(item["emphasis"]["focus"] == "none" for item in series)
    assert all(item["smooth"] == 0.28 for item in series)
    assert all(item["smoothMonotone"] == "x" for item in series)
    assert options["legend"] == legend_options(type="scroll", bottom=0)

    # These are the cumulative boundaries ECharts derives from the raw bands.
    boundaries = []
    cumulative = [0.0, 0.0]
    for item in series:
        lower = cumulative.copy()
        cumulative = [base + value for base, value in zip(cumulative, item["data"], strict=True)]
        boundaries.append((lower, cumulative.copy()))
    assert boundaries == [
        ([0.0, 0.0], [100.0, 120.0]),
        ([100.0, 120.0], [140.0, 145.0]),
        ([140.0, 145.0], [140.0, 225.0]),
    ]


def test_budget_overage_uses_a_strict_boundary() -> None:
    assert not is_over_budget(2_500, 10_000)
    assert not is_over_budget(10_000, 10_000)
    assert is_over_budget(10_001, 10_000)
    assert not is_over_budget(1_000, 0)


def test_budget_marker_uses_the_chart_scale() -> None:
    assert budget_marker_position(30_000, 50_000) == 60.0
    assert budget_marker_position(60_000, 50_000) == 100.0
    assert budget_marker_position(0, 50_000) == 0.0


def test_stacked_area_keeps_top_five_and_groups_remainder_exactly() -> None:
    data = {
        "months": ["2026-06", "2026-07"],
        "series": [
            {"name": "A", "values": [1000, 1000]},
            {"name": "B", "values": [900, 900]},
            {"name": "C", "values": [800, 800]},
            {"name": "D", "values": [700, 700]},
            {"name": "E", "values": [600, 600]},
            {"name": "F", "values": [500, 0]},
            {"name": "G", "values": [-100, 300]},
        ],
    }

    condensed = condensed_stacked_series(
        data, limit=5, other_label="Other categories",
    )
    assert [item["name"] for item in condensed["series"]] == [
        "A", "B", "C", "D", "E", "Other categories",
    ]
    assert condensed["series"][-1]["values"] == [400, 300]
    assert [
        sum(item["values"][index] for item in condensed["series"])
        for index in range(2)
    ] == [
        sum(item["values"][index] for item in data["series"])
        for index in range(2)
    ]


def test_stacked_area_grouping_validates_its_input() -> None:
    with pytest.raises(ValueError, match="at least one"):
        condensed_stacked_series({"months": [], "series": []}, limit=0)
    with pytest.raises(ValueError, match="one value per month"):
        condensed_stacked_series({
            "months": ["2026-07"],
            "series": [{"name": "Groceries", "values": []}],
        })


def test_stacked_area_budget_is_a_separate_step_line() -> None:
    options = stacked_area_options(
        {
            "months": ["2026-07", "2026-08"],
            "series": [{"name": "Groceries", "values": [20_000, 25_000]}],
        },
        budget_values=[30_000, 35_000],
    )
    spending, budget = options["series"]
    assert spending["stack"] == "spending"
    assert "stack" not in budget
    assert budget["name"] == "Budget"
    assert budget["data"] == [300.0, 350.0]
    assert budget["step"] == "end"
    assert budget["lineStyle"]["color"] == "#a33b33"
    with pytest.raises(ValueError, match="one value per month"):
        stacked_area_options(
            {"months": ["2026-07"], "series": []}, budget_values=[],
        )


def test_net_worth_trend_uses_actual_dates_and_excludes_estimates_from_fit() -> None:
    items = [
        {"date": "2026-01-01", "net_worth": 10_000, "estimated": False},
        {"date": "2026-03-01", "net_worth": 30_000, "estimated": False},
        {"date": "2026-04-01", "net_worth": 90_000, "estimated": True},
    ]
    trend = linear_net_worth_trend(items)
    assert trend[:2] == pytest.approx([100.0, 300.0])
    assert trend[2] == pytest.approx(405.08, abs=0.02)
    assert linear_net_worth_trend(items[:1]) == [None]

    options = net_worth_options(items)
    actual, estimated, fitted = options["series"][:3]
    assert actual["type"] == "line"
    assert actual["connectNulls"] is True
    assert actual["showSymbol"] is True
    assert actual["areaStyle"]["opacity"] > 0.1
    assert options["yAxis"]["scale"] is True
    assert options["yAxis"]["min"] == -10000
    assert options["yAxis"]["max"] == 11000
    assert estimated["lineStyle"]["type"] == "dashed"
    assert fitted["name"] == "Actual trend"
    assert fitted["data"] == pytest.approx(trend)


def test_net_worth_axis_uses_ten_percent_headroom_with_a_ten_thousand_minimum() -> None:
    close_values = [
        {"date": "2026-07-01", "net_worth": 25_600_000, "estimated": False},
        {"date": "2026-08-01", "net_worth": 25_597_900, "estimated": True},
    ]
    close_axis = net_worth_options(close_values)["yAxis"]
    assert close_axis["min"] == 230_000
    assert close_axis["max"] == 282_000

    small_values = [
        {"date": "2026-07-01", "net_worth": 4_000_000, "estimated": False},
        {"date": "2026-08-01", "net_worth": 4_100_000, "estimated": True},
    ]
    small_axis = net_worth_options(small_values)["yAxis"]
    assert small_axis["min"] == 30_000
    assert small_axis["max"] == 51_000


def test_liability_charts_preserve_cents_and_payment_provenance() -> None:
    data = {
        "months": ["2026-07", "2026-08"],
        "balance_series": [
            {"name": "Total remaining", "values": [25_000_00, 24_500_00]},
        ],
        "observed_payments": [150_000, 0],
        "scheduled_payments": [0, 125_000],
    }

    balance = liability_balance_options(data)
    payments = liability_payment_options(data)

    assert balance["series"][0]["data"] == [25_000.0, 24_500.0]
    assert payments["series"][0]["name"] == "Observed payments"
    assert payments["series"][0]["data"] == [1_500.0, 0.0]
    assert payments["series"][1]["name"] == "Contractual fallback"
    assert payments["series"][1]["data"] == [0.0, 1_250.0]
    assert {item["stack"] for item in payments["series"]} == {"payments"}
    for legend in (balance["legend"], payments["legend"]):
        assert legend["icon"] == "rect"
        assert legend["itemWidth"] == legend["itemHeight"]
        assert legend["itemStyle"]["borderWidth"] == 0
