"""Synthetic ledger extracts shaped like SAP ECC and S/4HANA tables.

Legacy (ECC): BKPF document headers and BSEG line items, on the old 6-digit
chart of accounts and the old profit-centre structure.

Target (S/4HANA): a universal-journal extract shaped like ACDOCA, with the
same postings on the harmonised 8-digit chart and the new profit centres.

Field names follow SAP conventions but the tables are heavily simplified;
real BSEG and ACDOCA carry hundreds of fields. All data is synthetic.

Two defects are planted in the migrated data so the parallel run has
something real to find:
  1. the new report mapping omits one account (depreciation)
  2. one migrated document is loaded twice
"""
from __future__ import annotations

import itertools
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

GJAHR = 2027                   # fiscal year July 2026 to June 2027
PERIODS = (1, 2, 3)            # July, August, September
COMPANY_PCS = {"1000": ["PC_VIC", "PC_NSW", "PC_QLD", "PC_WA"], "2000": ["PC_NZN", "PC_NZS"]}
PC_MAP = {"PC_VIC": "PC_VIC", "PC_NSW": "PC_NSW", "PC_QLD": "PC_QLDWA", "PC_WA": "PC_QLDWA",
          "PC_NZN": "PC_NZ", "PC_NZS": "PC_NZ"}

GL = [  # old account, new account, description, report line (None = balance sheet)
    ("400000", "41000000", "Sales revenue", "Revenue"),
    ("401000", "41100000", "Service revenue", "Revenue"),
    ("500000", "51000000", "Cost of sales", "Cost of sales"),
    ("510000", "51100000", "Freight", "Cost of sales"),
    ("600000", "61000000", "Salaries", "Employee costs"),
    ("601000", "61100000", "Superannuation", "Employee costs"),
    ("610000", "62000000", "Rent", "Occupancy"),
    ("620000", "63000000", "IT services", "Technology"),
    ("621000", "63000000", "Software licences", "Technology"),   # merged in the new chart
    ("630000", "64000000", "Marketing", "Marketing"),
    ("640000", "65000000", "Travel", "Travel"),
    ("650000", "66000000", "Depreciation", "Depreciation"),
    ("700000", "71000000", "Interest expense", "Finance costs"),
    ("100000", "10000000", "Bank", None),
    ("110000", "11000000", "Receivables", None),
    ("160000", "16000000", "Accumulated depreciation", None),
    ("200000", "20000000", "Payables", None),
    ("210000", "21000000", "Accruals", None),
]
OLD_TO_NEW = {o: n for o, n, _, _ in GL}

DOC_TEMPLATES = [  # (weight, document type, legs as (account, share, P&L?))
    (0.45, "DR", [("110000", 1.0, False), ("400000", -0.8, True), ("401000", -0.2, True)]),
    (0.25, "KR", [("500000", 0.8, True), ("510000", 0.2, True), ("200000", -1.0, False)]),
    (0.10, "KR", [("610000", 0.3, True), ("620000", 0.25, True), ("621000", 0.15, True),
                  ("630000", 0.2, True), ("640000", 0.1, True), ("200000", -1.0, False)]),
    (0.12, "SA", [("600000", 0.89, True), ("601000", 0.11, True), ("100000", -0.95, False),
                  ("210000", -0.05, False)]),
    (0.05, "AF", [("650000", 1.0, True), ("160000", -1.0, False)]),
    (0.03, "SA", [("700000", 1.0, True), ("100000", -1.0, False)]),
]


def generate(out_dir: Path, n_docs: int = 50_000, seed: int = 11) -> dict[str, Path]:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    bkpf, bseg, acdoca = [], [], []
    doc_no = itertools.count(100000001)
    weights = [t[0] for t in DOC_TEMPLATES]

    for _ in range(n_docs):
        bukrs = rng.choice(["1000", "1000", "1000", "2000"])
        monat = rng.choice(PERIODS)
        belnr = f"{next(doc_no):010d}"
        budat = date(2026, 6 + monat, 1) + timedelta(days=rng.randint(0, 27))
        _, blart, legs = rng.choices(DOC_TEMPLATES, weights)[0]
        pc = rng.choice(COMPANY_PCS[bukrs])
        total = round(rng.lognormvariate(8.0, 1.0), 2)
        amounts = [round(total * share, 2) for _, share, _ in legs]
        amounts[0] = round(amounts[0] - sum(amounts), 2)   # force the document to balance
        bkpf.append({"BUKRS": bukrs, "BELNR": belnr, "GJAHR": GJAHR, "MONAT": monat,
                     "BLART": blart, "BUDAT": budat})
        for i, ((hkont, _, _), amt) in enumerate(zip(legs, amounts), start=1):
            bseg.append({"BUKRS": bukrs, "BELNR": belnr, "GJAHR": GJAHR, "BUZEI": f"{i:03d}",
                         "HKONT": hkont, "SHKZG": "S" if amt >= 0 else "H", "DMBTR": abs(amt),
                         "PRCTR": pc})
            acdoca.append({"RLDNR": "0L", "RBUKRS": bukrs, "GJAHR": GJAHR, "BELNR": belnr,
                           "DOCLN": f"{i:06d}", "RACCT": OLD_TO_NEW[hkont], "HSL": amt,
                           "PRCTR": PC_MAP[pc], "POPER": f"{monat:03d}", "BUDAT": budat})

    # defect 2: one migrated document loaded twice (a P&L document in company 1000, period 2)
    victim = next(d["BELNR"] for d in bkpf if d["BUKRS"] == "1000" and d["MONAT"] == 2 and d["BLART"] == "KR")
    acdoca += [dict(r) for r in acdoca if r["BELNR"] == victim and r["RBUKRS"] == "1000"]

    legacy_map = [{"HKONT": o, "report_line": line} for o, _, _, line in GL if line]
    new_map = {}
    for _, n, _, line in GL:
        if line:
            new_map[n] = line
    new_map.pop("66000000")   # defect 1: depreciation missing from the rebuilt report's mapping

    paths = {k: out_dir / f"{k}.csv" for k in ("bkpf", "bseg", "acdoca", "legacy_report_mapping",
                                                 "s4_report_mapping", "profit_centre_mapping")}
    pd.DataFrame(bkpf).to_csv(paths["bkpf"], index=False)
    pd.DataFrame(bseg).to_csv(paths["bseg"], index=False)
    pd.DataFrame(acdoca).to_csv(paths["acdoca"], index=False)
    pd.DataFrame(legacy_map).to_csv(paths["legacy_report_mapping"], index=False)
    pd.DataFrame([{"RACCT": k, "report_line": v} for k, v in new_map.items()]).to_csv(paths["s4_report_mapping"], index=False)
    pd.DataFrame([{"legacy_prctr": k, "new_prctr": v} for k, v in PC_MAP.items()]).to_csv(paths["profit_centre_mapping"], index=False)
    return paths


if __name__ == "__main__":
    print(generate(Path("data/s4_rebuild")))
