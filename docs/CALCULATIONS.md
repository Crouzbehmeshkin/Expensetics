# Calculation rules

Expensetics performs financial arithmetic in integer cents. SQLite stores amounts as integers; display formatting is applied only after calculations.

## Settlements

An expense is stored as positive cents. A settlement is stored as negative cents and is explicitly identified as a `Settlement` transaction. The database enforces that sign/type relationship.

Settlements are not income. They offset only the category and optional subcategory assigned to them, then flow through sums that derive from spending:

```text
net category spending = expenses + settlements
outgoing cash = sum of signed transactions
net cash flow = income - outgoing cash
```

For example, a $2,000 Travel expense and a $1,000 Travel settlement produce $1,000 of net Travel spending and $1,000 of outgoing cash.

## Annual expenses

An entry marked **Annual allocation** is allocated from its transaction month through the following 11 months. This applies symmetrically to expenses and settlements.

For an amount of `C` cents:

1. `sign = -1 if C < 0 else 1`
2. `base, remainder = divmod(abs(C), 12)`
3. every month receives `sign * base` cents
4. the first `remainder` months receive one additional cent with the same sign

This guarantees that the 12 monthly values reconcile exactly to the recorded expense. Multiple annual expenses in a category are summed after allocation. The cash-spending total remains recorded in the actual transaction month; only monthly-equivalent trends use the allocation.

Example: two $600 car-service transactions in January and July each contribute $50 per month for 12 months. Their combined monthly equivalent is $100 from July onward while both allocations overlap.

## Budgets

Category budgets are independent ceilings and their sum is never inferred to be an overall spending limit. The Overview “Where spending moved” chart shows a budget line only when the user explicitly saves an optional total monthly limit. That overall limit remains constant for every month until changed or cleared; clearing it removes the line. Category budget markers and category-detail budget lines continue to use the effective category budget history.

## Estimated net worth

An estimated net-worth point is derived from the latest actual snapshot on or before the target date:

```text
estimated net worth
= last actual net worth
+ recorded income after the snapshot through the target date
- recorded expenses after the snapshot through the target date
```

The target date is the selected month's last day, capped at today. Estimates never extend into the future. Transactions on the actual snapshot date are excluded because the snapshot is treated as the closing balance for that date. An actual snapshot represents its entire calendar month in the chart and dashboard; estimated points begin in the following month. Cash flow recorded after a mid-month snapshot is carried into that following estimate, so no value is lost and an actual and estimated point can never share the same month label.

Estimated points are labeled in the interface. They do not invent an assets/liabilities split and are not persisted. Saving another actual snapshot replaces the projection anchor from that date onward.

The chart connects recorded actual snapshots with a solid line and keeps the cash-flow estimates on a separate dashed line. Its dotted trend is an ordinary least-squares line fitted only to actual snapshots, with each snapshot's calendar ordinal as `x` and net worth cents as `y`. Estimated points never influence the fit. A linear fit is intentional: it is auditable and avoids the edge swings that a high-degree polynomial can introduce with sparse personal data. The y-axis extends at least 10% of the largest displayed net-worth magnitude above and below the visible values, with a minimum $10,000 padding on each side; bounds are rounded outward to $1,000 increments.

## Estimated income

Income estimates are planning values and never enter the transaction ledger or net-worth calculation. When a selected month has no recorded income, Overview uses the estimate as its main Income value and shows a projected net cash flow based on it. Both values are explicitly labeled as estimated; the repository also retains the recorded-only income and cash-flow values for auditability. Once the month has recorded income, the recorded value takes precedence.

The calculated estimate uses at most the six most recent months before the target month that contain recorded income. A month with no income entry is treated as unknown, not as zero.

With month ordinals `x`, income cents `y`, and the most recent observation at `x_latest`, the weight is:

```text
weight = 0.70 ^ (x_latest - x)
```

Expensetics performs a weighted least-squares linear regression and evaluates it at the selected target month. This is normally one month after the latest observation, but a later selected month is projected at its actual month ordinal rather than silently treated as the next month. A single observation is used as a flat baseline. Negative projections are clamped to zero and the final result is rounded once to integer cents. A user override is stored separately and clearly labeled; resetting it reveals the calculated value again.

## Budgets

Budgets are effective-dated category plans. The first saved plan is the all-time default. A later revision applies from its effective month until another revision. A revision can instead replace every month, or be scoped to one calendar year; year-only changes restore the previously effective plan on the following January 1.

Budget-versus-actual uses the same monthly-equivalent category amounts as spending trends, including deterministic 12-month allocation of annual expenses. The selected month changes the comparison period, not the underlying persistence rule.

On a stacked spending chart, the budget is a separate, non-stacked step line. The Overview total-spending chart uses only the explicitly entered total monthly limit; it never infers that limit by summing category budgets. A category-detail chart uses that category's effective budget. Missing budgets leave a gap rather than being treated as zero. The line does not change, cap, or otherwise participate in the spending stack.

Stacked-area smoothing is presentation-only. ECharts receives every category's exact monthly cents as an independent band and derives the cumulative boundaries. The curve setting does not interpolate values used by totals, tooltips, comparisons, or exports.

## Transaction insights

Transaction insights are deterministic observations over the twelve months ending in the selected month. They never modify records. A purchase count means a raw expense transaction; settlements are excluded from visit/frequency counts and shown separately. Annual allocations are not repeated as additional transactions. A calendar month with no recorded transactions of any kind is treated as unknown and excluded from analytical baselines, not interpreted as zero activity.

The displayed average purchase divides the selected month's exact purchase cents by its purchase count and rounds to the nearest cent. It excludes settlements.

The notable-signal panel evaluates all categories and does not change when the category selector changes. The selector controls only the category-specific charts beneath it; the settlement section is global and separately labeled.

Merchant identity uses the normalized bank vendor key when one exists and otherwise the normalized description. The original latest merchant label remains visible. Current values are compared only with earlier transactions for the same identity.

### Recurring charges and timing

A merchant is treated as recurring for the selected month only when it has exactly one purchase in the current month and exactly one purchase in at least four of the preceding six months. The usual amount and day-of-month are the medians of those active prior months.

A recurring price increase is shown when the current charge is at least both 10% and $5 above its usual amount. A timing shift is shown when the circular day-of-month distance is at least five days; the circular comparison treats the end and start of adjacent months as close together. A recurring charge is considered stable when its timing is within three days and its amount is within the greater of $2 or 5% of usual. A stable-rhythm card appears only when there are at least two recurring candidates and every candidate is stable; a partial result is not presented as reassuring.

### Robust amount outliers

An amount outlier needs at least seven prior purchases at the same merchant. Expensetics uses the modified z-score:

```text
score = 0.6745 × (current amount - historical median) / median absolute deviation
```

The signal requires an absolute score above `3.5` and a material difference of at least the greater of $10 or 50% of the historical median. A zero median absolute deviation is left unlabeled because a robust score cannot be calculated without inventing variance. When a recurring increase also qualifies as a robust outlier, the more specific outlier label takes precedence so one event is not reported twice.

### Activity changes

Merchant activity is compared with the median monthly visit count over the active months among the preceding six calendar months. Months with no records anywhere in the ledger are excluded; a merchant's absence during an otherwise active month remains a zero visit count. A signal requires activity in at least two prior active months, more visits than in the most recent prior active month, at least three current visits, at least two more visits than usual, and at least twice the usual frequency. Category-level frequency also must exceed the most recent prior active month and uses the same minimum history and count thresholds, with a minimum increase of 1.5 times the usual count. This prevents an unchanged July-to-August count from being described as an August increase merely because older months were quieter.

At most six observations are displayed. Within each rule type, larger material changes sort first and names break ties deterministically. No observation is produced when its history or materiality threshold is not satisfied.

## Bank-import subcategory suggestions

Import suggestions never combine a category from one evidence source with a subcategory from another. The ordered evidence is:

1. a previously confirmed canonical vendor mapping, preferring the same bank and then other banks
2. an exact historical description or normalized vendor match
3. the selected bank adapter's identified subcategory
4. a historical description with a character-trigram Sørensen–Dice score of at least `0.42`
5. the selected category's most-used subcategory

For two trigram sets `A` and `B`, similarity is `2 × |A ∩ B| / (|A| + |B|)`. Similarity is restricted to the bank-selected category unless that category is only `Other`; in that case a similar historical record may supply its category/subcategory pair. Similarity ties are resolved by usage count, recency, description, and subcategory. Most-used ties are resolved by recency and then name. These are suggestions only and remain editable in the review grid.

## Loans and matched payments

Every loan projection begins at an explicit observed anchor:

```text
anchor = current balance + balance-as-of date
projection inputs = rate + remaining amortization + payment frequency + current payment
```

Changing today's rate or terms never recalculates history from the original principal. The original principal and first-payment date remain provenance fields; only payments strictly after the balance-as-of date affect the projected balance. If the current payment is left blank, it is calculated from the anchor balance and remaining amortization.

The contractual monthly payment uses the selected interest convention. For ordinary monthly compounding:

```text
monthly rate = annual percentage rate / 12
periodic rate = (1 + monthly rate)^(12 / payments per year) - 1
payment = balance × periodic rate × (1 + periodic rate)^periods
          / ((1 + periodic rate)^periods - 1)
```

For Canadian semi-annual compounding, the stated nominal annual rate `j` is converted to an equivalent monthly rate before applying the same payment formula:

```text
monthly rate = (1 + j / 2)^(1 / 6) - 1
```

The periodic conversion then uses the same annual growth factor. Monthly, semi-monthly, biweekly, and weekly payments use 12, 24, 26, and 52 periods per year. Accelerated biweekly is exactly one half of the calculated monthly payment; accelerated weekly is exactly one quarter. Zero-interest loans divide the anchor balance by the number of periods. Contractual payments are converted to a monthly cash equivalent with `payment × periods per year / 12` for month-level charts and projections. Payment and balance results use integer cents with half-up rounding.

Fixed and variable rate types are recorded explicitly. Long-term payoff projections assume the manually entered rate remains unchanged beyond the current rate term; the interface states this rather than guessing a renewal rate. The displayed balance is therefore a deterministic estimate anchored to a user-entered lender balance, not a claim to reproduce an undisclosed lender ledger.

An imported payment series is used only after the user links an exact normalized vendor key or description to a loan. For each due month, the matched debit total replaces the contractual payment; months without a match retain the contractual payment. Interest is rounded to cents before each payment is applied. The displayed recent payment is an exponential average of the last six observed monthly totals using the same `0.70` decay.

Payoff time is simulated month by month from the estimated balance using that recent payment and the selected interest convention. If the payment does not exceed first-month interest, no payoff date is reported. This projection explicitly assumes the entire matched bank debit is principal and interest; escrow, insurance, fees, prepayments outside the matched series, renewal rates, and future variable-rate changes are not inferred.

### Liability insights

Insights reuses the same month-by-month liability calculation; it does not maintain a second balance formula. Each month-end balance uses matched imported payments where they exist and contractual payments for unmatched due months. Months before a loan starts are absent rather than treated as a zero balance.

The payment chart keeps provenance explicit:

```text
observed payments = exact monthly total of linked imported debits
contractual fallback = stored scheduled payment for a due month with no match
```

These series are displayed separately and never combined under an "actual" label. Per-loan repayment pace is the non-negative average monthly reduction between the first and last available balance in the visible window:

```text
repayment pace = max(0, (first balance - last balance) / elapsed month intervals)
```

The total repayment pace is the sum of those per-loan values. This prevents a newly added loan from being mistaken for negative repayment on an existing loan. Principal repaid is original principal minus the current calculated balance, clamped between zero and the original principal.
