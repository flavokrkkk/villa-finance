"""Демонстрация идемпотентного импорта: python scripts/ingest.py [--db output/warehouse.db]

Запустите дважды — второй прогон ничего не добавит.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from villa_finance.ingest import connect, ingest_dir  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "output" / "warehouse.db"))
    ap.add_argument("--user", default="cli")
    a = ap.parse_args()
    Path(a.db).parent.mkdir(exist_ok=True)
    con = connect(Path(a.db))
    print(pd.DataFrame(ingest_dir(con, loaded_by=a.user)).to_string(index=False))
    total = con.execute("SELECT entity, COUNT(*) FROM current_record GROUP BY entity").fetchall()
    print("\nТекущих записей в хранилище:", dict(total))
