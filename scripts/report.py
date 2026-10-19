"""CLI: проверки качества + расчёт за период, выгрузка CSV в output/.

    python scripts/report.py                       # весь период данных
    python scripts/report.py --start 2026-10-01 --end 2026-10-31 --exclude-review
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from villa_finance import finance as fin  # noqa: E402
from villa_finance.load import load_dataset  # noqa: E402
from villa_finance.quality import run_checks, summary  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-01")
    ap.add_argument("--end", default="2026-10-31")
    ap.add_argument("--attribution", default="stay_night", choices=list(fin.ATTRIBUTIONS))
    ap.add_argument("--exclude-review", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "output"))
    a = ap.parse_args()

    q = run_checks(load_dataset())
    p = fin.Params(start=a.start, end=a.end, attribution=a.attribution, include_review=not a.exclude_review)
    prop = fin.by_property(q, p)
    tot = fin.totals(prop, p)
    recon = fin.reconciliation(q, p)

    out = Path(a.out)
    out.mkdir(exist_ok=True)
    q.issues.to_csv(out / "dq_issues.csv", index=False, encoding="utf-8-sig")
    summary(q.issues).to_csv(out / "dq_summary.csv", index=False, encoding="utf-8-sig")
    prop.round(2).to_csv(out / "by_property.csv", index=False, encoding="utf-8-sig")
    recon.to_csv(out / "reconciliation.csv", index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 200)
    print(f"Период {p.start:%Y-%m-%d}..{p.end:%Y-%m-%d}, признание: {a.attribution}, "
          f"записи на проверке: {'не учтены' if a.exclude_review else 'учтены'}\n")
    for k in ["gross_bookings", "refunds", "cancel_retained", "net_revenue", "ota_fees", "mgmt_fees", "opex",
              "owner_balance_est", "expected_cash", "cash_received", "accrual_vs_cash_gap", "review_gross"]:
        print(f"  {k:<22}{tot[k]:>18,.0f}")
    print(f"  {'occupancy':<22}{tot['occupancy']:>18.1%}")
    print("\nКачество данных:")
    print(summary(q.issues)[["severity", "entity", "rule", "records", "amount_at_risk", "examples"]]
          .to_string(index=False))
    print("\nСверка:")
    print(recon.groupby("recon_status")[["expected_cash", "received"]].agg(["count", "sum"]).to_string())
    print(f"\nCSV → {out}")


if __name__ == "__main__":
    main()
