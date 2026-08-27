from __future__ import annotations

from pathlib import Path


LOGO_PATH = Path(__file__).parents[1] / "finance_app" / "assets" / "logo.svg"


def test_logo_bar_geometry_is_exact() -> None:
    bodies = [(36.0, 44.0), (46.0, 54.0), (56.0, 64.0)]
    rising_edges = [
        ((36.0, 51.0), (44.0, 43.0)),
        ((46.0, 39.0), (54.0, 31.0)),
        ((54.5, 29.5), (60.0, 24.0)),
    ]

    assert {right - left for left, right in bodies} == {8.0}
    assert [bodies[index + 1][0] - bodies[index][1] for index in range(2)] == [2.0, 2.0]
    for (x1, y1), (x2, y2) in rising_edges:
        assert x2 - x1 == y1 - y2
    assert rising_edges[1][0][1] < rising_edges[0][1][1]
    assert rising_edges[2][1][1] < rising_edges[1][1][1]

    svg = LOGO_PATH.read_text(encoding="utf-8")
    assert 'd="M36 58V51L44 43V58Z"' in svg
    assert 'd="M46 58V39L54 31V58Z"' in svg
    assert 'd="M56 58V29.5H54.5L60 24L65.5 29.5H64V58Z"' in svg
