import pandas as pd
import pytest

from reconciliation.engine import Reconciler, evaluate, extract_refs, normalise_name
from reconciliation.generate_data import generate


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    paths = generate(tmp_path_factory.mktemp("recon"), n_invoices=5_000, n_customers=150, seed=3)
    truth = pd.read_csv(paths["truth"], keep_default_na=False)
    res = Reconciler(pd.read_csv(paths["invoices"]), pd.read_csv(paths["bank"])).run()
    return res, evaluate(res, truth)


def test_reference_extraction_handles_formats():
    assert extract_refs("INV-104532") == [104532]
    assert extract_refs("inv 104532 thanks") == [104532]
    assert extract_refs("00104532") == [104532]
    assert extract_refs("TRF 20260915") == []


def test_name_normalisation_strips_legal_suffixes():
    assert normalise_name("Coastal Logistics Pty Ltd") == "COASTAL LOGISTICS"
    assert normalise_name("COASTAL LOGISTICS P/L") == "COASTAL LOGISTICS"


def test_no_incorrect_matches(result):
    _, score = result
    assert score["incorrect_pairs"] == 0
    assert score["precision"] == 1.0


def test_high_recall(result):
    _, score = result
    assert score["recall"] >= 0.98


def test_every_duplicate_payment_is_flagged(result):
    _, score = result
    assert score["duplicates_flagged"] == score["duplicates_planted"]


def test_bank_noise_is_never_matched(result):
    _, score = result
    assert score["noise_lines_wrongly_matched"] == 0
