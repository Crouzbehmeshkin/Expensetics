from __future__ import annotations

from datetime import date
from fractions import Fraction

from nicegui import ui

from .formatting import display_name, money, month_label
from .i18n import translate

CHART_COLORS = (
    "#365f4b", "#839b8f", "#c59a62", "#9a7f8c",
    "#64788c", "#b8a77c", "#7f9272", "#b27268",
)

# Adjacent area bands need stronger hue separation than independent charts.
# The tones stay restrained, but no two neighboring bands rely on similar greens.
STACKED_AREA_COLORS = (
    "#365f4b", "#71899a", "#c49a5f", "#8e7489",
    "#667a6b", "#b37066", "#7b759b", "#9a895e",
    "#577f83", "#a06f78", "#85936a", "#80776d",
)


def legend_options(**overrides) -> dict:
    """Return one accessible, filled-square legend style for every chart."""
    options = {
        "icon": "rect",
        "itemWidth": 9,
        "itemHeight": 9,
        "itemStyle": {"borderWidth": 0},
        "textStyle": {"color": "#8a8f87", "fontSize": 10},
    }
    options.update(overrides)
    return options


def stacked_category_bar(data: dict):
    totals = [
        sum(series["values"][index] for series in data["series"])
        for index in range(len(data["categories"]))
    ]
    last_nonzero_series = [
        max(
            (series_index for series_index, item in enumerate(data["series"])
             if item["values"][category_index]),
            default=-1,
        )
        for category_index in range(len(data["categories"]))
    ]
    series = []
    for series_index, item in enumerate(data["series"]):
        values = []
        for category_index, value in enumerate(item["values"]):
            datum: dict = {"value": value / 100}
            if series_index == last_nonzero_series[category_index]:
                datum["label"] = {
                    "show": True,
                    "position": "left" if totals[category_index] < 0 else "right",
                    "distance": 8,
                    "color": "#8a8f87", "fontSize": 11, "fontWeight": 600,
                    "formatter": money(totals[category_index]),
                }
            values.append(datum)
        series.append({
            "name": item["name"], "type": "bar", "stack": "spending",
            "barWidth": 16, "data": values, "emphasis": {"focus": "series"},
        })
    options = {
        "color": list(CHART_COLORS[:4]),
        "tooltip": {"show": False},
        "legend": legend_options(
            show=len(series) > 1, bottom=0,
            textStyle={"color": "#8a8f87", "fontSize": 11},
        ),
        "grid": {
            "left": 8, "right": 88, "top": 8,
            "bottom": 18 if len(series) == 1 else 42, "containLabel": True,
        },
        "xAxis": {
            "type": "value", "axisLabel": {"color": "#8a8f87", "formatter": "${value}"},
            "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.20)"}},
        },
        "yAxis": {
            "type": "category", "data": data["categories"],
            "axisLabel": {"color": "#8a8f87", "fontSize": 11},
            "axisLine": {"show": False}, "axisTick": {"show": False},
        },
        "series": series,
    }
    return ui.echart(options).classes("finance-chart").style(
        f"height: {max(300, len(data['categories']) * 32 + 90)}px"
    )


def is_over_budget(actual_cents: int, budget_cents: int) -> bool:
    """Return whether monthly-equivalent spending exceeds a configured budget."""
    return budget_cents > 0 and actual_cents > budget_cents


def budget_marker_position(budget_cents: int, scale_maximum: int) -> float:
    """Return a budget target position on the chart's shared zero-to-maximum scale."""
    if budget_cents <= 0 or scale_maximum <= 0:
        return 0.0
    return min(100.0, budget_cents / scale_maximum * 100)


def selectable_category_bar(
    data: dict,
    selected: str | None,
    on_select,
    *,
    budgets: dict[str, dict] | None = None,
) -> None:
    budgets = budgets or {}
    values = data["series"][0]["values"]
    maximum = max(
        1,
        max((abs(value) for value in values), default=0),
        max((budget["amount_cents"] for budget in budgets.values()), default=0),
    )
    with ui.column().classes("selectable-category-chart w-full gap-1"):
        if budgets:
            with ui.row().classes("selectable-budget-key items-center justify-end"):
                ui.element("span").classes("selectable-budget-key-line")
                ui.label(translate("Budget limit")).classes("selectable-budget-key-label")
        for category, value in zip(data["categories"], values, strict=True):
            budget = budgets.get(category)
            row_classes = "selectable-category-row"
            if category == selected:
                row_classes += " selected"
            with ui.element("button").classes(row_classes).props("type=button").on(
                "click", lambda _, category=category: on_select(category)
            ):
                ui.label(translate(category)).classes("selectable-category-name")
                bar_width = max(1.5, abs(value) / maximum * 100) if value else 0
                with ui.element("div").classes("selectable-bar-track"):
                    fill_class = "selectable-bar-fill credit" if value < 0 else "selectable-bar-fill"
                    ui.element("span").classes(fill_class).style(
                        f"width: {bar_width:.2f}%"
                    )
                    if budget:
                        over_budget = is_over_budget(
                            budget["actual_cents"], budget["amount_cents"]
                        )
                        marker_position = budget_marker_position(
                            budget["amount_cents"], maximum
                        )
                        status = "Over budget" if over_budget else "Within budget"
                        ui.element("span").classes("selectable-budget-marker").style(
                            f"left: {marker_position:.2f}%"
                        ).props(f'role=img aria-label="{translate(status)}"')
                with ui.column().classes("selectable-category-values"):
                    ui.label(money(value)).classes("selectable-category-value")
                    if budget:
                        remaining = budget["remaining_cents"]
                        delta_message = translate(
                            "{amount} over" if over_budget else "{amount} left",
                            amount=money(abs(remaining)),
                        )
                        ui.label(delta_message).classes(
                            "selectable-budget-delta over" if over_budget
                            else "selectable-budget-delta"
                        )


def compact_detail_bar(data: dict):
    categories = [display_name(value) for value in data["categories"]][::-1]
    values = data["series"][0]["values"][::-1]
    options = {
        "tooltip": {"show": False},
        "grid": {"left": 4, "right": 76, "top": 8, "bottom": 18, "containLabel": True},
        "xAxis": {
            "type": "value", "axisLabel": {"show": False},
            "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.16)"}},
        },
        "yAxis": {
            "type": "category", "data": categories,
            "axisLabel": {"color": "#8a8f87", "fontSize": 10, "width": 86, "overflow": "truncate"},
            "axisLine": {"show": False}, "axisTick": {"show": False},
        },
        "series": [{
            "name": "Spending", "type": "bar", "barWidth": 13,
            "data": [{
                "value": value / 100,
                "itemStyle": {
                    "color": "#647f70" if value >= 0 else "#71899a",
                    "borderRadius": 4,
                },
                "label": {
                    "show": True,
                    "position": "left" if value < 0 else "right",
                    "distance": 6,
                    "color": "#8a8f87", "fontSize": 10, "fontWeight": 650,
                    "formatter": money(value),
                },
            } for value in values],
            "emphasis": {"disabled": True},
        }],
    }
    return ui.echart(options).classes("finance-chart compact-detail-chart").style(
        f"height: {max(250, len(categories) * 27 + 45)}px"
    )


def ranked_total_bar(items: list[dict], name_key: str):
    ordered = sorted(items, key=lambda item: item["total"])
    return stacked_category_bar({
        "categories": [
            translate(item[name_key]) if name_key == "category"
            else display_name(item[name_key])
            for item in ordered
        ],
        "series": [{"name": translate("Spending"), "values": [item["total"] for item in ordered]}],
    })


def condensed_stacked_series(
    data: dict, *, limit: int = 5, other_label: str = "Other",
) -> dict:
    """Keep the most consequential series and preserve all remainder values.

    Ranking uses absolute contribution across the visible window, then active
    month count and name. The remainder is summed month-by-month without loss.
    """
    if limit < 1:
        raise ValueError("Series limit must be at least one")

    months = list(data["months"])
    series = [
        {"name": item["name"], "values": list(item["values"])}
        for item in data["series"]
    ]
    if any(len(item["values"]) != len(months) for item in series):
        raise ValueError("Every chart series must contain one value per month")
    if len(series) <= limit:
        return {"months": months, "series": series}

    ranked = sorted(
        series,
        key=lambda item: (
            -sum(abs(value) for value in item["values"]),
            -sum(value != 0 for value in item["values"]),
            item["name"].casefold(),
        ),
    )
    selected = ranked[:limit]
    remainder = ranked[limit:]
    other_values = [
        sum(item["values"][index] for item in remainder)
        for index in range(len(months))
    ]
    return {
        "months": months,
        "series": [
            *selected,
            {"name": other_label, "values": other_values},
        ],
    }


def stacked_area_options(
    data: dict, *, series_limit: int = 5, other_label: str = "Other",
    translate_names: bool = False, budget_values: list[int | None] | None = None,
) -> dict:
    """Build a true categorical stack where every series is a distinct band."""
    visible_data = condensed_stacked_series(
        data, limit=series_limit, other_label=other_label,
    )
    series = []
    for item in visible_data["series"]:
        series.append({
            "name": translate(item["name"]) if translate_names else item["name"],
            "type": "line",
            "stack": "spending",
            "stackStrategy": "all",
            "smooth": 0.28,
            "smoothMonotone": "x",
            "showSymbol": False,
            "lineStyle": {"width": 1.4},
            "areaStyle": {"opacity": 1},
            "emphasis": {"focus": "none"},
            # Keep category values raw. ECharts derives each cumulative boundary
            # from this shared stack without changing tooltip values.
            "data": [value / 100 for value in item["values"]],
        })

    if budget_values is not None:
        if len(budget_values) != len(visible_data["months"]):
            raise ValueError("The budget must contain one value per month")
        if any(value is not None and value > 0 for value in budget_values):
            series.append({
                "name": translate("Budget"),
                "type": "line",
                "step": "end",
                "smooth": False,
                "showSymbol": False,
                "connectNulls": False,
                "z": 10,
                "data": [value / 100 if value is not None else None for value in budget_values],
                "lineStyle": {"width": 1.6, "type": "dashed", "color": "#a33b33"},
                "itemStyle": {"color": "#a33b33"},
                "emphasis": {"disabled": True},
            })

    return {
        "color": list(STACKED_AREA_COLORS),
        "tooltip": {"trigger": "axis"},
        "legend": legend_options(type="scroll", bottom=0),
        "grid": {"left": 10, "right": 18, "top": 18, "bottom": 52, "containLabel": True},
        "xAxis": {
            "type": "category", "boundaryGap": False,
            "data": [month_label(value, short=True) for value in visible_data["months"]],
            "axisLabel": {"color": "#8a8f87"},
            "axisLine": {"lineStyle": {"color": "rgba(128,132,124,.28)"}},
        },
        "yAxis": {
            "type": "value", "axisLabel": {"color": "#8a8f87", "formatter": "${value}"},
            "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.20)"}},
        },
        "series": series,
    }


def stacked_area_chart(
    data: dict, height: int = 330, *, other_label: str = "Other",
    translate_names: bool = False, budget_values: list[int | None] | None = None,
):
    options = stacked_area_options(
        data, other_label=translate(other_label), translate_names=translate_names,
        budget_values=budget_values,
    )
    return ui.echart(options).classes("finance-chart").style(f"height: {height}px")


def linear_net_worth_trend(items: list[dict]) -> list[float | None]:
    """Fit a least-squares line to actual snapshots only, using real date spacing."""
    actual = [
        (date.fromisoformat(item["date"]).toordinal(), int(item["net_worth"]))
        for item in items if not item.get("estimated")
    ]
    if len(actual) < 2:
        return [None] * len(items)
    x_mean = Fraction(sum(point[0] for point in actual), len(actual))
    y_mean = Fraction(sum(point[1] for point in actual), len(actual))
    denominator = sum((Fraction(x) - x_mean) ** 2 for x, _ in actual)
    if denominator == 0:
        return [None] * len(items)
    slope = sum(
        (Fraction(x) - x_mean) * (Fraction(y) - y_mean) for x, y in actual
    ) / denominator
    return [
        float((y_mean + slope * (date.fromisoformat(item["date"]).toordinal() - x_mean)) / 100)
        for item in items
    ]


def net_worth_axis_bounds(
    items: list[dict], trend_values: list[float | None],
) -> tuple[int, int] | None:
    """Return readable dollar bounds with 10% headroom and a $10k minimum."""
    values_cents = [int(item["net_worth"]) for item in items]
    values_cents.extend(
        round(value * 100) for value in trend_values if value is not None
    )
    if not values_cents:
        return None
    padding = max(1_000_000, max(abs(value) for value in values_cents) // 10)
    step = 100_000  # align displayed bounds to whole $1,000 increments
    lower_cents = ((min(values_cents) - padding) // step) * step
    upper_raw = max(values_cents) + padding
    upper_cents = ((upper_raw + step - 1) // step) * step
    return lower_cents // 100, upper_cents // 100


def net_worth_options(items: list[dict]) -> dict:
    actual_values, estimated_values = net_worth_values(items)
    estimated_line: list[object] = list(estimated_values)
    first_estimate = next(
        (index for index, value in enumerate(estimated_values) if value is not None), None,
    )
    if first_estimate and actual_values[first_estimate - 1] is not None:
        estimated_line[first_estimate - 1] = {
            "value": actual_values[first_estimate - 1], "symbol": "none",
        }
    trend_values = linear_net_worth_trend(items)
    axis_bounds = net_worth_axis_bounds(items, trend_values)
    actual_name = translate("Actual net worth")
    estimated_name = translate("Estimated net worth")
    assets_name = translate("Assets")
    liabilities_name = translate("Liabilities")
    trend_name = translate("Actual trend")

    return {
        "color": [CHART_COLORS[0], "#9a7b48", "#9a7f8c", "#b27268", "#64788c"],
        "tooltip": {"trigger": "axis"},
        "legend": legend_options(
            bottom=0,
            selected={assets_name: False, liabilities_name: False},
        ),
        "grid": {"left": 8, "right": 18, "top": 18, "bottom": 46, "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": [
                (f'{translate("Estimated")} ' if item.get("estimated") else "")
                + month_label(item["date"][:7], short=True)
                for item in items
            ],
            "axisLabel": {"color": "#8a8f87"},
        },
        "yAxis": {
            "type": "value", "scale": True,
            **({"min": axis_bounds[0], "max": axis_bounds[1]} if axis_bounds else {}),
            "splitNumber": 4,
            "axisLabel": {"color": "#8a8f87", "formatter": "${value}"},
            "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.20)"}},
        },
        "series": [
            {"name": actual_name, "type": "line", "smooth": 0.25,
             "symbol": "circle", "showSymbol": True, "symbolSize": 8,
             "data": actual_values,
             "connectNulls": True,
             "lineStyle": {"width": 3}, "areaStyle": {"opacity": 0.16}},
            {"name": estimated_name, "type": "line", "smooth": 0.2,
             "symbol": "emptyCircle", "symbolSize": 7, "data": estimated_line,
             "lineStyle": {"width": 2.4, "type": "dashed"}},
            {"name": trend_name, "type": "line", "smooth": False, "symbol": "none",
             "data": trend_values, "connectNulls": True,
             "lineStyle": {"width": 1.5, "type": "dotted", "opacity": 0.85}},
            {"name": assets_name, "type": "line", "smooth": 0.2, "symbol": "none",
             "data": [
                 item.get("assets_cents") / 100
                 if item.get("assets_cents") is not None else None
                 for item in items
             ],
             "lineStyle": {"width": 1.3, "type": "dashed"}},
            {"name": liabilities_name, "type": "line", "smooth": 0.2, "symbol": "none",
             "data": [
                 item.get("liabilities_cents") / 100
                 if item.get("liabilities_cents") is not None else None
                 for item in items
             ],
             "lineStyle": {"width": 1.3, "type": "dashed"}},
        ],
    }


def net_worth_chart(items: list[dict]):
    return ui.echart(net_worth_options(items)).classes("finance-chart").style(
        "height: 300px"
    )


def transaction_count_options(items: list[dict]) -> dict:
    ordered = sorted(items, key=lambda item: (item["count"], item["category"].casefold()))
    return {
        "tooltip": {"show": False},
        "grid": {"left": 8, "right": 44, "top": 8, "bottom": 16, "containLabel": True},
        "xAxis": {
            "type": "value", "minInterval": 1,
            "axisLabel": {"color": "#8a8f87", "formatter": "{value}"},
            "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.16)"}},
        },
        "yAxis": {
            "type": "category",
            "data": [translate(item["category"]) for item in ordered],
            "axisLabel": {"color": "#8a8f87", "fontSize": 11},
            "axisLine": {"show": False}, "axisTick": {"show": False},
        },
        "series": [{
            "name": translate("Transactions"), "type": "bar", "barWidth": 13,
            "data": [{
                "value": item["count"],
                "itemStyle": {"color": "#647f70", "borderRadius": 4},
                "label": {
                    "show": True, "position": "right", "distance": 6,
                    "color": "#8a8f87", "fontSize": 10, "fontWeight": 650,
                },
            } for item in ordered],
            "emphasis": {"disabled": True},
        }],
    }


def transaction_count_chart(items: list[dict]):
    return ui.echart(transaction_count_options(items)).classes("finance-chart").style(
        f"height: {max(250, len(items) * 26 + 40)}px"
    )


def category_activity_options(data: dict) -> dict:
    return {
        "color": [CHART_COLORS[0], CHART_COLORS[1]],
        "tooltip": {"trigger": "axis"},
        "legend": legend_options(bottom=0),
        "grid": {"left": 8, "right": 12, "top": 18, "bottom": 46, "containLabel": True},
        "xAxis": {
            "type": "category", "data": [month_label(month, short=True) for month in data["months"]],
            "axisLabel": {"color": "#8a8f87"},
            "axisLine": {"lineStyle": {"color": "rgba(128,132,124,.28)"}},
        },
        "yAxis": [
            {
                "type": "value", "minInterval": 1,
                "axisLabel": {"color": "#8a8f87", "formatter": "{value}×"},
                "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.16)"}},
            },
            {
                "type": "value", "axisLabel": {"color": "#8a8f87", "formatter": "${value}"},
                "splitLine": {"show": False},
            },
        ],
        "series": [
            {
                "name": translate("Purchases"), "type": "bar", "barMaxWidth": 22,
                "data": data["counts"], "itemStyle": {"borderRadius": [4, 4, 0, 0]},
            },
            {
                "name": translate("Net spending"), "type": "line", "yAxisIndex": 1,
                "smooth": 0.25, "smoothMonotone": "x", "showSymbol": False,
                "data": [value / 100 for value in data["totals"]],
                "lineStyle": {"width": 2.2},
            },
        ],
    }


def category_activity_chart(data: dict):
    return ui.echart(category_activity_options(data)).classes("finance-chart").style(
        "height: 300px"
    )


def weekday_activity_options(data: dict) -> dict:
    return {
        "tooltip": {"show": False},
        "grid": {"left": 8, "right": 8, "top": 18, "bottom": 28, "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": [translate(label)[:3] for label in data["labels"]],
            "axisLabel": {"color": "#8a8f87"},
            "axisLine": {"lineStyle": {"color": "rgba(128,132,124,.28)"}},
            "axisTick": {"show": False},
        },
        "yAxis": {
            "type": "value", "minInterval": 1,
            "axisLabel": {"color": "#8a8f87"},
            "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.16)"}},
        },
        "series": [{
            "name": translate("Purchases"), "type": "bar", "barMaxWidth": 28,
            "data": data["counts"],
            "itemStyle": {"color": "#71899a", "borderRadius": [5, 5, 0, 0]},
            "label": {"show": True, "position": "top", "color": "#8a8f87", "fontSize": 10},
            "emphasis": {"disabled": True},
        }],
    }


def weekday_activity_chart(data: dict):
    return ui.echart(weekday_activity_options(data)).classes("finance-chart").style(
        "height: 260px"
    )


def settlement_activity_options(data: dict) -> dict:
    return {
        "color": ["#71899a", "#9a7f8c"],
        "tooltip": {"trigger": "axis"},
        "legend": legend_options(bottom=0),
        "grid": {"left": 8, "right": 12, "top": 18, "bottom": 46, "containLabel": True},
        "xAxis": {
            "type": "category", "data": [month_label(month, short=True) for month in data["months"]],
            "axisLabel": {"color": "#8a8f87"},
            "axisLine": {"lineStyle": {"color": "rgba(128,132,124,.28)"}},
        },
        "yAxis": [
            {
                "type": "value", "axisLabel": {"color": "#8a8f87", "formatter": "${value}"},
                "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.16)"}},
            },
            {
                "type": "value", "minInterval": 1,
                "axisLabel": {"color": "#8a8f87", "formatter": "{value}×"},
                "splitLine": {"show": False},
            },
        ],
        "series": [
            {
                "name": translate("Settled"), "type": "bar", "barMaxWidth": 24,
                "data": [value / 100 for value in data["totals"]],
                "itemStyle": {"borderRadius": [4, 4, 0, 0]},
            },
            {
                "name": translate("Settlements"), "type": "line", "yAxisIndex": 1,
                "smooth": 0.25, "showSymbol": False, "data": data["counts"],
                "lineStyle": {"width": 2},
            },
        ],
    }


def settlement_activity_chart(data: dict):
    return ui.echart(settlement_activity_options(data)).classes("finance-chart").style(
        "height: 290px"
    )


def liability_balance_options(data: dict) -> dict:
    series = []
    for index, item in enumerate(data["balance_series"]):
        series.append({
            "name": translate(item["name"]),
            "type": "line",
            "smooth": False,
            "connectNulls": False,
            "symbol": "circle" if index == 0 else "none",
            "symbolSize": 5,
            "data": [value / 100 if value is not None else None for value in item["values"]],
            "lineStyle": {
                "width": 2.8 if index == 0 else 1.3,
                "type": "solid" if index == 0 else "dashed",
            },
            "areaStyle": {"opacity": 0.06} if index == 0 else None,
            "emphasis": {"focus": "series"},
        })
    return {
        "color": list(CHART_COLORS),
        "tooltip": {
            "trigger": "axis",
            ":valueFormatter": "(value) => '$' + value.toLocaleString()",
        },
        "legend": legend_options(
            show=len(series) > 1, type="scroll", bottom=0,
        ),
        "grid": {
            "left": 8, "right": 18, "top": 18,
            "bottom": 48 if len(series) > 1 else 28, "containLabel": True,
        },
        "xAxis": {
            "type": "category",
            "data": [month_label(value, short=True) for value in data["months"]],
            "axisLabel": {"color": "#8a8f87"},
            "axisLine": {"lineStyle": {"color": "rgba(128,132,124,.28)"}},
        },
        "yAxis": {
            "type": "value", "axisLabel": {"color": "#8a8f87", "formatter": "${value}"},
            "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.20)"}},
        },
        "series": series,
    }


def liability_balance_chart(data: dict):
    return ui.echart(liability_balance_options(data)).classes("finance-chart").style(
        "height: 320px"
    )


def liability_payment_options(data: dict) -> dict:
    return {
        "color": [CHART_COLORS[0], "#b8a77c"],
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": legend_options(bottom=0),
        "grid": {"left": 8, "right": 18, "top": 18, "bottom": 48, "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": [month_label(value, short=True) for value in data["months"]],
            "axisLabel": {"color": "#8a8f87"},
            "axisLine": {"lineStyle": {"color": "rgba(128,132,124,.28)"}},
        },
        "yAxis": {
            "type": "value", "axisLabel": {"color": "#8a8f87", "formatter": "${value}"},
            "splitLine": {"lineStyle": {"color": "rgba(128,132,124,.20)"}},
        },
        "series": [
            {
                "name": translate("Observed payments"), "type": "bar", "stack": "payments",
                "barMaxWidth": 24,
                "data": [value / 100 for value in data["observed_payments"]],
                "itemStyle": {"borderRadius": [4, 4, 0, 0]},
            },
            {
                "name": translate("Contractual fallback"), "type": "bar", "stack": "payments",
                "barMaxWidth": 24,
                "data": [value / 100 for value in data["scheduled_payments"]],
                "itemStyle": {"borderRadius": [4, 4, 0, 0]},
            },
        ],
    }


def liability_payment_chart(data: dict):
    return ui.echart(liability_payment_options(data)).classes("finance-chart").style(
        "height: 320px"
    )


def net_worth_values(items: list[dict]) -> tuple[list[float | None], list[float | None]]:
    """Keep actual and estimated points in mutually exclusive chart series."""
    actual = [
        item["net_worth"] / 100 if not item.get("estimated") else None
        for item in items
    ]
    estimated = [
        item["net_worth"] / 100 if item.get("estimated") else None
        for item in items
    ]
    return actual, estimated
