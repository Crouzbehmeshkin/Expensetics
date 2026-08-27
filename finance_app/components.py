from __future__ import annotations

from pathlib import Path

from nicegui import ui

from .i18n import translate


BRAND_MARK_SVG = (
    Path(__file__).parent / "assets" / "logo.svg"
).read_text(encoding="utf-8")


def brand_mark() -> None:
    ui.html(BRAND_MARK_SVG, sanitize=False).classes("brand-mark")


def primary_action(label: str, icon: str | None, on_click):
    return ui.button(translate(label), icon=icon, on_click=on_click).props(
        "unelevated no-caps"
    ).classes("primary").style(
        "background-color: var(--ink) !important; color: var(--paper) !important"
    )


def chip_button(label: str, selected: bool, on_click, count: int | None = None) -> None:
    translated = translate(label)
    text = f"{translated}  {count}" if count is not None else translated
    ui.button(text, on_click=on_click).props("flat dense no-caps").classes(
        f"category-chip {'selected' if selected else ''}"
    )


def chart_title(text: str, supporting: str) -> None:
    with ui.column().classes("gap-1"):
        ui.label(translate(text)).classes("section-title")
        ui.label(translate(supporting)).classes("muted text-xs")
