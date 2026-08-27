# Contributing to Expensetics

Expensetics welcomes focused contributions that improve transaction capture, data reliability, privacy, portability, or maintainability. The highest-priority contribution area is support for additional bank, credit-union, and card-provider CSV exports.

## Protect financial data

Never commit, attach, or paste a real statement or transaction export. Real files may contain account identifiers, card suffixes, merchant history, balances, locations, references, and other personal information even when obvious names are removed.

Use a synthetic fixture that preserves only the structural properties needed by the parser:

- encoding and delimiter;
- preamble and header names;
- column order when the format is positional;
- representative date and decimal formatting;
- debit, credit, payment, and purchase sign semantics; and
- optional or repeated rows needed to verify behavior.

Replace every account identifier, reference, merchant, amount, balance, and free-text field with fictional values. If you are uncertain whether a sample is safe, do not publish it.

Security concerns should follow [SECURITY.md](SECURITY.md), not a public implementation issue.

## Proposing a bank or card format

Before implementing an adapter, check the supported and researched backlog in [`docs/BANK_IMPORT_COMPATIBILITY.md`](docs/BANK_IMPORT_COMPATIBILITY.md). A useful proposal identifies:

1. institution, country, and account or card product;
2. where the export is downloaded in the provider's online banking interface;
3. whether the source is CSV and its encoding and delimiter;
4. the exact minimum columns needed for transaction date, vendor/description, and amount;
5. date format and amount sign convention;
6. how purchases, deposits, refunds, card payments, and pending/declined rows are represented; and
7. authoritative evidence, preferably an official specification or help page.

Do not infer one product's contract from another product at the same institution. Consumer accounts, business accounts, and credit cards often use different formats.

## Implementing an adapter

Bank adapters live in [`finance_app/bank_import.py`](finance_app/bank_import.py) behind the explicit `BANK_PARSERS` registry. Keep a new adapter deterministic and isolated:

- validate the selected format before parsing rows;
- require only fields the adapter consumes and ignore unrelated extra columns;
- use an exact documented date contract rather than locale guessing;
- normalize debit/credit signs into the existing positive-purchase model;
- mark credits, payments, deposits, declined rows, and unsupported currencies as excluded with an explanation;
- preserve the best available merchant text without storing account or card numbers;
- reject ambiguous or malformed formats instead of silently guessing; and
- keep suggestions local and history-based.

Register the provider in `BANK_PARSERS` and its regional group, then add a synthetic fixture under [`tests/fixtures/bank_csv`](tests/fixtures/bank_csv). Update `BANK_FIXTURE_CASES`, the stable registry assertion, strict-date and malformed-header coverage where applicable, the fixture README, and the compatibility document.

Prefer one provider/export contract per pull request. Closely related variants may share one provider choice only when strict headers distinguish them deterministically.

## Verification

Create the isolated development environment and install Chromium once:

```powershell
.\scripts\setup.ps1
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/dev.lock
.\.venv\Scripts\python.exe -m playwright install chromium
```

Run the same quality gates used by GitHub Actions:

```powershell
.\.venv\Scripts\python.exe -m compileall -q finance_app tests
.\.venv\Scripts\python.exe -m pytest -m "not e2e" --cov=finance_app --cov-report=term-missing
.\.venv\Scripts\python.exe -m pytest -m e2e
```

The non-E2E suite includes Hypothesis property tests for financial arithmetic,
month movement, similarity scoring, liability payments, forecasting, and atomic
reviewed imports. A failing property is automatically reduced to a minimal
reproducible example; preserve it as a focused regression test when it reveals
a defect.

On macOS or Linux, replace `.\.venv\Scripts\python.exe` with `./.venv/bin/python` and run `sh scripts/setup.sh` for the runtime environment.

Do not weaken parser validation, authorization boundaries, encryption, duplicate protection, or test coverage to make a fixture pass. Explain any behavior with financial consequences in the pull request.
