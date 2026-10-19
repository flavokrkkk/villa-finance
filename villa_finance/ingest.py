"""Идемпотентная загрузка CSV в SQLite — минимальная модель будущего хранилища.

Три уровня защиты от дублей при повторном импорте:

1. файл: SHA-256 содержимого; тот же файл второй раз не загружается вовсе;
2. запись: (сущность, натуральный ключ, хэш содержимого) — первичный ключ,
   INSERT OR IGNORE; та же строка в новом файле не создаёт копию;
3. версия: тот же ключ с другим содержимым — новая версия, старая остаётся
   (история = аудит изменений). «Текущее» состояние — последняя версия.

Хранилище — append-only: ничего не удаляется и не перезаписывается.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .load import DATA_DIR, FILES, read_raw

NATURAL_KEY = {
    "properties": ["property_id"],
    "reservations": ["reservation_id"],
    "payments": ["payment_id"],
    "expenses": ["expense_id"],
    "commission_rules": ["property_id", "effective_from"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batch (
    batch_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    entity       TEXT NOT NULL,
    file_name    TEXT NOT NULL,
    file_sha256  TEXT NOT NULL,
    rows_in_file INTEGER NOT NULL,
    new_records  INTEGER NOT NULL,
    new_versions INTEGER NOT NULL,
    loaded_by    TEXT NOT NULL,
    loaded_at    TEXT NOT NULL,
    UNIQUE (entity, file_sha256)
);
CREATE TABLE IF NOT EXISTS raw_record (
    entity       TEXT NOT NULL,
    natural_key  TEXT NOT NULL,
    record_hash  TEXT NOT NULL,
    batch_id     INTEGER NOT NULL REFERENCES import_batch(batch_id),
    source_row   INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    PRIMARY KEY (entity, natural_key, record_hash)
);
-- текущая версия = запись из самого позднего батча по ключу
CREATE VIEW IF NOT EXISTS current_record AS
SELECT r.* FROM raw_record r
WHERE r.batch_id = (SELECT MAX(batch_id) FROM raw_record x
                    WHERE x.entity = r.entity AND x.natural_key = r.natural_key);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    return con


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ingest_file(con: sqlite3.Connection, entity: str, path: Path, loaded_by: str = "cli") -> dict:
    path = Path(path)
    digest = _sha256(path)
    if con.execute("SELECT 1 FROM import_batch WHERE entity=? AND file_sha256=?", (entity, digest)).fetchone():
        return {"entity": entity, "file": path.name, "status": "skipped: файл уже загружен", "rows": 0,
                "new_records": 0, "new_versions": 0, "duplicates_ignored": 0}

    df = read_raw(path, entity)
    business = [c for c in df.columns if not c.startswith("_")]
    with con:  # одна транзакция на файл: либо весь файл, либо ничего
        cur = con.execute(
            "INSERT INTO import_batch(entity, file_name, file_sha256, rows_in_file, new_records, new_versions,"
            " loaded_by, loaded_at) VALUES (?,?,?,?,0,0,?,?)",
            (entity, path.name, digest, len(df), loaded_by, datetime.now(timezone.utc).isoformat()))
        batch_id = cur.lastrowid
        new_records = new_versions = dups = 0
        for rec in df.to_dict("records"):
            payload = json.dumps({k: rec[k] for k in business}, ensure_ascii=False, sort_keys=True)
            key = "|".join(str(rec[k]) for k in NATURAL_KEY[entity])
            rhash = hashlib.sha256(payload.encode()).hexdigest()
            existed = con.execute("SELECT 1 FROM raw_record WHERE entity=? AND natural_key=?",
                                  (entity, key)).fetchone()
            inserted = con.execute(
                "INSERT OR IGNORE INTO raw_record VALUES (?,?,?,?,?,?)",
                (entity, key, rhash, batch_id, rec["_row"], payload)).rowcount
            if not inserted:
                dups += 1
            elif existed:
                new_versions += 1
            else:
                new_records += 1
        con.execute("UPDATE import_batch SET new_records=?, new_versions=? WHERE batch_id=?",
                    (new_records, new_versions, batch_id))
    return {"entity": entity, "file": path.name, "status": "loaded", "rows": len(df),
            "new_records": new_records, "new_versions": new_versions, "duplicates_ignored": dups}


def ingest_dir(con: sqlite3.Connection, data_dir: Path = DATA_DIR, loaded_by: str = "cli") -> list[dict]:
    return [ingest_file(con, e, Path(data_dir) / f, loaded_by) for e, f in FILES.items()]
