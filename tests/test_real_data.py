"""«Золотые» проверки на выданном наборе: фиксируем найденные ошибки и сходимость формул."""
import pytest

from villa_finance import finance as fin

ALL = fin.Params(start="2026-01-01", end="2026-12-31")


def excluded(q, entity):
    df = getattr(q.data, entity)
    id_col = {"reservations": "reservation_id", "payments": "payment_id", "expenses": "expense_id"}[entity]
    return sorted(df.loc[df["dq_status"] == "excluded", id_col])


def test_known_errors_are_excluded(real):
    assert excluded(real, "reservations") == ["R0015", "R0034", "R0061", "R0072", "R0102"]
    assert excluded(real, "payments") == ["P0017", "P0075"]
    assert excluded(real, "expenses") == ["E0017"]
    # копия R0015 исключена, оригинал (строка 16) остался
    r = real.data.reservations
    assert r.loc[r["reservation_id"] == "R0015", "dq_status"].tolist() == ["ok", "excluded"]


def test_payment_equals_gross_minus_ota_minus_refund(real):
    """Подтверждение формулы expected_cash: у всех оплаченных броней платёж сходится до рупии."""
    rc = fin.reconciliation(real, ALL)
    paid = rc[rc["received"] > 0]
    assert len(paid) == 66
    assert (paid["diff"].abs() < 1).all()


def test_ledger_matches_reservation_totals(real):
    facts = fin.reservation_facts(real, ALL)
    prop = fin.by_property(real, ALL)
    for col in ["gross_bookings", "ota_fees", "mgmt_fees", "net_revenue"]:
        assert prop[col].sum() == pytest.approx(facts[col].sum())


def test_months_add_up_to_whole_period(real):
    sep = fin.by_property(real, fin.Params(start="2026-09-01", end="2026-09-30"))
    oct_ = fin.by_property(real, fin.Params(start="2026-10-01", end="2026-10-31"))
    whole = fin.by_property(real, fin.Params(start="2026-09-01", end="2026-10-31"))
    for col in ["gross_bookings", "mgmt_fees", "opex", "cash_received", "owner_balance_est"]:
        assert sep[col].sum() + oct_[col].sum() == pytest.approx(whole[col].sum())


def test_systemic_payment_gap_detected(real):
    msg = real.issues.loc[real.issues["rule"] == "SYS_PAYMENT_COVERAGE", "message"].iloc[0]
    assert "чётным" in msg


def test_management_fee_rates(real):
    facts = fin.reservation_facts(real, ALL).set_index("reservation_id")
    # V004 — 20%, V013 — 17% по commission_rules
    for rid, rate in [("R0011", 0.20), ("R0001", 0.17)]:
        assert facts.at[rid, "mgmt_fees"] == pytest.approx(facts.at[rid, "gross_booking_value"] * rate)
