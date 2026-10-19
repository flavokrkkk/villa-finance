from villa_finance import finance as fin

OK = "R1,V1,2026-09-01,2026-09-03,confirmed,1000,IDR,100,0,OTA,2026-09-01"


def statuses(q, entity="reservations"):
    df = getattr(q.data, entity)
    return dict(zip(df["_row"], df["dq_status"]))


def rules_of(q, record_id):
    return set(q.issues.loc[q.issues["record_id"] == record_id, "rule"])


def test_exact_duplicate_counted_once(make_data):
    q = make_data([OK, OK])
    assert statuses(q) == {2: "ok", 3: "excluded"}
    t = fin.by_property(q, fin.Params(start="2026-09-01", end="2026-09-30"))
    assert t.set_index("property_id").at["V1", "gross_bookings"] == 1000
    assert "OVERLAP" not in rules_of(q, "R1")  # дубль не должен «пересекаться» с оригиналом


def test_later_version_supersedes_earlier(make_data):
    q = make_data([OK, "R1,V1,2026-09-01,2026-09-04,confirmed,1500,IDR,150,0,OTA,2026-09-10"])
    assert statuses(q) == {2: "excluded", 3: "ok"}
    assert "ID_SUPERSEDED" in rules_of(q, "R1")


def test_hard_errors_are_excluded(make_data):
    q = make_data([
        "R1,V9,2026-09-01,2026-09-03,confirmed,1000,IDR,100,0,OTA,2026-09-01",    # неизвестный объект
        "R2,V1,2026-09-01,2026-09-03,confirmed,1000,USD,100,0,OTA,2026-09-01",    # чужая валюта
        "R3,V1,2026-09-05,2026-09-07,confirmed,1000,IDR,100,1100,OTA,2026-09-01", # refund > gross
        "R4,V1,2026-09-08,2026-09-09,confirmed,1000,IDR,-10,0,OTA,2026-09-01",    # отрицательная комиссия
        "R5,V1,2026-09-12,2026-09-10,confirmed,1000,IDR,100,0,OTA,2026-09-01",    # выезд раньше заезда
        "R6,V2,2026-13-01,2026-09-10,confirmed,1000,IDR,100,0,OTA,2026-09-01",    # дата не парсится
    ])
    assert set(statuses(q).values()) == {"excluded"}
    assert "UNKNOWN_PROPERTY" in rules_of(q, "R1")
    assert "CURRENCY_MISMATCH" in rules_of(q, "R2")
    assert "REFUND_GT_GROSS" in rules_of(q, "R3")
    assert "NEGATIVE_OTA_FEE" in rules_of(q, "R4")
    assert "BAD_DATE" in rules_of(q, "R5") and "BAD_DATE" in rules_of(q, "R6")


def test_overlap_rules(make_data):
    q = make_data([
        "R1,V1,2026-09-01,2026-09-05,confirmed,1000,IDR,100,0,OTA,2026-09-01",
        "R2,V1,2026-09-05,2026-09-07,confirmed,1000,IDR,100,0,OTA,2026-09-01",    # касание — не пересечение
        "R3,V1,2026-09-06,2026-09-08,confirmed,1000,IDR,100,0,OTA,2026-09-01",    # пересекается с R2
        "R4,V1,2026-09-01,2026-09-03,cancelled,1000,IDR,100,1000,OTA,2026-09-01", # отмена не занимает виллу
        "R5,V2,2026-09-01,2026-09-05,confirmed,1000,IDR,100,0,OTA,2026-09-01",    # другой объект
    ])
    flagged = set(q.issues.loc[q.issues["rule"] == "OVERLAP", "record_id"])
    assert flagged == {"R2", "R3"}


def test_payment_rules(make_data):
    q = make_data(
        [OK, "R2,V1,2026-09-05,2026-09-07,confirmed,1000,IDR,100,1100,OTA,2026-09-01"],
        payments=["P1,R1,V1,2026-09-02,900,IDR,received",
                  "P1,R1,V1,2026-09-02,900,IDR,received",   # дубль
                  "P2,R2,V1,2026-09-06,0,IDR,received",     # по исключённой брони
                  "P3,R9,V1,2026-09-06,500,IDR,received",   # бронь не найдена
                  "P4,R1,V2,2026-09-06,500,IDR,received"])  # объект не совпадает
    s = statuses(q, "payments")
    assert s == {2: "ok", 3: "excluded", 4: "review", 5: "review", 6: "review"}
    assert "PAYMENT_FOR_EXCLUDED_RES" in rules_of(q, "P2")
    assert "PAYMENT_UNKNOWN_RESERVATION" in rules_of(q, "P3")
    assert "PAYMENT_PROPERTY_MISMATCH" in rules_of(q, "P4")


def test_expense_rules(make_data):
    q = make_data(expenses=["E1,V1,2026-09-01,Cleaning,100,IDR,paid",
                            "E2,V1,2026-09-01,Cleaning,100,IDR,paid",    # похож на E1
                            "E3,V7,2026-09-01,Cleaning,100,IDR,paid"])   # неизвестный объект
    assert statuses(q, "expenses") == {2: "review", 3: "review", 4: "excluded"}


def test_missing_commission_rule_goes_to_review(make_data):
    q = make_data([OK], rules=["V1,2026-09-02,2026-12-31,0.2,gross_booking_value,"])  # 01.09 без ставки
    assert "NO_COMMISSION_RULE" in rules_of(q, "R1")
