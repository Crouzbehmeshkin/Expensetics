from __future__ import annotations

import pytest

from finance_app.i18n import (
    DEFAULT_LANGUAGE, LANGUAGES, catalog_keys, date_name, month_name, translate,
)


NON_ENGLISH = [language.code for language in LANGUAGES if language.code != DEFAULT_LANGUAGE]


def test_every_language_pack_has_the_same_reviewed_vocabulary() -> None:
    reference = catalog_keys("es")
    assert len(reference) >= 100
    for language in NON_ENGLISH:
        assert catalog_keys(language) == reference


@pytest.mark.parametrize("language", NON_ENGLISH)
def test_navigation_and_finance_terms_are_native(language: str) -> None:
    for message in ("Overview", "Expenses", "Add expense", "Net worth", "Groceries"):
        assert translate(message, language=language) != message


@pytest.mark.parametrize("language", [language.code for language in LANGUAGES])
def test_dates_are_deterministic_for_every_language(language: str) -> None:
    assert "2026" in month_name("2026-07", language=language)
    assert "10" in date_name("2026-07-10", include_year=True, language=language)


def test_french_short_months_keep_june_and_july_distinct() -> None:
    assert month_name("2026-06", short=True, language="fr") == "juin 2026"
    assert month_name("2026-07", short=True, language="fr") == "juil 2026"


def test_unknown_messages_and_languages_fall_back_safely() -> None:
    assert translate("User-entered merchant", language="fr") == "User-entered merchant"
    assert translate("Overview", language="unsupported") == "Overview"
