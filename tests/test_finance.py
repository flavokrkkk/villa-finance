import pandas as pd
import pytest

from villa_finance import finance as fin

SEP = dict(start="2026-09-01", end="2026-09-30")
OCT = dict(start="2026-10-01", end="2026-10-31")
ALL = dict(start="2026-01-01", end="2026-12-31")

# 3 ночи: 29.09, 30.09, 01.10 — две в сентябре, одна в октябре
CROSS = "R1,V1,2026-09-29,2026-10-02,confirmed,3000,IDR,300,0,OTA,2026-09-01"


def row(q, period, prop="V1", **kw):
    t = fin.by_property(q, fin.Params(**period, **kw))
    return t.set_index("property_id").loc[prop]


def test_pro_rata_split_across_months(make_data):
    q = make_data([CROSS])
    sep, oct_ = row(q, SEP), row(q, OCT)
    assert sep["gross_bookings"] == pytest.approx(2000)
    assert oct_["gross_bookings"] == pytest.approx(1000)
    assert sep["ota_fees"] == pytest.approx(200)
    assert sep["mgmt_fees"] == pytest.approx(400)  # 20% от gross, OTA не вычитается
    assert sep["nights_sold"] == 2 and oct_["nights_sold"] == 1


@pytest.mark.parametrize("attribution, sep_gross", [("check_in", 3000), ("check_out", 0)])
def test_attribution_moves_whole_booking(make_data, attribution, sep_gross):
    q = make_data([CROSS])
    assert row(q, SEP, attribution=attribution)["gross_bookings"] == pytest.approx(sep_gross)
    # загрузка не зависит от метода признания денег
    assert row(q, SEP, attribution=attribution)["nights_sold"] == 2


def test_rate_change_mid_stay_applies_per_night(make_data):
    rules = ["V1,2026-01-01,2026-09-30,0.1,gross_booking_value,", "V1,2026-10-01,2026-12-31,0.3,gross_booking_value,",
             "V2,2026-01-01,2026-12-31,0.2,gross_booking_value,"]
    q = make_data([CROSS], rules=rules)
    assert row(q, ALL)["mgmt_fees"] == pytest.approx(2000 * 0.1 + 1000 * 0.3)


def test_full_cancellation_is_zero_but_reported(make_data):
    q = make_data(["R1,V1,2026-09-10,2026-09-12,cancelled,1000,IDR,150,1000,OTA,2026-09-01"])
    r = row(q, ALL)
    assert r[["gross_bookings", "net_revenue", "ota_fees", "mgmt_fees", "nights_sold"]].sum() == 0
    assert r["cancelled_gross"] == pytest.approx(1000)


def test_cancellation_with_penalty_keeps_retained_amount(make_data):
    q = make_data(["R1,V1,2026-09-10,2026-09-12,cancelled,1000,IDR,150,700,OTA,2026-09-01"])
    r = row(q, ALL)
    assert r["cancel_retained"] == pytest.approx(300)
    assert r["net_revenue"] == pytest.approx(300)
    assert r["mgmt_fees"] == pytest.approx(60)
    assert q.data.reservations["dq_status"].iloc[0] == "review"  # частичный возврат — на проверку


def test_owner_balance_identity_and_pending_expenses(make_data):
    q = make_data([CROSS], expenses=["E1,V1,2026-09-15,Cleaning,100,IDR,paid",
                                     "E2,V1,2026-09-16,Supplies,50,IDR,pending"])
    r = row(q, SEP)
    assert r["opex"] == 150 and r["opex_paid"] == 100
    assert r["owner_balance_est"] == pytest.approx(2000 - 200 - 400 - 150)


def test_reconciliation_statuses(make_data):
    q = make_data(
        ["R1,V1,2026-09-01,2026-09-03,confirmed,1000,IDR,100,0,OTA,2026-09-01",   # оплачено ровно
         "R2,V1,2026-09-05,2026-09-07,confirmed,1000,IDR,100,0,OTA,2026-09-01",   # недоплата
         "R3,V2,2026-09-01,2026-09-03,confirmed,1000,IDR,100,0,OTA,2026-09-01",   # не оплачено, срок прошёл
         "R4,V2,2026-09-25,2026-09-28,confirmed,1000,IDR,100,0,OTA,2026-09-01"],  # срок не наступил
        payments=["P1,R1,V1,2026-09-02,900,IDR,received", "P2,R2,V1,2026-09-06,800,IDR,received"])
    rc = fin.reconciliation(q, fin.Params(**SEP, as_of=pd.Timestamp("2026-09-20"))).set_index("reservation_id")
    assert rc.loc["R1", "recon_status"] == "сходится"
    assert rc.loc["R2", "recon_status"] == "недоплата" and rc.loc["R2", "diff"] == pytest.approx(-100)
    assert rc.loc["R3", "recon_status"] == "не получено, срок прошёл"
    assert rc.loc["R4", "recon_status"] == "не получено, срок не наступил"


def test_cash_does_not_depend_on_review_toggle(make_data):
    q = make_data(["R1,V1,2026-09-01,2026-09-05,confirmed,1000,IDR,100,0,OTA,2026-09-01",
                   "R2,V1,2026-09-03,2026-09-06,confirmed,1000,IDR,100,0,OTA,2026-09-01"],  # пересечение
                  payments=["P1,R1,V1,2026-09-02,900,IDR,received"])
    on, off = row(q, SEP, include_review=True), row(q, SEP, include_review=False)
    assert on["cash_received"] == off["cash_received"] == 900
    assert on["gross_bookings"] == pytest.approx(2000) and off["gross_bookings"] == 0
