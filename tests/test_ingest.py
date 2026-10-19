import shutil

from villa_finance.ingest import connect, ingest_dir, ingest_file
from villa_finance.load import DATA_DIR


def test_reimport_is_idempotent(tmp_path):
    con = connect(tmp_path / "w.db")
    first = {r["entity"]: r for r in ingest_dir(con)}
    assert first["reservations"]["new_records"] == 150  # 151 строка, одна — полный дубль
    assert first["reservations"]["duplicates_ignored"] == 1
    before = con.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0]

    second = ingest_dir(con)
    assert all(r["status"].startswith("skipped") for r in second)
    assert con.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == before


def test_same_rows_in_new_file_do_not_duplicate_and_changes_are_versioned(tmp_path):
    con = connect(tmp_path / "w.db")
    src = tmp_path / "reservations.csv"
    shutil.copy(DATA_DIR / "reservations.csv", src)
    ingest_file(con, "reservations", src)

    # новый файл: те же строки + изменённая бронь R0001
    text = src.read_text(encoding="utf-8-sig").replace(
        "R0001,V013,2026-09-22,2026-09-25,confirmed,5285633", "R0001,V013,2026-09-22,2026-09-26,confirmed,6000000")
    text = text.replace("R0001,V013,2026-09-22,2026-09-26,confirmed,6000000,IDR,792845,0,OTA,2026-09-15",
                        "R0001,V013,2026-09-22,2026-09-26,confirmed,6000000,IDR,792845,0,OTA,2026-10-20")
    v2 = tmp_path / "reservations_v2.csv"
    v2.write_text(text, encoding="utf-8")
    stats = ingest_file(con, "reservations", v2)

    assert stats["new_records"] == 0 and stats["new_versions"] == 1
    versions = con.execute("SELECT COUNT(*) FROM raw_record WHERE natural_key='R0001'").fetchone()[0]
    current = con.execute("SELECT payload FROM current_record WHERE natural_key='R0001'").fetchone()[0]
    assert versions == 2 and '"6000000"' in current
