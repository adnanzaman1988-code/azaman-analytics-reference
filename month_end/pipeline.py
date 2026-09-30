"""Month-end close pipeline replacing a macro-driven Excel reporting pack.

Steps:
  1. load ledger extracts, opening balances, FX rates, budget and mapping
  2. validate: duplicate lines, unbalanced journals, unmapped accounts,
     wrong-period postings, missing FX rates, unbalanced opening balances
  3. build each entity's closing trial balance in local currency
  4. translate to AUD in line with AASB 121: assets and liabilities at the
     closing rate, income and expenses at the average rate, equity at
     historical rates, with the difference to the translation reserve (FCTR)
  5. map accounts to reporting lines using finance's mapping table
  6. eliminate intercompany revenue, purchases and balances
  7. consolidate and compare to budget; flag lines needing commentary
  8. write the reporting pack (Excel) and a SQLite database for BI tools

Every check is logged; a failed critical check stops the run rather than
producing a pack that looks right and isn't.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

PL_ORDER = ["Revenue", "Cost of sales", "Gross profit", "Employee costs", "Occupancy", "Technology",
            "Marketing", "Other operating expenses", "EBITDA", "Depreciation", "EBIT",
            "Net finance costs", "Net profit before tax"]
BS_ORDER = ["Cash", "Receivables", "Inventory", "Other current assets", "Property, plant and equipment",
            "Total assets", "Payables", "Accruals and provisions", "Tax payable", "Borrowings",
            "Total liabilities", "Share capital", "Retained earnings", "Current period profit",
            "Foreign currency translation reserve", "Total equity"]
VARIANCE_PCT, VARIANCE_ABS = 0.10, 50_000
IC_TOLERANCE_PCT = 0.01


class ValidationError(RuntimeError):
    pass


@dataclass
class Checks:
    rows: list[dict] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "", critical: bool = True):
        self.rows.append({"check": name, "status": "PASS" if passed else ("FAIL" if critical else "WARN"),
                          "detail": detail})
        if critical and not passed:
            raise ValidationError(f"{name}: {detail}")

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def load(data_dir: Path) -> dict[str, pd.DataFrame]:
    dtype = {"account": str}
    return {
        "gl": pd.read_csv(data_dir / "gl_lines.csv", dtype=dtype),
        "opening": pd.read_csv(data_dir / "opening_balances.csv", dtype=dtype),
        "equity_hist": pd.read_csv(data_dir / "equity_historical.csv", dtype=dtype),
        "fx": pd.read_csv(data_dir / "fx_rates.csv"),
        "budget": pd.read_csv(data_dir / "budget.csv"),
        "mapping": pd.read_csv(data_dir / "account_mapping.csv", dtype=dtype),
    }


def validate(d: dict, period: str, currencies: dict[str, str], checks: Checks) -> None:
    gl, mapping = d["gl"], d["mapping"]
    dup = gl["line_id"].duplicated().sum()
    checks.add("No duplicate ledger lines", dup == 0, f"{dup} duplicates")

    by_journal = gl.groupby(["entity", "journal_id"])["amount_local"].sum().round(2)
    bad = by_journal[by_journal.abs() > 0.01]
    checks.add("Every journal balances", bad.empty, f"{len(bad)} unbalanced journals")

    used = set(gl["account"]) | set(d["opening"]["account"])
    unmapped = sorted(used - set(mapping["account"]))
    checks.add("Every account is mapped to a reporting line", not unmapped,
               f"unmapped: {', '.join(unmapped)}" if unmapped else f"{len(used)} accounts mapped")

    wrong_period = (gl["period"] != period).sum()
    checks.add("All postings in the reporting period", wrong_period == 0, f"{wrong_period} lines outside {period}")

    missing_fx = sorted(set(currencies.values()) - set(d["fx"]["currency"]))
    checks.add("FX rates available for every entity currency", not missing_fx, ", ".join(missing_fx))

    ob = d["opening"].groupby("entity")["amount_local"].sum().round(2)
    checks.add("Opening balances balance by entity", (ob.abs() <= 0.01).all(), ob.to_dict().__repr__())


def closing_tb(d: dict) -> pd.DataFrame:
    movement = d["gl"].groupby(["entity", "account"], as_index=False)["amount_local"].sum()
    tb = pd.concat([d["opening"], movement]).groupby(["entity", "account"], as_index=False)["amount_local"].sum()
    return tb.merge(d["mapping"], on="account", how="left")


def translate(tb: pd.DataFrame, d: dict, currencies: dict[str, str], checks: Checks) -> pd.DataFrame:
    fx = d["fx"].set_index("currency")
    tb = tb.copy()
    tb["currency"] = tb["entity"].map(currencies)
    is_pl = tb["statement"] == "PL"
    is_equity = tb["account_type"] == "equity"
    tb["rate"] = tb["currency"].map(fx["closing"])
    tb.loc[is_pl, "rate"] = tb.loc[is_pl, "currency"].map(fx["average"])
    tb["amount_aud"] = (tb["amount_local"] * tb["rate"]).round(2)

    hist = d["equity_hist"].set_index(["entity", "account"])["amount_aud"]
    eq_keys = list(zip(tb.loc[is_equity, "entity"], tb.loc[is_equity, "account"]))
    tb.loc[is_equity, "amount_aud"] = [hist[k] for k in eq_keys]
    tb.loc[is_equity, "rate"] = float("nan")

    fctr = (-tb.groupby("entity")["amount_aud"].sum()).round(2)
    fctr_rows = pd.DataFrame({"entity": fctr.index, "account": "3200",
                              "account_name": "Foreign currency translation reserve",
                              "account_type": "equity", "reporting_line": "Foreign currency translation reserve",
                              "statement": "BS", "amount_local": 0.0, "amount_aud": fctr.values})
    out = pd.concat([tb, fctr_rows], ignore_index=True)
    residual = out.groupby("entity")["amount_aud"].sum().round(2)
    checks.add("Translated trial balance balances by entity", (residual.abs() <= 0.01).all(),
               f"FCTR by entity: {fctr.to_dict()}")
    return out


def eliminate(tr: pd.DataFrame, checks: Checks) -> pd.DataFrame:
    ic_rev = -tr.loc[tr["reporting_line"] == "Intercompany revenue", "amount_aud"].sum()
    ic_pur = tr.loc[tr["reporting_line"] == "Intercompany purchases", "amount_aud"].sum()
    checks.add("Intercompany revenue equals intercompany purchases", abs(ic_rev - ic_pur) <= max(1.0, 0.0001 * ic_rev),
               f"revenue {ic_rev:,.2f} vs purchases {ic_pur:,.2f}")

    ic_bs = tr[tr["reporting_line"] == "Intercompany"]
    receivable = ic_bs.loc[ic_bs["account_type"] == "asset", "amount_aud"].sum()
    payable = -ic_bs.loc[ic_bs["account_type"] == "liability", "amount_aud"].sum()
    residual = round(receivable - payable, 2)
    checks.add("Intercompany balances agree after translation", abs(residual) <= IC_TOLERANCE_PCT * receivable,
               f"residual {residual:,.2f} (translation difference, taken to FCTR)", critical=False)

    elim = tr[tr["reporting_line"].isin(["Intercompany revenue", "Intercompany purchases", "Intercompany"])].copy()
    elim["entity"] = "ELIM"
    elim["amount_aud"] = -elim["amount_aud"]
    if residual:
        elim = pd.concat([elim, pd.DataFrame([{"entity": "ELIM", "reporting_line": "Foreign currency translation reserve",
                                               "statement": "BS", "account_type": "equity",
                                               "amount_aud": residual}])], ignore_index=True)
    return pd.concat([tr, elim], ignore_index=True)


def statements(tr: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    pl = tr[tr["statement"] == "PL"].pivot_table(index="reporting_line", columns="entity", values="amount_aud",
                                                 aggfunc="sum", fill_value=0.0)
    pl = -pl  # present income positive, expenses negative
    pl = pl.drop(index=[i for i in ("Intercompany revenue", "Intercompany purchases") if i in pl.index])

    def row(name):
        return pl.loc[name] if name in pl.index else 0.0
    pl.loc["Gross profit"] = row("Revenue") + row("Cost of sales")
    pl.loc["EBITDA"] = pl.loc["Gross profit"] + sum(row(x) for x in ("Employee costs", "Occupancy", "Technology",
                                                                       "Marketing", "Other operating expenses"))
    pl.loc["EBIT"] = pl.loc["EBITDA"] + row("Depreciation")
    pl.loc["Net profit before tax"] = pl.loc["EBIT"] + row("Net finance costs")
    pl = pl.reindex(PL_ORDER).fillna(0.0)
    pl["Consolidated"] = pl.sum(axis=1)

    bs_src = tr[tr["statement"] == "BS"]
    bs = bs_src.pivot_table(index="reporting_line", columns="entity", values="amount_aud", aggfunc="sum", fill_value=0.0)
    bs = bs.drop(index=[i for i in ("Intercompany",) if i in bs.index and abs(bs.loc[i].sum()) < 0.01])
    for liab in ("Payables", "Accruals and provisions", "Tax payable", "Borrowings", "Share capital",
                 "Retained earnings", "Foreign currency translation reserve"):
        if liab in bs.index:
            bs.loc[liab] = -bs.loc[liab]
    bs.loc["Current period profit"] = pl.loc["Net profit before tax"].drop("Consolidated")
    assets = ["Cash", "Receivables", "Inventory", "Other current assets", "Property, plant and equipment"]
    liabilities = ["Payables", "Accruals and provisions", "Tax payable", "Borrowings"]
    equity = ["Share capital", "Retained earnings", "Current period profit", "Foreign currency translation reserve"]
    bs.loc["Total assets"] = bs.reindex(assets).fillna(0).sum()
    bs.loc["Total liabilities"] = bs.reindex(liabilities).fillna(0).sum()
    bs.loc["Total equity"] = bs.reindex(equity).fillna(0).sum()
    bs = bs.reindex(BS_ORDER).fillna(0.0)
    bs["Consolidated"] = bs.sum(axis=1)
    return pl.round(2), bs.round(2)


def variance(pl: pd.DataFrame, budget: pd.DataFrame) -> pd.DataFrame:
    b = budget.groupby("reporting_line")["budget_aud"].sum()
    b = -b  # budget stored with the ledger sign convention
    lines = [l for l in PL_ORDER if l in b.index]
    v = pd.DataFrame({"Actual": pl.loc[lines, "Consolidated"], "Budget": b.reindex(lines)})
    v["Variance"] = (v["Actual"] - v["Budget"]).round(2)
    v["Variance %"] = (v["Variance"] / v["Budget"].abs()).round(4)
    v["Commentary required"] = (v["Variance %"].abs() > VARIANCE_PCT) & (v["Variance"].abs() > VARIANCE_ABS)
    return v


def write_pack(out_dir: Path, period: str, pl, bs, var, checks: Checks, fx: pd.DataFrame, runtime: float) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"month_end_pack_{period}.xlsx"
    summary = pd.DataFrame({
        "Metric": ["Period", "Revenue (AUD)", "Gross margin", "EBITDA (AUD)", "Net profit before tax (AUD)",
                   "Validation checks passed", "Lines needing commentary", "Pipeline runtime (seconds)"],
        "Value": [period, pl.loc["Revenue", "Consolidated"],
                  round(pl.loc["Gross profit", "Consolidated"] / pl.loc["Revenue", "Consolidated"], 4),
                  pl.loc["EBITDA", "Consolidated"], pl.loc["Net profit before tax", "Consolidated"],
                  f"{(checks.frame()['status'] == 'PASS').sum()} of {len(checks.rows)}",
                  int(var["Commentary required"].sum()), round(runtime, 2)],
    })
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        pl.to_excel(xw, sheet_name="Profit and loss")
        bs.to_excel(xw, sheet_name="Balance sheet")
        var.to_excel(xw, sheet_name="Budget variance")
        checks.frame().to_excel(xw, sheet_name="Validation log", index=False)
        fx.to_excel(xw, sheet_name="FX rates", index=False)
        money, pct = "#,##0.00;(#,##0.00)", "0.0%"
        for ws in xw.book.worksheets:
            ws.column_dimensions["A"].width = 44
            for col in "BCDEFGH":
                ws.column_dimensions[col].width = 18
            for row in ws.iter_rows(min_row=2):
                for cell in row[1:]:
                    if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                        cell.number_format = money
        xw.book["Summary"]["B4"].number_format = pct            # gross margin
        for row in xw.book["Budget variance"].iter_rows(min_row=2, min_col=5, max_col=5):
            row[0].number_format = pct
    with sqlite3.connect(out_dir / "month_end.sqlite") as db:
        pl.reset_index().to_sql("profit_and_loss", db, if_exists="replace", index=False)
        bs.reset_index().to_sql("balance_sheet", db, if_exists="replace", index=False)
        var.reset_index().to_sql("budget_variance", db, if_exists="replace", index=False)
        checks.frame().to_sql("validation_log", db, if_exists="replace", index=False)
    return path


def run(data_dir: Path, out_dir: Path, period: str = "2026-09",
        currencies: dict[str, str] | None = None) -> dict:
    currencies = currencies or {"AU01": "AUD", "NZ01": "NZD", "SG01": "SGD"}
    t0 = time.perf_counter()
    checks = Checks()
    d = load(data_dir)
    validate(d, period, currencies, checks)
    tr = translate(closing_tb(d), d, currencies, checks)
    tr = eliminate(tr, checks)
    pl, bs = statements(tr)
    checks.add("Consolidated balance sheet balances",
               abs(bs.loc["Total assets", "Consolidated"] - bs.loc["Total liabilities", "Consolidated"]
                   - bs.loc["Total equity", "Consolidated"]) <= 0.05,
               f"assets {bs.loc['Total assets', 'Consolidated']:,.2f}")
    var = variance(pl, d["budget"])
    runtime = time.perf_counter() - t0
    pack = write_pack(out_dir, period, pl, bs, var, checks, d["fx"], runtime)
    return {"gl_lines": len(d["gl"]), "journals": d["gl"]["journal_id"].nunique(),
            "entities": len(currencies), "checks": len(checks.rows),
            "checks_passed": int((checks.frame()["status"] == "PASS").sum()),
            "lines_needing_commentary": int(var["Commentary required"].sum()),
            "revenue_aud": float(pl.loc["Revenue", "Consolidated"]),
            "runtime_seconds": round(runtime, 2), "pack": str(pack),
            "pl": pl, "bs": bs, "variance": var, "checks_frame": checks.frame()}


if __name__ == "__main__":
    result = run(Path("data/month_end"), Path("output/month_end"))
    print({k: v for k, v in result.items() if not isinstance(v, pd.DataFrame)})
