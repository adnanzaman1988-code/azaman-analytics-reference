"""Parallel run: prove a rebuilt S/4HANA report matches the legacy ECC report.

The same management P&L (by company code, period, profit centre and report
line) is produced twice:
  legacy  BKPF + BSEG, old 6-digit accounts, old profit centres
  rebuilt ACDOCA-style universal journal, new 8-digit accounts, new profit centres

Legacy profit centres are translated to the new structure so both reports sit
on the same basis. Every cell is compared; differences are traced to root
causes: accounts missing from the new report mapping, and documents whose
line counts differ between the legacy and migrated ledgers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

KEYS = ["company_code", "period", "profit_centre", "report_line"]
TOLERANCE = 0.01


@dataclass
class ParallelRun:
    comparison: pd.DataFrame
    differences: pd.DataFrame
    unmapped_accounts: pd.DataFrame
    document_count_breaks: pd.DataFrame
    summary: dict


def load(data_dir: Path) -> dict[str, pd.DataFrame]:
    s = {"BUKRS": str, "RBUKRS": str, "BELNR": str, "HKONT": str, "RACCT": str, "POPER": str,
         "BUZEI": str, "DOCLN": str}
    return {name: pd.read_csv(data_dir / f"{name}.csv", dtype=s) for name in
            ("bkpf", "bseg", "acdoca", "legacy_report_mapping", "s4_report_mapping", "profit_centre_mapping")}


def legacy_report(d: dict) -> pd.DataFrame:
    lines = d["bseg"].merge(d["bkpf"][["BUKRS", "BELNR", "GJAHR", "MONAT"]], on=["BUKRS", "BELNR", "GJAHR"])
    lines["amount"] = lines["DMBTR"].where(lines["SHKZG"] == "S", -lines["DMBTR"])
    lines = lines.merge(d["legacy_report_mapping"], on="HKONT", how="inner")      # P&L accounts only
    pcs = dict(zip(d["profit_centre_mapping"]["legacy_prctr"], d["profit_centre_mapping"]["new_prctr"]))
    lines["profit_centre"] = lines["PRCTR"].map(pcs)
    out = lines.rename(columns={"BUKRS": "company_code", "MONAT": "period"})
    return out.groupby(KEYS, as_index=False)["amount"].sum().rename(columns={"amount": "legacy"})


def s4_report(d: dict) -> pd.DataFrame:
    j = d["acdoca"][d["acdoca"]["RLDNR"] == "0L"].copy()
    pl_accounts = set(d["s4_report_mapping"]["RACCT"]) | {a for a in j["RACCT"] if a[0] in "4567"}
    j = j[j["RACCT"].isin(pl_accounts)]
    j = j.merge(d["s4_report_mapping"], on="RACCT", how="left")
    j["report_line"] = j["report_line"].fillna("UNMAPPED")
    j["period"] = j["POPER"].astype(int)
    out = j.rename(columns={"RBUKRS": "company_code", "PRCTR": "profit_centre"})
    return out.groupby(KEYS, as_index=False)["HSL"].sum().rename(columns={"HSL": "rebuilt"})


def reconcile(d: dict) -> ParallelRun:
    t0 = time.perf_counter()
    cmp = legacy_report(d).merge(s4_report(d), on=KEYS, how="outer").fillna({"legacy": 0.0, "rebuilt": 0.0})
    cmp["difference"] = (cmp["rebuilt"] - cmp["legacy"]).round(2)
    cmp["status"] = cmp["difference"].abs().le(TOLERANCE).map({True: "MATCH", False: "DIFFERENCE"})
    diffs = cmp[cmp["status"] == "DIFFERENCE"].sort_values("difference", key=abs, ascending=False)

    j = d["acdoca"]
    used_pl = j[j["RACCT"].str[0].isin(list("4567"))]
    unmapped = (used_pl[~used_pl["RACCT"].isin(d["s4_report_mapping"]["RACCT"])]
                .groupby("RACCT", as_index=False)["HSL"].agg(["sum", "count"])
                .rename(columns={"sum": "amount", "count": "lines"}))

    legacy_counts = d["bseg"].groupby(["BUKRS", "BELNR"]).size().rename("legacy_lines")
    new_counts = j.groupby(["RBUKRS", "BELNR"]).size().rename("migrated_lines")
    new_counts.index.names = ["BUKRS", "BELNR"]
    counts = pd.concat([legacy_counts, new_counts], axis=1).fillna(0).astype(int)
    breaks = counts[counts["legacy_lines"] != counts["migrated_lines"]].reset_index()
    breaks["issue"] = breaks.apply(lambda r: "duplicated in migration" if r["migrated_lines"] > r["legacy_lines"]
                                   else "missing from migration", axis=1)

    elapsed = time.perf_counter() - t0
    summary = {
        "legacy_line_items": len(d["bseg"]), "migrated_line_items": len(j),
        "cells_compared": len(cmp), "cells_matched": int((cmp["status"] == "MATCH").sum()),
        "cells_different": len(diffs), "net_difference": round(float(cmp["difference"].sum()), 2),
        "unmapped_accounts": unmapped["RACCT"].tolist(),
        "documents_with_line_count_breaks": breaks["BELNR"].tolist(),
        "runtime_seconds": round(elapsed, 2),
    }
    return ParallelRun(cmp, diffs, unmapped, breaks, summary)


def remediate(d: dict, run: ParallelRun, mapping_fixes: dict[str, str]) -> dict:
    """Apply the fixes a migration team would make, then re-run."""
    fixed = dict(d)
    fixed["s4_report_mapping"] = pd.concat([d["s4_report_mapping"], pd.DataFrame(
        [{"RACCT": k, "report_line": v} for k, v in mapping_fixes.items()])], ignore_index=True)
    fixed["acdoca"] = d["acdoca"].drop_duplicates(["RLDNR", "RBUKRS", "GJAHR", "BELNR", "DOCLN"])
    return fixed


def write_outputs(first: ParallelRun, second: ParallelRun, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "parallel_run_report.xlsx"
    rows = []
    for label, run in (("First parallel run", first), ("After remediation", second)):
        for k, v in run.summary.items():
            rows.append({"run": label, "metric": k, "value": ", ".join(map(str, v)) if isinstance(v, list) else v})
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="Summary", index=False)
        first.differences.to_excel(xw, sheet_name="Differences (run 1)", index=False)
        first.unmapped_accounts.to_excel(xw, sheet_name="Root cause - mapping", index=False)
        first.document_count_breaks.to_excel(xw, sheet_name="Root cause - documents", index=False)
        second.comparison.to_excel(xw, sheet_name="Signed-off comparison", index=False)
        for ws in xw.book.worksheets:
            for col in "ABCDEFGH":
                ws.column_dimensions[col].width = 22
    return path


if __name__ == "__main__":
    data = load(Path("data/s4_rebuild"))
    run1 = reconcile(data)
    run2 = reconcile(remediate(data, run1, {"66000000": "Depreciation"}))
    print(run1.summary, run2.summary, sep="\n")
    print(write_outputs(run1, run2, Path("output/s4_rebuild")))
