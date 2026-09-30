# Azaman Analytics — Reference Implementations

Working Python/SQL reference implementations of three finance-automation problems
that mid-market finance teams usually solve by hand in Excel:

| Module | Problem it replaces | What it demonstrates |
|---|---|---|
| `reconciliation/` | Days of manual cash application and bank-to-invoice matching | A multi-pass matching engine with policy-driven tolerances, an exception queue with reason codes, duplicate-payment detection, and accuracy scored against labelled data |
| `month_end/` | A macro-heavy Excel month-end reporting pack | A validated close pipeline: ledger checks, AASB 121 translation, intercompany elimination, consolidation, budget variance, and an Excel pack plus SQLite tables for BI |
| `s4_rebuild/` | Manually re-checking finance reports after an SAP S/4HANA cutover | A parallel-run reconciliation of the legacy ECC report against the rebuilt S/4HANA report, with root-cause drill-down |

**All data is synthetic.** Nothing in this repository comes from a client or employer.
SAP table and field names follow ECC and S/4HANA conventions but are heavily simplified.

## Results on the synthetic datasets

Measured with `python run_all.py` on a standard cloud container; timings will vary by machine.

**Reconciliation** — 20,000 open invoices, 20,139 bank receipts
- 97.4% of receipts (98.0% by value) matched automatically in 0.35 seconds
- Precision 100%: no receipt matched to the wrong invoice
- Recall 99.65% of genuinely matchable receipt–invoice pairs
- All 168 planted duplicate payments flagged; none of 250 unrelated bank lines matched
- Short payments outside policy routed to review, not written off

**Month-end close** — 198,660 ledger lines, 77,421 journals, 3 entities, 3 currencies
- 10 of 10 validation checks passed; consolidated balance sheet balances
- Full pack (P&L, balance sheet, budget variance, validation log) produced in under 0.5 seconds
- An unmapped account or an unbalanced journal stops the run with a clear error

**S/4HANA parallel run** — 166,919 legacy line items, 50,000 documents
- First run: 25 report cells differed; both planted defects traced to root cause
  (an account missing from the new report mapping, and a document loaded twice)
- After remediation: 108 of 108 cells match, net difference nil

## Quick start

```bash
pip install -r requirements.txt
python run_all.py          # generates data, runs all three pipelines, writes ./output
python -m pytest -q        # 12 tests
```

Or with Docker:

```bash
docker build -t azaman-reference .
docker run --rm -v "$PWD/output:/app/output" azaman-reference
```

Outputs land in `./output`: an Excel results workbook per module, a SQLite database
for the month-end pack, and `metrics.json`. A copy of each workbook from a reference run
is in `sample_output/` for anyone who wants to see the results without running the code.

## Repository structure

```
reconciliation/   generate_data.py · engine.py · policy.py
month_end/        generate_data.py · pipeline.py
s4_rebuild/       generate_data.py · parallel_run.py
tests/            one test file per module
run_all.py        end-to-end run and metrics
```

## Design principles

- **Validation stops the run.** A pack that looks right and isn't is worse than no pack.
- **Finance owns the rules.** Tolerances, account mappings and budgets live in tables finance maintains, not in code.
- **Exceptions, not guesses.** Anything the engine cannot settle with confidence goes to a queue with a reason.
- **Prove it before sign-off.** Rebuilt reports are reconciled cell by cell against the system they replace.
- **Measured, not asserted.** Synthetic data is labelled so accuracy can be scored.

## About

Built by Adnan Zaman, Azaman Analytics — finance systems and data automation for
mid-market finance teams. Australian ABN 43 651 839 030.
adnanzaman1988@gmail.com · linkedin.com/in/adnanzaman1988
