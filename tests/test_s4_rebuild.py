import pytest

from s4_rebuild.generate_data import generate
from s4_rebuild.parallel_run import load, reconcile, remediate


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    d = tmp_path_factory.mktemp("s4")
    generate(d, n_docs=8_000)
    return load(d)


def test_first_run_finds_both_planted_defects(data):
    run = reconcile(data)
    assert run.summary["unmapped_accounts"] == ["66000000"]
    assert len(run.summary["documents_with_line_count_breaks"]) == 1
    assert run.summary["cells_different"] > 0


def test_remediated_run_matches_every_cell(data):
    first = reconcile(data)
    second = reconcile(remediate(data, first, {"66000000": "Depreciation"}))
    assert second.summary["cells_different"] == 0
    assert second.summary["net_difference"] == 0
