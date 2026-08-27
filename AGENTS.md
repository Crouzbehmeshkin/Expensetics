# AGENTS.md

## Project Context

This repository contains a local-first personal finance tracker.

Before making meaningful changes, read the local `PROJECT_SPEC.md` when it is present. The file is intentionally untracked.

The primary product goal is not feature breadth. It is an exceptionally fast, polished manual expense-entry workflow backed by simple, reliable local data.

---

## Core Principles

### 1. Protect the entry workflow

The most important UX is:

```text
N -> amount -> Tab -> description -> Tab -> Enter
```

Do not add required fields or extra interactions unless absolutely necessary.

Prefer defaults, history-based suggestions, and optional details.

### 2. Keep the UI refined

This application must not look like a generic Python dashboard.

Prefer:

- restrained styling
- subtle borders
- excellent spacing
- compact components
- clear typography
- responsive layout
- polished focus/hover states

Avoid:

- large colorful cards
- excessive shadows
- unnecessary gradients
- clutter
- noisy sidebars
- excessive charts
- dropdowns for common categories

### 3. Keep the architecture simple

Prefer understandable Python over abstraction-heavy architecture.

Do not introduce:

- microservices
- cloud infrastructure
- Docker unless later requested
- message queues
- remote databases
- unnecessary async complexity
- JavaScript frameworks unless a required interaction cannot be implemented cleanly in NiceGUI

### 4. Local-first by default

Do not add:

- telemetry
- analytics SDKs
- third-party tracking
- external APIs
- cloud storage
- authentication

unless explicitly requested.

### 5. SQLite is the source of truth

Do not treat CSV as the primary transactional store.

Production data must use SQLCipher-encrypted SQLite. Do not persist readable CSV mirrors. Create portable encrypted backups only when the user explicitly requests one.

### 6. Preserve data portability

Keep the relational schema and versioned encrypted backup format clean and portable.

Avoid opaque serialized blobs for normal financial fields.

---

## Preferred Stack

Use:

- Python
- NiceGUI
- SQLite
- Pandas where useful
- standard Python libraries where practical

Keep dependencies modest.

If a new dependency is proposed, justify why it materially improves the application.

---

## Coding Style

Write straightforward, readable Python.

Prefer:

- type hints
- dataclasses or simple models where useful
- small focused functions
- explicit names
- centralized database access
- centralized export logic
- clear separation between UI and persistence

Avoid clever metaprogramming.

Avoid abstraction for abstraction's sake.

Comments should explain non-obvious decisions, not restate obvious code.

---

## Data Safety

Financial records are important.

For any operation that mutates transactions:

1. validate input
2. perform SQLite mutation safely
3. commit only when valid
4. commit only to the encrypted database
5. surface errors clearly

For delete operations, require intentional confirmation in the UI.

Do not silently discard user-entered data.

---

## Database Rules

Use one transactions table across all dates.

Do not create:

- one table per month
- one CSV per month as the primary data model
- duplicated aggregation tables unless there is a demonstrated performance need

Derive month/year from transaction date.

Use stable IDs.

Use migrations or a small schema-version mechanism once schema changes begin.

---

## Description Normalization

When learning description/category preferences:

- preserve the user's original description for display
- use a normalized form for matching
- normalization may lowercase, trim whitespace, and collapse repeated spaces
- do not aggressively rewrite merchant names
- category prediction should be deterministic and explainable

Prefer a simple historical-frequency model before adding more complex logic.

---

## Category UX

Common categories must be visible as one-click chips/buttons.

Do not replace them with a dropdown.

A compact "More" mechanism is acceptable for uncommon categories.

The predicted category may be preselected but must always be easy to override.

---

## Autocomplete UX

Description autocomplete is a critical feature.

Requirements:

- fast prefix matching
- history-derived suggestions
- frequent items ranked higher
- `Tab` accepts suggestion
- mouse selection works
- keyboard navigation works
- no perceptible lag for normal personal datasets

Do not implement autocomplete in a way that requires a network request.

---

## Keyboard Behavior

Keyboard interaction is first-class functionality.

Important shortcuts:

- `N` opens Add Expense from normal app context
- `Escape` closes Add Expense
- `Tab` navigates/accepts description autocomplete appropriately
- `Enter` saves once the required transaction fields are valid

Be careful not to trigger global shortcuts while the user is typing in inputs.

Manage focus deliberately after opening dialogs and after autocomplete completion.

---

## Styling Rules

Prefer custom CSS where needed rather than accepting default NiceGUI appearance.

Use a small design system:

- spacing scale
- border radius scale
- neutral colors
- one restrained accent color
- typography hierarchy
- common button styles
- common chip styles

Keep the UI visually consistent.

Do not scatter arbitrary inline styles everywhere if a reusable CSS class is more appropriate.

---

## Analytics Rules

Do not let travel or one-off expenses distort normal cost-of-living analysis.

When showing spending trends, make it easy to distinguish:

- total spending
- living spending
- discretionary spending
- travel
- one-off

Prefer horizontal category bars over decorative charts.

Add analytics only when they help answer a real question.

---

## Bank CSV Import

Imports must use the bank-specific in-app workflow. Require explicit bank selection, parse only the minimum documented columns, and show the review grid before persistence. Keep adapters isolated and deterministic, report ambiguous rows, verify totals, and prevent duplicates. Do not add a generic Excel or command-line importer.

---

## Testing

Prioritize correctness tests for:

- transaction CRUD
- monthly totals
- category totals
- expense-type totals
- category prediction
- autocomplete source data
- encrypted migration, backup, restore, rekey, and deletion
- bank-adapter parsing and reviewed import
- duplicate prevention

Whenever fixing a bug in financial calculations, add or update a regression test.

---

## Scope Discipline

Before adding a feature, ask whether it improves one of these:

1. faster expense entry
2. better spending visibility
3. data reliability
4. privacy
5. portability
6. maintainability

If not, defer it.

Do not build speculative features merely because they are common in finance apps.

---

## Initial Task Guidance

When starting from an empty repository:

1. read the local `PROJECT_SPEC.md` when present
2. inspect the repository and supported bank fixtures
3. establish the smallest clean project structure
4. implement SQLite schema and repository
5. implement encrypted vault migration and on-demand backup
6. build the Add Expense interaction before advanced dashboard work
7. create the Expenses page
8. create the Overview page
9. implement reviewed bank CSV import
10. validate totals
11. refine visual design and keyboard behavior

Favor a functioning polished vertical slice over many incomplete features.

---

## Definition of Done for Core Flow

The following interaction should work cleanly:

```text
N
54.82
Tab
cos
Tab
Enter
```

with behavior equivalent to:

```text
Amount: 54.82
Description: Costco
Category: predicted from history
Date: today
```

The transaction should:

- save to SQLite
- appear immediately in the UI
- update overview totals
- remain encrypted at rest without creating a readable mirror

If this workflow is not smooth, prioritize fixing it before adding additional functionality.
