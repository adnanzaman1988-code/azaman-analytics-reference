"""Synthetic general ledger for an illustrative three-entity group.

AU01 (AUD), NZ01 (NZD) and SG01 (SGD); one month of balanced journals, an
opening trial balance, intercompany trading, FX rates, a budget and the
account-to-reporting-line mapping that finance owns. All data is synthetic
and the FX rates are illustrative.
"""
from __future__ import annotations

import itertools
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PERIOD = "2026-09"
ENTITIES = {"AU01": ("AUD", 0.10), "NZ01": ("NZD", 0.15), "SG01": ("SGD", 0.09)}
RATES = [  # AUD per one unit of local currency (illustrative)
    ("AUD", 1.0, 1.0, 1.0), ("NZD", 0.9115, 0.9062, 0.9400), ("SGD", 1.1742, 1.1805, 1.1300),
]
SALES_COUNT = {"AU01": 24_000, "NZ01": 4_000, "SG01": 2_000}

CHART = [  # account, name, type, reporting line, statement
    ("1000", "Cash at bank", "asset", "Cash", "BS"),
    ("1100", "Trade receivables", "asset", "Receivables", "BS"),
    ("1200", "Inventory", "asset", "Inventory", "BS"),
    ("1300", "Prepayments", "asset", "Other current assets", "BS"),
    ("1500", "Intercompany receivable", "asset", "Intercompany", "BS"),
    ("1600", "Property, plant and equipment", "asset", "Property, plant and equipment", "BS"),
    ("1650", "Accumulated depreciation", "asset", "Property, plant and equipment", "BS"),
    ("2000", "Trade payables", "liability", "Payables", "BS"),
    ("2100", "Accrued expenses", "liability", "Accruals and provisions", "BS"),
    ("2200", "GST payable", "liability", "Tax payable", "BS"),
    ("2300", "Employee provisions", "liability", "Accruals and provisions", "BS"),
    ("2500", "Intercompany payable", "liability", "Intercompany", "BS"),
    ("2600", "Borrowings", "liability", "Borrowings", "BS"),
    ("3000", "Share capital", "equity", "Share capital", "BS"),
    ("3100", "Retained earnings", "equity", "Retained earnings", "BS"),
    ("4000", "Sales revenue", "revenue", "Revenue", "PL"),
    ("4100", "Service revenue", "revenue", "Revenue", "PL"),
    ("4900", "Intercompany revenue", "revenue", "Intercompany revenue", "PL"),
    ("5000", "Cost of goods sold", "expense", "Cost of sales", "PL"),
    ("5100", "Freight inwards", "expense", "Cost of sales", "PL"),
    ("5900", "Intercompany purchases", "expense", "Intercompany purchases", "PL"),
    ("6000", "Salaries and wages", "expense", "Employee costs", "PL"),
    ("6010", "Superannuation", "expense", "Employee costs", "PL"),
    ("6020", "Payroll tax", "expense", "Employee costs", "PL"),
    ("6100", "Rent", "expense", "Occupancy", "PL"),
    ("6110", "Utilities", "expense", "Occupancy", "PL"),
    ("6200", "Software subscriptions", "expense", "Technology", "PL"),
    ("6210", "IT support", "expense", "Technology", "PL"),
    ("6300", "Marketing", "expense", "Marketing", "PL"),
    ("6400", "Travel", "expense", "Other operating expenses", "PL"),
    ("6410", "Professional fees", "expense", "Other operating expenses", "PL"),
    ("6420", "Insurance", "expense", "Other operating expenses", "PL"),
    ("6500", "Depreciation", "expense", "Depreciation", "PL"),
    ("7000", "Interest expense", "expense", "Net finance costs", "PL"),
]

EXPENSES = [("6100", 0.020), ("6110", 0.004), ("6200", 0.012), ("6210", 0.006), ("6300", 0.030),
            ("6400", 0.005), ("6410", 0.008), ("6420", 0.004)]  # share of sales


def generate(out_dir: Path, seed: int = 7) -> dict[str, Path]:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines: list[dict] = []
    line_id = itertools.count(1)
    journal_id = itertools.count(1)
    start = date(2026, 9, 1)

    def post(entity, day, legs, source):
        """legs: list of (account, amount) where debit > 0 and credit < 0."""
        jnl = f"J{next(journal_id):07d}"
        legs = [(a, round(v, 2)) for a, v in legs]
        drift = round(-sum(v for _, v in legs), 2)
        a0, v0 = legs[0]
        legs[0] = (a0, round(v0 + drift, 2))
        for account, amount in legs:
            lines.append({"line_id": f"L{next(line_id):08d}", "journal_id": jnl, "entity": entity,
                          "period": PERIOD, "posting_date": start + timedelta(days=day),
                          "account": account, "amount_local": amount, "source": source})

    for entity, (ccy, gst) in ENTITIES.items():
        n_sales = SALES_COUNT[entity]
        sales_total = 0.0
        for _ in range(n_sales):
            net = round(rng.lognormvariate(5.7, 0.8), 2)
            sales_total += net
            day = rng.randint(0, 29)
            rev = "4000" if rng.random() < 0.85 else "4100"
            post(entity, day, [("1100", net * (1 + gst)), (rev, -net), ("2200", -net * gst)], "AR")
            if rng.random() < 0.9:
                post(entity, min(29, day + rng.randint(0, 20)),
                     [("1000", net * (1 + gst)), ("1100", -net * (1 + gst))], "CASH")
        for _ in range(int(n_sales * 0.3)):
            cost = round(sales_total * 0.55 / (n_sales * 0.3) * rng.uniform(0.6, 1.4), 2)
            day = rng.randint(0, 29)
            post(entity, day, [("1200", cost), ("2200", cost * gst), ("2000", -cost * (1 + gst))], "AP")
            if rng.random() < 0.7:
                post(entity, min(29, day + rng.randint(5, 25)),
                     [("2000", cost * (1 + gst)), ("1000", -cost * (1 + gst))], "CASH")
        for day in range(30):
            cogs = sales_total * 0.52 / 30 * rng.uniform(0.9, 1.1)
            post(entity, day, [("5000", cogs), ("1200", -cogs)], "INV")
            freight = sales_total * 0.02 / 30 * rng.uniform(0.8, 1.2)
            post(entity, day, [("5100", freight), ("2000", -freight)], "AP")
        for account, share in EXPENSES:
            budget_like = sales_total * share
            for _ in range(max(20, int(n_sales * 0.02))):
                amt = budget_like / max(20, int(n_sales * 0.02)) * rng.uniform(0.5, 1.5)
                post(entity, rng.randint(0, 29), [(account, amt), ("2200", amt * gst), ("2000", -amt * (1 + gst))], "AP")
        payroll = sales_total * 0.16
        for day in (13, 27):
            wages = payroll / 2 * rng.uniform(0.97, 1.03)
            post(entity, day, [("6000", wages), ("6010", wages * 0.12), ("6020", wages * 0.05),
                               ("1000", -wages * 1.12), ("2100", -wages * 0.05)], "PAYROLL")
        post(entity, 29, [("6500", sales_total * 0.015), ("1650", -sales_total * 0.015)], "GL")
        post(entity, 29, [("7000", sales_total * 0.006), ("1000", -sales_total * 0.006)], "GL")

    # intercompany: AU01 supplies NZ01 and SG01; both sides booked at the average rate
    avg = {ccy: a for ccy, a, _, _ in RATES}
    for target, n in (("NZ01", 40), ("SG01", 20)):
        ccy = ENTITIES[target][0]
        for _ in range(n):
            aud = round(rng.uniform(15_000, 60_000), 2)
            day = rng.randint(0, 29)
            post("AU01", day, [("1500", aud), ("4900", -aud)], "IC")
            local = round(aud / avg[ccy], 2)
            post(target, day, [("5900", local), ("2500", -local)], "IC")

    # opening balances (local) and historical equity in AUD
    opening, equity_hist = [], []
    for entity, (ccy, _) in ENTITIES.items():
        scale = SALES_COUNT[entity] * 400
        bal = {"1000": 0.20, "1100": 1.10, "1200": 0.90, "1300": 0.05, "1600": 1.80, "1650": -0.60,
               "2000": -0.85, "2100": -0.15, "2200": -0.08, "2300": -0.20, "2600": -1.20, "3000": -0.50}
        rows = {a: round(scale * v, 2) for a, v in bal.items()}
        rows["3100"] = round(-sum(rows.values()), 2)
        opening += [{"entity": entity, "account": a, "amount_local": v} for a, v in rows.items()]
        hist = {c: h for c, _, _, h in RATES}[ccy]
        for acct in ("3000", "3100"):
            equity_hist.append({"entity": entity, "account": acct,
                                "amount_aud": round(rows[acct] * hist, 2)})

    # budget in AUD by entity and P&L reporting line (set before the month)
    budget = []
    shares = {"Revenue": -1.0, "Cost of sales": 0.54, "Employee costs": 0.187, "Occupancy": 0.024,
              "Technology": 0.018, "Marketing": 0.022, "Other operating expenses": 0.017,
              "Depreciation": 0.015, "Net finance costs": 0.006}
    for entity, (ccy, _) in ENTITIES.items():
        expected_sales = SALES_COUNT[entity] * 385 * avg[ccy]
        for line, share in shares.items():
            budget.append({"entity": entity, "period": PERIOD, "reporting_line": line,
                           "budget_aud": round(expected_sales * share * rng.uniform(0.95, 1.05), 2)})

    paths = {k: out_dir / f"{k}.csv" for k in ("gl_lines", "opening_balances", "equity_historical",
                                                 "fx_rates", "budget", "account_mapping")}
    pd.DataFrame(lines).to_csv(paths["gl_lines"], index=False)
    pd.DataFrame(opening).to_csv(paths["opening_balances"], index=False)
    pd.DataFrame(equity_hist).to_csv(paths["equity_historical"], index=False)
    pd.DataFrame(RATES, columns=["currency", "average", "closing", "historical"]).to_csv(paths["fx_rates"], index=False)
    pd.DataFrame(budget).to_csv(paths["budget"], index=False)
    pd.DataFrame(CHART, columns=["account", "account_name", "account_type", "reporting_line", "statement"]) \
        .to_csv(paths["account_mapping"], index=False)
    return paths


if __name__ == "__main__":
    print(generate(Path("data/month_end")))
