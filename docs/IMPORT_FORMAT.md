# Bank transaction import format

## Bank exports

The in-app bank importer accepts strict original CSV layouts for supported North American and European providers. Do not reformat those files: open **Expenses**, choose **Import CSV**, choose the region and provider, and review the detected transactions before saving them. Expensetics uses the transaction date only. It does not store posting dates or card numbers. Mark recurring but occasional annual costs in the review grid to allocate each charge across 12 months.

European support currently covers Monzo, N26, Rabobank, Revolut Business,
Starling, Wise, and bunq. Localized semicolon/tab delimiters, decimal commas, and day-first dates
are handled only inside those explicit adapters. Revolut Personal is not the
same export: its current help documents PDF/Excel rather than CSV. See
[`BANK_IMPORT_COMPATIBILITY.md`](BANK_IMPORT_COMPATIBILITY.md) for exact accepted columns, official source
links, confidence levels, and the researched backlog. Rabobank account and
credit-card CSVs share one provider choice and are distinguished by strict
headers. Revolut Business accepts both account statements and its separate
Expenses CSV export.

Each bank format is an isolated parser registered in `finance_app/bank_import.py`. A new parser only needs to normalize the bank's columns into `ParsedBankRow` records and be added to `BANK_PARSERS`; review suggestions, duplicate checks, the UI, and SQLite persistence remain shared. Add parser tests with an anonymized fixture whenever a format is introduced or changed.

## Expensetics encrypted backups

Expensetics does not generate readable CSV mirrors. Create a password-protected `.expensetics` snapshot from **Settings → Encrypted backup** and restore it from the same settings panel on another computer. The snapshot is a complete, integrity-checked SQLCipher database with a password independent from the local vault password.

Bank CSVs remain user-supplied import sources. Expensetics reads them in place for the requested import and does not copy them into its data directory. Generic Excel and command-line imports are intentionally unsupported because every transaction must pass through the bank selection and review grid.
