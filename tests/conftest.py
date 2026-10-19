from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from villa_finance.load import load_dataset  # noqa: E402
from villa_finance.quality import run_checks  # noqa: E402

HEADERS = {
    "properties.csv": "property_id,property_name,cluster,owner_id,base_currency",
    "reservations.csv": "reservation_id,property_id,check_in,check_out,status,gross_booking_value,currency,"
                        "ota_fee,refund_amount,channel,updated_at",
    "payments.csv": "payment_id,reservation_id,property_id,payment_date,amount,currency,status",
    "expenses.csv": "expense_id,property_id,expense_date,category,amount,currency,payment_status",
    "commission_rules.csv": "property_id,effective_from,effective_to,management_fee_rate,fee_basis,note",
}
DEFAULT_PROPS = ["V1,Villa 1,Ubud,O1,IDR", "V2,Villa 2,Canggu,O2,IDR"]
DEFAULT_RULES = ["V1,2026-01-01,2026-12-31,0.2,gross_booking_value,", "V2,2026-01-01,2026-12-31,0.2,gross_booking_value,"]


@pytest.fixture
def make_data(tmp_path):
    """make_data(reservations=[...csv lines...], ...) -> QualityResult"""

    def _make(reservations=(), payments=(), expenses=(), properties=DEFAULT_PROPS, rules=DEFAULT_RULES):
        rows = {"properties.csv": properties, "reservations.csv": reservations, "payments.csv": payments,
                "expenses.csv": expenses, "commission_rules.csv": rules}
        for name, lines in rows.items():
            (tmp_path / name).write_text("﻿" + "\n".join([HEADERS[name], *lines]) + "\n", encoding="utf-8")
        return run_checks(load_dataset(tmp_path))

    return _make


@pytest.fixture(scope="session")
def real():
    return run_checks(load_dataset())
