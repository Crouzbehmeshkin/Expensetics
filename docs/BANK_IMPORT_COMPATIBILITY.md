# Bank CSV import compatibility

Research checked: 2026-08-11. This is a format contract, not a marketing list.
An institution is marked **supported** only when Expensetics has enough evidence
to validate its columns, date representation, and debit/credit semantics before
reading any transaction.

Bank exports can differ by country, account product, and online-banking version.
When a selected file does not match the documented contract, Expensetics stops
with a format error; it does not guess column meanings or currency conversion.

## Supported and tested

| Institution / product | Market | CSV contract used by the adapter | Date | Purchase semantics | Evidence |
|---|---|---|---|---|---|
| American Express consumer card | US | `Date, Description, Card Member, Account #, Amount` | `MM/DD/YYYY` | positive charge; negative payment/credit | [Amex confirms CSV downloads](https://www.americanexpress.com/us/customer-service/faq.download-export-transactions-software.html); [public raw sample](https://pkg.go.dev/github.com/mschilli/ynabler) |
| Apple Card | US | `Transaction Date, Clearing Date, Description, Merchant, Category, Type, Amount (USD)`; optional `Purchased By` | `MM/DD/YYYY` | `Type=Purchase` is an expense; payment/refund/credit excluded | [official export instructions](https://support.apple.com/en-lamr/102284); [public raw sample](https://gist.github.com/amazingandyyy/515d01d65d622b5733123dd0e2960cda) |
| BMO card export | CA | preamble, then `Item #, Card #, Transaction Date, Posting Date, Transaction Amount, Description` | `YYYYMMDD` | positive charge; negative credit | real user-supplied project sample, sanitized for tests |
| Bank of America deposit account | US | optional summary preamble, then `Date, Description, Amount, Running Bal.` | `MM/DD/YYYY` | negative withdrawal is inverted; positive deposit excluded | [public header sample](https://www.reddit.com/r/BankOfAmerica/comments/1fu3pzq/) |
| Capital One card | US/CA-compatible contract | `Transaction Date, Posted Date, Card No., Description, Category, Debit, Credit` | ISO or US numeric date | Debit is expense; Credit excluded | [Capital One confirms CSV](https://support.capitalone.ca/support-article-content-stream/how-to-view-transaction-history-and-past-statements); [public format reference](https://csvtoqbo.com/blog/csv-to-qbo-quickbooks-online) |
| Chase deposit account | US | `Details, Posting Date, Description, Amount, Type, Balance, Check or Slip #` | `MM/DD/YYYY` | negative withdrawal is inverted; positive deposit excluded | [Chase documents CSV](https://www.chase.com/content/dam/chaseonline/en/legacy/content/secure/sso/document/cco_account_activity_quick_reference-lores.pdf); [public importer](https://github.com/jbms/beancount-import) |
| CIBC account | CA | either headerless `date, merchant, debit, credit, account`, or headered `Transaction Date, Description, Withdrawals, Deposits` | ISO or US numeric date | withdrawal/debit is expense; deposit/credit excluded | public user export plus [Canadian format reference](https://ibill.ca/bank-statement-import) |
| Citi card/account | US | `Date, Description, Debit, Credit` | ISO or US numeric date | Debit is expense; Credit excluded | [public format reference](https://csvtoqbo.com/blog/csv-to-qbo-quickbooks-online) |
| Desjardins AccèsD | CA | official headerless 13-column positional account/Visa format | `YYYY/MM/DD` | account Withdrawal or Visa Advance is expense; Deposit/Reimbursement excluded | [official field specification and examples](https://www.desjardins.com/content/dam/pdf/en/business/accounts-treasury/accesd-affaires-guide.pdf) |
| Discover card | US | `Trans. Date, Post Date, Description, Amount, Category` | `MM/DD/YYYY` | positive charge; negative payment/credit | public raw statement/export examples; [statement layout corroboration](https://www.reddit.com/r/QuickBooks/comments/xv1pqq/qbo_export_disappeared_from_discover_card/) |
| RBC account | CA | `Account Type, Account #, Transaction Date, Cheque Number, Description 1, Description 2, CAD$, USD$` | ISO or US numeric date | negative CAD withdrawal is inverted; positive credit excluded; USD-only rows require manual conversion | [public verified mapping](https://partita.app/free/setup) |
| Rogers Bank card | CA | full 15-column activity export including date, reference, status, MCC, merchant, and amount | `YYYY-MM-DD` | positive approved charge; negative amount excluded | real user-supplied project sample, sanitized for tests |
| Scotiabank account | CA | signed 7-column export, or legacy withdrawal/deposit columns | ISO or US numeric date | signed negative/Withdrawal is expense; positive/Deposit excluded | [official CSV export help](https://www1.scotiaconnectuat.scotiabank.com/help/secured/en_US/file_download.htm); [public verified mapping](https://partita.app/free/setup) |
| TD account | CA | `Date, Transaction, Description, Withdrawals, Deposits, Balance`; legacy Debit/Credit headers accepted | ISO or US numeric date | Withdrawal is expense; Deposit excluded | [public raw-header report](https://www.reddit.com/r/actualbudgeting/comments/1la4xnr/issues_with_importing_csv_file_from_td_bank/) |
| U.S. Bank account | US | `Date, Transaction, Name, Memo, Amount` | `MM/DD/YYYY` | signed negative debit is inverted; positive credit excluded | [official CSV availability](https://www.usbank.com/customer-service/knowledge-base/KB0069323.html); [public file description](https://qbomaker.com/banks/us-bank) |
| Wells Fargo account | US | headerless `date, signed amount, reference fields, description` | US numeric date | signed negative withdrawal is inverted; positive deposit excluded | [official comma-delimited export help](https://www.wellsfargo.com/help/online-banking/activity-faqs/); [public format reference](https://csvtoqbo.com/blog/csv-to-qbo-quickbooks-online) |

### European adapters

European providers are split by evidence quality. Monzo publishes its exact
column set through an official staff channel. The other providers publicly
confirm that CSV export exists but do not publish a versioned consumer header
contract; their strict adapters are tested against sanitized, known-layout
fixtures and stop with a format error if an export differs. This is intentional:
the importer never guesses whether a localized number is a debit or credit.

| Institution / product | Market | How the online export works | Strict contract accepted by Expensetics | Evidence / confidence |
|---|---|---|---|---|
| Monzo personal, joint, and business | UK | In the mobile app, open account statements and choose CSV | published fields including `Transaction ID, Date, Type, Name, Category, Amount, Currency, Description`; day-first dates; negative amount is spending | [Monzo statement help](https://monzo.com/es/ayuda/usar-monzo/ayuda-extractos); [exact fields published by Monzo staff](https://community.monzo.com/t/csv-exports/89487/69) — **documented contract** |
| N26 | EU | In the web app, open Downloads / Account activity, select a custom range, and export CSV | `Booking Date` or `Date`, `Partner Name` or `Payee`, and `Amount (<currency>)`; day-first or ISO dates; negative amount is spending | [N26 export help](https://support.n26.com/en-eu/payments-transfers-and-withdrawals/balance-and-limits/how-to-get-bank-statement-n26) — **fixture-validated layout** |
| Rabobank account and credit card | NL | In Rabo Online Banking or Rabo Business Banking, download CSV Transactions or CSV Creditcard | account: fixed 26-column Dutch or English contract; card: fixed 13-column Dutch contract; ISO dates and decimal commas; negative amount is spending | [official formats page](https://www.rabobank.nl/en/business/support/online-bankieren/formats), [transaction specification and sample index](https://media.rabobank.com/asset/adf64278-7c74-4e10-9128-e13892bbf0c3/2026-06-03-Specifications.pdf) — **official specification and sample** |
| Revolut Business | UK/EEA | Export either an account statement or the Expenses CSV from Business | statement: `Type, Product, Started Date/Completed Date, Description, Amount, Currency, State`; Expenses: the official transaction/expense ID, status, description, currency, amount, account, tax, and category columns; only completed negative rows are spending | [statement help](https://help.revolut.com/business/help/managing-my-business/viewing-my-account-statements/how-to-get-a-monthly-statement/); [official Expenses CSV columns](https://help.revolut.com/business/help/managing-my-business/expenses/introduction-to-expenses/what-information-does-the-expenses-csv-export-contain/) — **documented Expenses contract; fixture-validated statement contract** |
| Starling | UK | In the mobile app, open Statements and choose CSV | `Date, Counter Party, Reference, Type, Amount (<currency>)`; day-first or ISO dates; negative amount is spending | [Starling statement help](https://help.starlingbank.com/joint/topics/account-management/how-do-i-receive-my-statements/) — **fixture-validated layout** |
| Wise balance account/card | Europe and multi-region | On web or app, open Statements, choose balance/date range, then CSV | official 19-column statement headed by `TransferWise ID, Date, Amount, Currency, Description, Payment Reference, Running Balance`, with exchange, payer/payee, merchant, card, attachment, note, and fee fields; negative amount is spending | [Wise statement help](https://wise.com/help/articles/2736049/how-do-i-download-a-statement); [official public CSV response](https://www.postman.com/transferwise/transferwise-s-public-workspace/request/8269os0/get-statement-csv) — **documented contract** |
| bunq | EEA | In the app or web client, open an account statement and export CSV | `Date, Amount, Account, Description` plus optional counterparty/name; comma, semicolon, or tab delimiters; decimal point or decimal comma; negative amount is spending | [bunq statement help](https://help.bunq.com/en-ie/articles/how-do-i-export-a-bank-statement) — **fixture-validated layout** |

Every row in this table has a sanitized regression fixture in
`tests/fixtures/bank_csv`. BMO and Rogers fixtures preserve the structure of
real files supplied to this project. Other fixtures use fictional transactions
against the cited real column contracts; no personal financial records are
committed to the repository.

### Asian adapters

Asian CSV exports are often localized and Windows-oriented. Expensetics accepts
UTF-8 as usual and detects Japanese Shift-JIS (CP932) only when decoding yields
Japanese text. Adapters validate only the fields they consume; unrelated columns
may be added, removed, or reordered when the bank's format is header-based.

| Institution / product | Market | How the online export works | Required contract accepted by Expensetics | Evidence / confidence |
|---|---|---|---|---|
| MUFG BizSTATION all-transactions report | JP | Download all account activity as CSV from statement inquiry | official positional record types; transaction rows require record type, `YYYY.M.D` date, transaction type, description, withdrawal, and deposit; Shift-JIS | [official two-page output specification](https://web.bizstn.bk.mufg.jp/biz/ikou2026/contents/guide/pdf/mei_syoukai_zen_csv.pdf) - **documented contract** |
| Mizuho Business WEB account activity | JP | Download inquiry results as CSV from the account-activity result | named subset `勘定日, 出金（円）, 入金（円）, 摘要`; Japanese Gregorian date text and Shift-JIS; other columns ignored | [official inquiry manual, CSV layout on pp. 28-29](https://www.mizuhobank.co.jp/corporate/ebservice/b_web/pdf/zandaka.pdf) - **documented contract** |
| SMBC Direct account activity | JP | Web Passbook customers download the displayed account activity as CSV | named subset `日付, お引出し, お預入れ` and either `お取り扱い内容` or `お取引内容`; Gregorian Japanese or slash date; other columns ignored | [official SMBC Direct help](https://www.smbc.co.jp/direct/sousa/help_kouza/10.html) documents that CSV mirrors the on-screen fields and retains the withdrawal dash - **fixture-validated named subset** |

The Japanese adapters treat withdrawals as expenses and deposits as excluded
credits. They do not perform currency conversion or infer a sign from the
description.

## CSV export verified, adapter awaiting an anonymized sample

These institutions publicly offer CSV, but their public documentation does not
fully pin down the current consumer columns, product variants, date rules, and
amount signs. They remain unavailable in the selector until a sanitized raw
export is available.

| Institution / product | Market | What is verified | Why it is not enabled yet |
|---|---|---|---|
| Tangerine | CA | [official Excel CSV download](https://www.tangerine.ca/en/faq/how-do-i-download-transactions) | exact account/card headers and sign rules are not published |
| EQ Bank savings/card | CA | [official account CSV download](https://www.eqbank.ca/about-us/help/common-questions) | savings and card exports appear to be distinct; raw layouts needed |
| Simplii Financial account/card | CA | CSV is reported in online banking | no authoritative or anonymized raw file in the project |
| National Bank | CA | [official business CSV export](https://www.nbc.ca/business/help-centre/transactions/account-management/export-transactions-accounting-software.html) | consumer/card schema and amount signs are not published |
| American Express Canada | CA | Amex offers downloadable transaction history | the tested US contract must not be assumed identical for Canada |
| Ally Bank | US | [official CSV account-activity download](https://www.ally.com/help/bank/account-information/) | exact current columns and sign convention are not published |
| SoFi checking/savings/card | US | [official checking/savings CSV export](https://support.sofi.com/hc/en-us/articles/12905767525773-Can-I-export-my-Checking-and-Savings-transactions) | checking, Relay, and card exports may differ; raw layouts needed |
| PNC consumer/business cards | US | [official business-card CSV download](https://www.pnc.com/en/small-business/borrowing/business-credit-cards/businessoptions-online-toolkit.html) | no verified consumer/card row contract |
| PayPal Activity Download | US/CA | [official, fully documented customizable CSV](https://developer.paypal.com/docs/reports/online-reports/activity-download/) | multi-currency rows and user-customizable columns need an explicit import policy |
| Venmo | US | [official downloadable CSV statements](https://help.venmo.com/cs/articles/transaction-history-vhel281) | exact current statement columns and transfer semantics are not published |
| Lloyds Bank business | UK | its [online-banking guide](https://www.lloydsbank.com/assets/assets-business-banking/pdfs/accessing-online-banking-services.pdf) documents CSV/QIF transaction download | the consumer `midata` and business exports are separate products; sanitized raw files are needed for each |
| NatWest `midata` | UK | NatWest documents a [comma-delimited 12-month `midata` download](https://www.natwest.com/support-centre/payments/general/do-you-offer-the-ability-to-obtain-midata.html) | `midata` and the [standard Excel/PDF transaction search exports](https://www.natwest.com/support-centre/help-with-your-card/transactions/how-do-i-search-for-transactions-online.html) are different; exact headers and debit signs need a fixture |
| ING Germany | DE | [ING documents transaction export](https://www.ing.de/hilfe/banking/) | the public help page does not identify a stable file format or column contract |
| American Express consumer cards | UK/EEA | [Amex UK documents downloadable statement file types](https://www.americanexpress.com/en-gb/customer-service/payments-and-billings/faq.paper-statement.html) | the exact European consumer CSV option, columns, locale, and amount signs are not published; the US adapter is not reused |
| Curve card | UK/EEA | [Curve documents filtered transaction-history CSV export from the app](https://help.curve.com/spending-with-curve-SyUTsUIUxe) | Curve does not publish a stable column/date/sign contract; a sanitized original export is needed |
| ICS / ABN AMRO business credit cards | NL | [My ICS Business confirms transactions are CSV-only](https://abnamro.icsbusiness.nl/service/portal/) | the portal documentation does not publish the CSV columns or debit/refund signs; a sanitized original export is needed |
| Standard Chartered Singapore personal accounts/cards | SG | [online banking provides a Download CSV action](https://www.sc.com/sg/help/faqs/bank-with-us-faqs/) | current account/card column names and amount signs are not published |
| DBS IDEAL | SG | [DBS documents CSV statements with column headers](https://www.dbs.com.sg/documents/1038650/59715357/additional-faqs.pdf/) | current header names and debit/credit semantics are not published |
| UOB Infinity | SG | [UOB documents account-list and transaction-detail CSV exports](https://www.uob.com.sg/assets/pdfs/infinity-beforeafter-maker.pdf) | the selectable columns make a single required transaction contract unclear |
| Maybank2u Singapore | SG | [Maybank documents up to 12 months of CSV transaction history](https://www.maybank2u.com.sg/en/personal/banking-services/self-service/online-mobile-banking/online-banking.page) | current download headers and amount signs are not published |
| ICICI Bank account/card statements | IN | [ICICI documents downloadable PDF and CSV detailed statements](https://www.icicibank.com/online-services/clicktopayloan) | account and card products need separate anonymized raw exports |

## Verified non-CSV alternatives

These products currently document downloadable records, but not a CSV suitable
for this importer. They are deliberately absent from the bank selector.

| Institution / product | Documented download | Import status |
|---|---|---|
| Revolut Personal | [PDF or Excel](https://help.revolut.com/help/profile-and-plan/managing-my-account/account-statement-per-chosen-currency) | not supported; Expensetics does not relabel Excel as CSV |
| ABN AMRO personal | [PDF, TXT, MT940, XLS, and CAMT.053](https://www.abnamro.nl/en/personal/payments/credit-and-debit-transactions/download.html) | not supported; a future XLS or CAMT importer should be a separate adapter |

## Popular issuers not currently supported

No sufficiently reliable current raw CSV contract was found during this
research. This does not necessarily mean the institution offers no CSV; it
means Expensetics cannot safely promise an adapter yet.

### Canada

- PC Financial / PC Mastercard
- Canadian Tire Bank / Triangle Mastercard
- MBNA Canada (including Amazon.ca Mastercard)
- Neo Financial
- KOHO
- Brim Financial
- Manulife Bank
- Wealthsimple chequing and credit card. Wealthsimple's documented
  [activity CSV currently excludes chequing](https://help.wealthsimple.com/hc/en-ca/articles/35654428540571-Request-a-custom-statement).

### United States

- USAA
- Navy Federal Credit Union
- Synchrony-issued cards, including Amazon Store Card
- Barclays US cards
- Fidelity Rewards / Elan Financial Services
- Truist
- Regions Bank
- Fifth Third Bank
- Citizens Bank
- Huntington Bank

Amazon Visa exports produced by Chase are supported only when they match the
tested Chase contract. Store cards issued by Synchrony are not supported.

### Europe

No sufficiently reliable, current public CSV contract was found in this pass
for the providers below. This is **not** a claim that they never export CSV;
online-banking region, product, and interface versions differ. It means an
Expensetics adapter would be guesswork without an anonymized original file.

- Barclays UK and Barclaycard
- HSBC UK and HSBC Continental Europe
- Santander UK and Santander Europe
- Halifax and Bank of Scotland
- Nationwide Building Society
- Deutsche Bank and Postbank
- Commerzbank
- BNP Paribas
- Crédit Agricole
- Société Générale
- BBVA
- CaixaBank
- UniCredit
- Intesa Sanpaolo
- Nordea
- Handelsbanken
- Danske Bank
- DNB
- Swedbank

### Asia

- DBS/POSB consumer accounts and cards
- OCBC consumer accounts and cards
- UOB consumer accounts and cards
- HSBC and Hang Seng Hong Kong
- Bank of China (Hong Kong) consumer accounts and cards
- Rakuten Bank and Sony Bank
- SBI, HDFC Bank, Axis Bank, and Kotak Mahindra Bank
- KB Kookmin, Shinhan, Hana, and Woori Bank
- Maybank Malaysia and CIMB Malaysia
- BPI, BDO, and Metrobank Philippines

Several of these providers document downloadable statements or business CSVs,
but no sufficiently stable public consumer header-and-sign contract was found.
They remain absent from the selector until a sanitized original export can pin
down the minimum required fields.

## Adding an institution safely

Provide an anonymized original CSV containing at least one purchase and one
payment, refund, or deposit. Keep the header and date/amount formatting intact;
replace names, account numbers, references, merchants, and values. A new adapter
then requires:

1. strict format validation;
2. explicit date and amount semantics;
3. a sanitized fixture preserving the raw structure;
4. purchase and credit/deposit regression assertions;
5. a selected-bank mismatch test;
6. browser verification of selection, upload, review, and exclusion behavior.
