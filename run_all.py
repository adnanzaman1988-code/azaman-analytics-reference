"""Generate the synthetic datasets, run all three reference pipelines and
write their outputs to ./output. Prints a summary and saves metrics.json."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from month_end import pipeline as month_end
from month_end.generate_data import generate as gen_month_end
from reconciliation.engine import Reconciler, evaluate, write_outputs as write_recon
from reconciliation.generate_data import generate as gen_recon
from s4_rebuild import parallel_run
from s4_rebuild.generate_data import generate as gen_s4

DATA, OUT = Path("data"), Path("output")


def main() -> dict:
    metrics = {}

    paths = gen_recon(DATA / "reconciliation")
    truth = pd.read_csv(paths["truth"], keep_default_na=False)
    result = Reconciler(pd.read_csv(paths["invoices"]), pd.read_csv(paths["bank"])).run()
    write_recon(result, OUT / "reconciliation")
    metrics["reconciliation"] = {**result.summary, **evaluate(result, truth)}

    gen_month_end(DATA / "month_end")
    me = month_end.run(DATA / "month_end", OUT / "month_end")
    metrics["month_end"] = {k: v for k, v in me.items() if not isinstance(v, pd.DataFrame)}

    gen_s4(DATA / "s4_rebuild")
    d = parallel_run.load(DATA / "s4_rebuild")
    first = parallel_run.reconcile(d)
    second = parallel_run.reconcile(parallel_run.remediate(d, first, {"66000000": "Depreciation"}))
    parallel_run.write_outputs(first, second, OUT / "s4_rebuild")
    metrics["s4_rebuild"] = {"first_run": first.summary, "after_remediation": second.summary}

    OUT.mkdir(exist_ok=True)
    (OUT / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    return metrics


if __name__ == "__main__":
    t0 = time.perf_counter()
    m = main()
    print(json.dumps(m, indent=2, default=str))
    print(f"\nAll pipelines complete in {time.perf_counter() - t0:.1f}s. Outputs in ./output")
