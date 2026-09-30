"""Multi-pass cash application: bank receipts matched to open AR invoices.

Each pass handles one real-world problem, from most certain to least:

  P1  exact reference (any format) and exact amount; multi-reference remittances
  P2  reference typo corrected: payer identified, exact amount, reference one edit away
  P3  single reference, short-paid within policy (bank fees, rounding)
  P4  split payments: several receipts that together settle one invoice
  P5  combined payment: one receipt settling several invoices of the same payer
  P6  no reference: payer identified and a single open invoice for the exact amount

Anything the engine cannot settle with confidence goes to an exception queue
with a reason code, never to a guess. A second payment against an invoice
already settled is flagged as a possible duplicate.
"""
from __future__ import annotations

import itertools
import re
import time
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz.distance import DamerauLevenshtein

from reconciliation import policy

REF_PATTERN = re.compile(r"(?<!\d)0*(1\d{5})(?!\d)")
NAME_NOISE = re.compile(r"\b(PTY|LTD|P/L|LIMITED|HOLDINGS|GROUP)\b|[^A-Z ]")


def normalise_name(text: str) -> str:
    return " ".join(NAME_NOISE.sub(" ", str(text).upper()).split())


def extract_refs(text) -> list[int]:
    if not isinstance(text, str):
        return []
    return [int(m) for m in REF_PATTERN.findall(text)]


@dataclass
class Result:
    matches: pd.DataFrame
    exceptions: pd.DataFrame
    open_invoices: pd.DataFrame
    summary: dict


class Reconciler:
    def __init__(self, invoices: pd.DataFrame, bank: pd.DataFrame):
        inv = invoices.copy()
        inv["key"] = inv["invoice_no"].str[-6:].astype(int)
        self.invoices = inv
        self.amount = dict(zip(inv["key"], inv["amount"].round(2)))
        self.customer_of = dict(zip(inv["key"], inv["customer_id"]))
        self.invoice_date = dict(zip(inv["key"], pd.to_datetime(inv["invoice_date"])))
        self.due_date = dict(zip(inv["key"], pd.to_datetime(inv["due_date"])))
        self.invoice_no = dict(zip(inv["key"], inv["invoice_no"]))
        self.by_customer = inv.groupby("customer_id")["key"].apply(list).to_dict()

        names = inv.drop_duplicates("customer_id")
        self._choice_names = [normalise_name(n) for n in names["customer_name"]]
        self._choice_ids = list(names["customer_id"])
        self._customer_cache: dict[str, str | None] = {}

        bank = bank.copy()
        bank["value_date"] = pd.to_datetime(bank["value_date"])
        bank = bank.sort_values(["value_date", "bank_txn_id"]).reset_index(drop=True)
        self.lines = bank.to_dict("records")
        for ln in self.lines:
            ln["refs"] = [r for r in extract_refs(ln["reference"]) if r in self.amount]
            ln["raw_refs"] = extract_refs(ln["reference"])

        self.done: set[str] = set()
        self.settled: set[int] = set()
        self.match_rows: list[dict] = []
        self.exception_rows: list[dict] = []
        self._group = itertools.count(1)

    # ------------------------------------------------------------------ helpers
    def _remaining(self):
        return [ln for ln in self.lines if ln["bank_txn_id"] not in self.done]

    def _customer(self, ln) -> str | None:
        """Identify the payer. Remitter names are truncated by the bank, so a
        unique prefix match is tried before fuzzy matching."""
        key = normalise_name(ln["remitter"])
        if key not in self._customer_cache:
            prefix_hits = {cid for name, cid in zip(self._choice_names, self._choice_ids)
                           if len(key) >= 10 and name.startswith(key)}
            if len(prefix_hits) == 1:
                self._customer_cache[key] = prefix_hits.pop()
            else:
                hit = process.extractOne(key, self._choice_names, scorer=fuzz.WRatio,
                                         score_cutoff=policy.NAME_MATCH_CUTOFF)
                self._customer_cache[key] = self._choice_ids[hit[2]] if hit else None
        return self._customer_cache[key]

    def _open_for(self, customer_id: str) -> list[int]:
        return [k for k in self.by_customer.get(customer_id, []) if k not in self.settled]

    def _record(self, lines: list[dict], keys: list[int], pass_name: str, confidence: float):
        group = next(self._group)
        paid = round(sum(ln["amount"] for ln in lines), 2)
        billed = round(sum(self.amount[k] for k in keys), 2)
        for ln in lines:
            for k in keys:
                self.match_rows.append({
                    "group_id": group, "bank_txn_id": ln["bank_txn_id"],
                    "invoice_no": self.invoice_no[k], "match_pass": pass_name,
                    "confidence": confidence, "group_paid": paid, "group_invoiced": billed,
                    "group_difference": round(paid - billed, 2),
                })
            self.done.add(ln["bank_txn_id"])
        self.settled.update(keys)

    def _exception(self, ln, reason: str, invoice_key: int | None = None):
        self.exception_rows.append({
            "bank_txn_id": ln["bank_txn_id"], "value_date": ln["value_date"].date(),
            "amount": ln["amount"], "remitter": ln["remitter"], "reference": ln["reference"],
            "reason": reason,
            "candidate_invoice": self.invoice_no.get(invoice_key, "") if invoice_key else "",
        })
        self.done.add(ln["bank_txn_id"])

    def _plausible_date(self, ln, key: int) -> bool:
        return (self.invoice_date[key] <= ln["value_date"]
                <= self.due_date[key] + timedelta(days=policy.PAYMENT_WINDOW_DAYS))

    # ------------------------------------------------------------------ passes
    def p1_exact(self):
        for ln in self._remaining():
            refs = ln["refs"]
            if len(refs) == 1:
                k = refs[0]
                if abs(ln["amount"] - self.amount[k]) <= policy.CENT:
                    if k in self.settled:
                        self._exception(ln, "possible duplicate payment", k)
                    else:
                        self._record([ln], [k], "P1 exact reference", 1.00)
            elif len(refs) > 1:
                open_refs = [k for k in dict.fromkeys(refs) if k not in self.settled]
                total = sum(self.amount[k] for k in open_refs)
                if open_refs and abs(ln["amount"] - total) <= policy.CENT:
                    self._record([ln], open_refs, "P1 multi-reference remittance", 0.98)

    def p3_short_paid(self):
        for ln in self._remaining():
            if len(ln["refs"]) != 1:
                continue
            k = ln["refs"][0]
            if k not in self.settled and policy.short_payment_within_policy(self.amount[k], ln["amount"]):
                self._record([ln], [k], "P3 short-paid within policy", 0.90)

    def p4_split(self):
        partials: dict[int, list[dict]] = {}
        for ln in self._remaining():
            if len(ln["refs"]) == 1:
                k = ln["refs"][0]
                if k not in self.settled and ln["amount"] < self.amount[k]:
                    partials.setdefault(k, []).append(ln)
        for k, group in partials.items():
            group = [g for g in group if self._customer(g) in (None, self.customer_of[k])]
            for size in range(len(group), 1, -1):
                hit = next((c for c in itertools.combinations(group, size)
                            if abs(sum(g["amount"] for g in c) - self.amount[k]) <= policy.CENT), None)
                if hit:
                    self._record(list(hit), [k], "P4 split payment", 0.92)
                    break

    def p2_typo(self):
        for ln in self._remaining():
            if not ln["raw_refs"]:
                continue
            cid = self._customer(ln)
            if not cid:
                continue
            hits = [k for k in self._open_for(cid)
                    if abs(self.amount[k] - ln["amount"]) <= policy.CENT
                    and any(DamerauLevenshtein.distance(str(r), str(k)) == 1 for r in ln["raw_refs"])]
            if len(hits) == 1:
                self._record([ln], hits, "P2 reference typo corrected", 0.85)

    def p5_combined(self):
        for ln in self._remaining():
            cid = self._customer(ln)
            if not cid:
                continue
            candidates = [k for k in self._open_for(cid)
                          if self.amount[k] < ln["amount"] and self._plausible_date(ln, k)]
            required = [k for k in dict.fromkeys(ln["refs"]) if k in candidates]
            others = sorted((k for k in candidates if k not in required),
                            key=lambda k: abs((self.due_date[k] - ln["value_date"]).days))
            others = others[:policy.MAX_COMBINATION_CANDIDATES]
            found = []
            for size in range(max(2, len(required)), policy.MAX_COMBINATION_SIZE + 1):
                for extra in itertools.combinations(others, size - len(required)):
                    combo = tuple(required) + extra
                    if abs(sum(self.amount[k] for k in combo) - ln["amount"]) <= policy.CENT:
                        found.append(combo)
                if found:
                    break
            if len(found) == 1:
                self._record([ln], list(found[0]), "P5 combined payment", 0.85)
            elif len(found) > 1:
                self._exception(ln, "ambiguous combined payment")

    def p6_amount_and_payer(self):
        for ln in self._remaining():
            cid = self._customer(ln)
            if not cid:
                continue
            hits = [k for k in self._open_for(cid)
                    if abs(self.amount[k] - ln["amount"]) <= policy.CENT and self._plausible_date(ln, k)]
            if len(hits) == 1:
                self._record([ln], hits, "P6 amount and payer", 0.80)
            elif len(hits) > 1:
                self._exception(ln, "ambiguous: several open invoices for this amount")

    def sweep(self):
        for ln in self._remaining():
            if len(ln["refs"]) == 1 and ln["refs"][0] not in self.settled and ln["amount"] < self.amount[ln["refs"][0]]:
                self._exception(ln, "short payment outside policy", ln["refs"][0])
            elif self._customer(ln):
                self._exception(ln, "payer identified, no matching open invoice")
            else:
                self._exception(ln, "unidentified receipt")

    # ------------------------------------------------------------------ run
    def run(self) -> Result:
        t0 = time.perf_counter()
        for step in (self.p1_exact, self.p2_typo, self.p3_short_paid, self.p4_split,
                     self.p5_combined, self.p6_amount_and_payer, self.sweep):
            step()
        elapsed = time.perf_counter() - t0

        matches = pd.DataFrame(self.match_rows)
        exceptions = pd.DataFrame(self.exception_rows)
        open_inv = self.invoices[~self.invoices["key"].isin(self.settled)].drop(columns="key")
        total_value = sum(ln["amount"] for ln in self.lines)
        matched_ids = set(matches["bank_txn_id"]) if len(matches) else set()
        matched_value = sum(ln["amount"] for ln in self.lines if ln["bank_txn_id"] in matched_ids)
        summary = {
            "invoices": len(self.invoices),
            "bank_lines": len(self.lines),
            "lines_auto_matched": len(matched_ids),
            "line_match_rate": round(len(matched_ids) / len(self.lines), 4),
            "value_match_rate": round(matched_value / total_value, 4),
            "exceptions": len(exceptions),
            "exceptions_by_reason": exceptions["reason"].value_counts().to_dict() if len(exceptions) else {},
            "matches_by_pass": matches.drop_duplicates(["group_id", "bank_txn_id"])["match_pass"]
                                     .value_counts().to_dict() if len(matches) else {},
            "runtime_seconds": round(elapsed, 2),
        }
        return Result(matches, exceptions, open_inv, summary)


def evaluate(result: Result, truth: pd.DataFrame) -> dict:
    """Score the engine against the labelled ground truth."""
    expected = truth[truth["expected"] == "match"]
    true_pairs = set(zip(expected["bank_txn_id"], expected["invoice_no"]))
    found_pairs = set(zip(result.matches["bank_txn_id"], result.matches["invoice_no"]))
    correct = true_pairs & found_pairs

    exc = result.exceptions.set_index("bank_txn_id")["reason"] if len(result.exceptions) else pd.Series(dtype=str)
    dup_lines = set(truth.loc[truth["expected"] == "duplicate", "bank_txn_id"])
    dup_flagged = {t for t in dup_lines if exc.get(t) == "possible duplicate payment"}
    noise_lines = set(truth.loc[truth["expected"] == "noise", "bank_txn_id"])
    matched_lines = set(result.matches["bank_txn_id"])

    return {
        "precision": round(len(correct) / len(found_pairs), 4) if found_pairs else 0.0,
        "recall": round(len(correct) / len(true_pairs), 4) if true_pairs else 0.0,
        "incorrect_pairs": len(found_pairs - true_pairs),
        "duplicates_planted": len(dup_lines),
        "duplicates_flagged": len(dup_flagged),
        "noise_lines_wrongly_matched": len(noise_lines & matched_lines),
    }


def write_outputs(result: Result, out_dir) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_dir / "reconciliation_results.xlsx", engine="openpyxl") as xw:
        pd.DataFrame([{k: v for k, v in result.summary.items() if not isinstance(v, dict)}]).T \
            .rename(columns={0: "value"}).to_excel(xw, sheet_name="Summary")
        result.matches.to_excel(xw, sheet_name="Matches", index=False)
        result.exceptions.to_excel(xw, sheet_name="Exception queue", index=False)
        result.open_invoices.to_excel(xw, sheet_name="Open invoices", index=False)
