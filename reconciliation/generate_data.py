"""Synthetic accounts-receivable invoices and bank receipts.

The generator reproduces the problems that make manual cash application slow:
reference formats that vary by payer, missing references, bank fees deducted
from payments, split and combined payments, typos in invoice numbers,
duplicate payments and unrelated bank noise.

Bank lines follow Australian direct-entry conventions: a remitter name of at
most 16 characters and a lodgement reference of at most 18, so truncation
happens naturally.

Every bank line is labelled in ground_truth.csv, so the matching engine's
accuracy is measured rather than asserted. All data is synthetic.
"""
from __future__ import annotations

import itertools
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from reconciliation.policy import short_payment_within_policy

FIRST = ["Coastal", "Southern", "Harbour", "Summit", "Redgum", "Ironbark", "Bluewater",
         "Meridian", "Granite", "Eastgate", "Westfield", "Northline", "Silverleaf",
         "Kestrel", "Wattle", "Banksia", "Riverbend", "Highland", "Pacific", "Outback"]
SECOND = ["Logistics", "Foods", "Engineering", "Traders", "Distribution", "Supplies",
          "Hospitality", "Construction", "Medical", "Retail", "Agriculture", "Freight",
          "Industries", "Packaging", "Electrical", "Plumbing", "Wholesale", "Services"]
SUFFIX = ["Pty Ltd", "Pty Ltd", "Pty Ltd", "Holdings Pty Ltd", "Group Pty Ltd", "Limited"]
NOISE_REMITTERS = ["ATO", "WESTPAC INTEREST", "NAB MERCHANT FEE", "REFUND", "INTERNAL TRANSFER",
                   "STRIPE PAYOUT", "UNKNOWN DEPOSIT", "INSURANCE CLAIM"]

SCENARIO_WEIGHTS = {
    "exact": 0.56, "ref_noise": 0.12, "no_ref": 0.07, "short_paid": 0.06,
    "split": 0.05, "typo": 0.03, "combined": 0.04, "unpaid": 0.07,
}


def _remitter(rng: random.Random, name: str) -> str:
    upper = name.upper()
    if rng.random() < 0.5:
        for suffix in ("HOLDINGS PTY LTD", "GROUP PTY LTD", "PTY LTD", "LIMITED"):
            upper = upper.replace(suffix, "").strip()
    elif rng.random() < 0.3:
        upper = upper.replace("PTY LTD", "P/L")
    return upper


def _noisy_reference(rng: random.Random, num: int) -> str:
    return rng.choice([f"{num}", f"INV {num}", f"INV{num}", f"PAYMENT {num}",
                       f"00{num}", f"inv {num}", f"REF {num} THANKS"])


def _transpose(rng: random.Random, num: int) -> int:
    digits = list(str(num))
    positions = [i for i in range(1, len(digits) - 1) if digits[i] != digits[i + 1]]
    if not positions:
        return num
    i = rng.choice(positions)
    digits[i], digits[i + 1] = digits[i + 1], digits[i]
    return int("".join(digits))


def generate(out_dir: Path, n_invoices: int = 20_000, n_customers: int = 300,
             seed: int = 42) -> dict[str, Path]:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    names = rng.sample([f"{a} {b}" for a in FIRST for b in SECOND], n_customers)
    customers = [(f"C{i:04d}", f"{n} {rng.choice(SUFFIX)}") for i, n in enumerate(names, 1)]

    start = date(2026, 6, 1)
    invoices = []
    for i in range(n_invoices):
        cid, cname = rng.choice(customers)
        inv_date = start + timedelta(days=rng.randint(0, 75))
        invoices.append({
            "invoice_no": f"INV-{100000 + i}", "customer_id": cid, "customer_name": cname,
            "invoice_date": inv_date, "due_date": inv_date + timedelta(days=30),
            "amount": round(max(60.0, rng.lognormvariate(7.8, 0.9)), 2),
        })

    lines: list[dict] = []
    truth: list[dict] = []
    counter = itertools.count(1)

    def add_line(value_date, amount, remitter, reference, labels):
        txn = f"BTX{next(counter):06d}"
        lines.append({"bank_txn_id": txn, "value_date": value_date, "amount": round(amount, 2),
                      "remitter": remitter[:16], "reference": reference[:18]})
        for invoice_no, expected, scenario in labels:
            truth.append({"bank_txn_id": txn, "invoice_no": invoice_no,
                          "expected": expected, "scenario": scenario})
        return txn

    scenarios = list(SCENARIO_WEIGHTS)
    weights = list(SCENARIO_WEIGHTS.values())
    combined_pool: dict[str, list[dict]] = {}
    exact_lines: list[tuple] = []

    for inv in invoices:
        scenario = rng.choices(scenarios, weights)[0]
        num = int(inv["invoice_no"][-6:])
        pay_date = inv["due_date"] + timedelta(days=rng.randint(-10, 25))
        remitter = _remitter(rng, inv["customer_name"])
        amount = inv["amount"]
        label = inv["invoice_no"]

        if scenario == "combined":
            combined_pool.setdefault(inv["customer_id"], []).append(inv)
        elif scenario == "exact":
            args = (pay_date, amount, remitter, f"INV-{num}")
            add_line(*args, [(label, "match", "exact")])
            exact_lines.append(args + (label,))
        elif scenario == "ref_noise":
            add_line(pay_date, amount, remitter, _noisy_reference(rng, num),
                     [(label, "match", "ref_noise")])
        elif scenario == "no_ref":
            ref = rng.choice(["PAYMENT", "ACCOUNT PAYMENT", "SUPPLIER PMT", "", "MONTHLY"])
            add_line(pay_date, amount, remitter, ref, [(label, "match", "no_ref")])
        elif scenario == "short_paid":
            if rng.random() < 0.5:
                paid = round(amount - rng.uniform(5, 30), 2)
            else:
                delta = rng.choice([-1, 1]) * rng.uniform(0.01, 0.99)
                paid = round(amount + delta, 2)
            expected = "match" if short_payment_within_policy(amount, paid) else "exception"
            add_line(pay_date, paid, remitter, f"INV-{num}", [(label, expected, "short_paid")])
        elif scenario == "split":
            parts = rng.choice([2, 3])
            cuts = sorted(rng.uniform(0.2, 0.8) for _ in range(parts - 1))
            shares = [b - a for a, b in zip([0.0] + cuts, cuts + [1.0])]
            amounts = [round(amount * s, 2) for s in shares[:-1]]
            amounts.append(round(amount - sum(amounts), 2))
            for k, part in enumerate(amounts):
                add_line(pay_date + timedelta(days=7 * k), part, remitter, f"INV-{num}",
                         [(label, "match", "split")])
        elif scenario == "typo":
            typo = _transpose(rng, num)
            add_line(pay_date, amount, remitter, f"INV-{typo}", [(label, "match", "typo")])
        # "unpaid": no bank line

    for cid, group in combined_pool.items():
        group.sort(key=lambda c: c["due_date"])   # payers remit invoices falling due together
        i = 0
        while i < len(group):
            size = rng.randint(2, 4)
            chunk = group[i:i + size]
            i += size
            if len(chunk) == 1:
                inv = chunk[0]
                add_line(inv["due_date"], inv["amount"], _remitter(rng, inv["customer_name"]),
                         inv["invoice_no"], [(inv["invoice_no"], "match", "exact")])
                continue
            total = round(sum(c["amount"] for c in chunk), 2)
            nums = [c["invoice_no"][-6:] for c in chunk]
            ref = " ".join(nums) if rng.random() < 0.5 else rng.choice(["REMITTANCE", "", "INVOICES"])
            pay_date = max(c["due_date"] for c in chunk) + timedelta(days=rng.randint(0, 15))
            add_line(pay_date, total, _remitter(rng, chunk[0]["customer_name"]), ref,
                     [(c["invoice_no"], "match", "combined") for c in chunk])

    for args in rng.sample(exact_lines, int(len(exact_lines) * 0.015)):
        pay_date, amount, remitter, ref, label = args
        add_line(pay_date + timedelta(days=rng.randint(1, 5)), amount, remitter, ref,
                 [(label, "duplicate", "duplicate")])

    for _ in range(250):
        add_line(start + timedelta(days=rng.randint(0, 120)),
                 round(rng.lognormvariate(6.5, 1.2), 2), rng.choice(NOISE_REMITTERS),
                 rng.choice(["", f"TRF {rng.randint(20000000, 99999999)}", "INTEREST", "ADJ"]),
                 [("", "noise", "noise")])

    paths = {"invoices": out_dir / "invoices.csv", "bank": out_dir / "bank_lines.csv",
             "truth": out_dir / "ground_truth.csv"}
    pd.DataFrame(invoices).to_csv(paths["invoices"], index=False)
    pd.DataFrame(lines).to_csv(paths["bank"], index=False)
    pd.DataFrame(truth).to_csv(paths["truth"], index=False)
    return paths


if __name__ == "__main__":
    print(generate(Path("data/reconciliation")))
