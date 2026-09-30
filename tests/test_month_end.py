import shutil

import pandas as pd
import pytest

from month_end.generate_data import generate
from month_end.pipeline import ValidationError, run


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("month_end")
    generate(d)
    return d


def test_pipeline_balances_and_passes_every_check(data_dir, tmp_path):
    r = run(data_dir, tmp_path)
    bs = r["bs"]["Consolidated"]
    assert abs(bs["Total assets"] - bs["Total liabilities"] - bs["Total equity"]) <= 0.05
    assert r["checks_passed"] == r["checks"]


def test_intercompany_is_fully_eliminated(data_dir, tmp_path):
    r = run(data_dir, tmp_path)
    assert "Intercompany revenue" not in r["pl"].index
    assert r["pl"].loc["Revenue", "ELIM"] == 0


def test_unmapped_account_stops_the_run(data_dir, tmp_path):
    broken = tmp_path / "broken"
    shutil.copytree(data_dir, broken)
    mapping = pd.read_csv(broken / "account_mapping.csv", dtype={"account": str})
    mapping[mapping["account"] != "6300"].to_csv(broken / "account_mapping.csv", index=False)
    with pytest.raises(ValidationError, match="unmapped: 6300"):
        run(broken, tmp_path / "out")


def test_unbalanced_journal_stops_the_run(data_dir, tmp_path):
    broken = tmp_path / "broken2"
    shutil.copytree(data_dir, broken)
    gl = pd.read_csv(broken / "gl_lines.csv", dtype={"account": str})
    gl.loc[0, "amount_local"] += 100
    gl.to_csv(broken / "gl_lines.csv", index=False)
    with pytest.raises(ValidationError, match="1 unbalanced journals"):
        run(broken, tmp_path / "out2")
