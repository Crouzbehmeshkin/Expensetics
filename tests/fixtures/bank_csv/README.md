# Sanitized bank CSV fixtures

These synthetic fixtures contain no real account or transaction data. They
encode only the minimum verified column layout needed to regression-test each
adapter.

- American Express: date, description, card member, masked account, and
  signed card amount columns.
- Apple Card: transaction/clearing date, description, merchant, category,
  transaction type, USD amount, and optional purchaser columns.
- Capital One: transaction/posted date, card suffix, description, category,
  debit, and credit columns.
- Bank of America: optional summary preamble followed by date, description,
  signed amount, and running balance columns.
- BMO: the real project-supplied preamble/header structure, sanitized with
  fictional card and transaction values.
- Chase: posting date, description, signed amount, transaction type, balance,
  and optional check/slip reference columns.
- CIBC: the five-column headerless personal export reported as transaction
  date, merchant, debit, credit, and account.
- Citi: date, description, debit, and credit columns.
- Desjardins: the official headerless 13-column AccèsD layout, including the
  `YYYY/MM/DD` date and positional withdrawal/deposit fields.
- Discover: transaction/post date, description, signed card amount, and
  category columns.
- RBC: account metadata, transaction date, two description columns, and
  separate CAD/USD amount columns.
- Rogers: the real project-supplied card export structure, sanitized with
  fictional account and transaction values.
- Scotiabank: the current signed-amount account export. The adapter also accepts
  the older withdrawal/deposit-column layout.
- TD: date, transaction type, description, withdrawals, deposits, and balance
  account export. The older Debit/Credit header variant remains accepted.
- U.S. Bank: date, debit/credit flag, name, memo, and signed amount columns.
- Wells Fargo: its headerless date, signed amount, two reference fields, and
  description layout.
- Monzo: the fields published by Monzo staff, including transaction ID,
  day-first date, name, category, signed amount, currency, and description.
- N26: booking/value date, partner, payment reference, account name, and a
  currency-labelled signed amount column.
- Revolut Business: product, started/completed timestamps, description,
  signed amount, currency, state, and balance. A second fixture follows the
  provider's officially documented Expenses CSV columns.
- Starling: day-first date, counterparty, reference, transaction type, signed
  currency-labelled amount, balance, and spending category.
- Wise: the complete 19-column header returned by Wise's official public CSV
  statement example, including exchange, counterparty, card, note, and fee fields.
- Rabobank: the official 26-column account-transaction contract and separate
  13-column credit-card contract, with ISO dates, decimal commas, and explicit
  debit/credit signs. Both documented Dutch and English account headers are
  covered. Values are synthetic; headers and semantics come from Rabobank's
  published specification and account sample archive.
- bunq: a semicolon-delimited European-locale fixture with day-first dates and
  decimal-comma signed amounts.
- MUFG BizSTATION: the official Shift-JIS positional record types for header,
  transaction, footer, and final rows; only the transaction date, type,
  description, withdrawal, and deposit positions are consumed.
- Mizuho Business WEB: the official Shift-JIS named columns, Japanese-era text
  labels, Gregorian Japanese date rendering, and separate withdrawal/deposit
  amounts. Extra published columns are present but not required by the parser.
- SMBC Direct: the consumer screen-mirroring columns documented by SMBC,
  including Gregorian date, withdrawal, deposit, and transaction description.

Every adapter validates its selected format before parsing. Credits, payments,
deposits, and unsupported RBC USD rows enter review as excluded rather than
being silently treated as expenses.

European fixtures are synthetic representations of the strict layouts accepted
by their adapters. Monzo, Wise, Revolut Business Expenses, and Rabobank publish
their contracts; other providers publicly document CSV availability but not a
stable consumer header contract. Every adapter rejects variants it does not
recognize, and the compatibility document records that confidence difference.

Asian fixtures are likewise synthetic and contain no customer data. MUFG and
Mizuho publish formal CSV contracts. SMBC documents CSV availability and that
the download mirrors its displayed fields; its fixture therefore represents the
strict named subset accepted by Expensetics rather than a claimed full schema.
