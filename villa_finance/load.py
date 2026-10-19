"""Загрузка исходных CSV.

Всё читается как строки, типизация — отдельным шагом с errors="coerce":
некорректное значение не роняет загрузку, а превращается в NaN/NaT и потом
ловится правилом качества. Каждая строка помнит файл и номер строки в нём
(`_row`), чтобы любую проблему можно было показать пользователю с адресом.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

FILES = {
    "properties": "properties.csv",
    "reservations": "reservations.csv",
    "payments": "payments.csv",
    "expenses": "expenses.csv",
    "commission_rules": "commission_rules.csv",
}

DATE_COLS = {
    "reservations": ["check_in", "check_out", "updated_at"],
    "payments": ["payment_date"],
    "expenses": ["expense_date"],
    "commission_rules": ["effective_from", "effective_to"],
}
NUM_COLS = {
    "reservations": ["gross_booking_value", "ota_fee", "refund_amount"],
    "payments": ["amount"],
    "expenses": ["amount"],
    "commission_rules": ["management_fee_rate"],
}


@dataclass
class Dataset:
    properties: pd.DataFrame
    reservations: pd.DataFrame
    payments: pd.DataFrame
    expenses: pd.DataFrame
    commission_rules: pd.DataFrame


def read_raw(path: Path, entity: str) -> pd.DataFrame:
    # utf-8-sig снимает BOM, иначе первая колонка называется '﻿reservation_id'
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    df = df.apply(lambda s: s.str.strip())
    df.insert(0, "_row", range(2, len(df) + 2))  # номер строки в файле (1 — заголовок)
    df.insert(1, "_source", path.name)
    return df


def typed(df: pd.DataFrame, entity: str) -> pd.DataFrame:
    df = df.copy()
    for c in DATE_COLS.get(entity, []):
        df[c] = pd.to_datetime(df[c], format="%Y-%m-%d", errors="coerce")
    for c in NUM_COLS.get(entity, []):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_dataset(data_dir: Path = DATA_DIR) -> Dataset:
    frames = {e: typed(read_raw(Path(data_dir) / f, e), e) for e, f in FILES.items()}
    return Dataset(**frames)
