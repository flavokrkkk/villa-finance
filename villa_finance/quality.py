"""Правила качества данных.

Каждое правило возвращает «проблему» с адресом записи (файл, строка, ID),
кодом правила, решением и суммой под риском. Данные не исправляются молча:

* exclude — запись не участвует в расчётах (нельзя корректно посчитать или
  атрибутировать: дубль, неизвестный объект, невозможная арифметика, чужая валюта);
* review  — запись участвует в расчётах, но помечена и уходит на проверку
  (данные правдоподобны по отдельности, но конфликтуют с другими записями);
* info    — системное наблюдение / вопрос к бизнесу, на расчёт не влияет.

Итоговый статус записи (`dq_status`): excluded > review > ok.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .load import Dataset
from .nights import attach_rates, expand_nights

EXCLUDE, REVIEW, INFO = "exclude", "review", "info"

ACTIVE_STATUSES = {"confirmed", "modified"}
KNOWN_STATUSES = ACTIVE_STATUSES | {"cancelled"}
MAX_PLAUSIBLE_OTA_RATE = 0.30

RULES = {
    # код: (сущность, решение, описание)
    "DUP_EXACT": ("*", EXCLUDE, "Полный дубль строки — учитываем первую копию"),
    "ID_SUPERSEDED": ("*", EXCLUDE, "Есть более поздняя версия записи с тем же ID (updated_at)"),
    "ID_CONFLICT": ("*", REVIEW, "Один ID — разное содержимое без признака версии"),
    "UNKNOWN_PROPERTY": ("*", EXCLUDE, "property_id отсутствует в справочнике объектов"),
    "BAD_DATE": ("*", EXCLUDE, "Дата не распознана или check_out <= check_in"),
    "BAD_AMOUNT": ("*", EXCLUDE, "Сумма пустая/отрицательная/нулевая там, где это невозможно"),
    "CURRENCY_MISMATCH": ("*", EXCLUDE, "Валюта отличается от базовой валюты объекта, курса нет"),
    "UNKNOWN_STATUS": ("*", EXCLUDE, "Неизвестный статус"),
    "REFUND_GT_GROSS": ("reservations", EXCLUDE, "Возврат больше стоимости бронирования"),
    "NEGATIVE_OTA_FEE": ("reservations", EXCLUDE, "Отрицательная комиссия OTA"),
    "OTA_RATE_OUTLIER": ("reservations", REVIEW, "Доля комиссии OTA вне правдоподобного диапазона"),
    "PARTIAL_REFUND": ("reservations", REVIEW, "Частичный возврат: статус и сумма возврата не согласованы"),
    "NO_COMMISSION_RULE": ("reservations", REVIEW, "Нет действующей ставки management fee на часть ночей"),
    "OVERLAP": ("reservations", REVIEW, "Пересечение периодов проживания на одном объекте"),
    "DIRECT_WITH_OTA_FEE": ("reservations", INFO, "Прямое бронирование с комиссией OTA"),
    "PAYMENT_UNKNOWN_RESERVATION": ("payments", REVIEW, "Платёж не сопоставлен с бронированием"),
    "PAYMENT_PROPERTY_MISMATCH": ("payments", REVIEW, "Объект платежа ≠ объект бронирования"),
    "PAYMENT_FOR_EXCLUDED_RES": ("payments", REVIEW, "Платёж по исключённому бронированию — деньги есть, начисления нет"),
    "PAYMENT_ON_CANCELLED": ("payments", REVIEW, "Ненулевой платёж по отменённому бронированию"),
    "PAYMENT_ZERO": ("payments", INFO, "Нулевой платёж (запись без движения денег)"),
    "PAYMENT_NOT_RECEIVED": ("payments", EXCLUDE, "Статус платежа не 'received' — не денежное поступление"),
    "EXPENSE_SUSPECT_DUP": ("expenses", REVIEW, "Похожий расход: объект, дата, категория и сумма совпадают"),
    "EXPENSE_UNKNOWN_STATUS": ("expenses", REVIEW, "Неизвестный статус оплаты расхода"),
    "RULE_OVERLAP": ("commission_rules", REVIEW, "Пересекающиеся периоды ставок по объекту"),
    "RULE_BAD_RATE": ("commission_rules", EXCLUDE, "Ставка вне диапазона (0; 1)"),
    "RULE_UNKNOWN_BASIS": ("commission_rules", REVIEW, "Неизвестная база начисления комиссии"),
    "SYS_PAYMENT_COVERAGE": ("dataset", INFO, "Системный пропуск платежей"),
}

ID_COL = {
    "reservations": "reservation_id",
    "payments": "payment_id",
    "expenses": "expense_id",
    "commission_rules": "property_id",
}
AMOUNT_COL = {"reservations": "gross_booking_value", "payments": "amount", "expenses": "amount"}
KNOWN_FEE_BASES = {"gross_booking_value", "net_of_ota_fee"}


@dataclass
class QualityResult:
    issues: pd.DataFrame
    data: Dataset  # те же таблицы + колонки dq_status / dq_rules


class _Collector:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, entity: str, df: pd.DataFrame, rule: str, message, related=None) -> None:
        """message/related — строка или функция от строки df."""
        if df is None or df.empty:
            return
        severity = RULES[rule][1]
        amt_col = AMOUNT_COL.get(entity)
        for _, r in df.iterrows():
            self.rows.append(
                {
                    "entity": entity,
                    "record_id": r.get(ID_COL.get(entity, ""), ""),
                    "source": r.get("_source", ""),
                    "row": r.get("_row"),
                    "property_id": r.get("property_id", ""),
                    "rule": rule,
                    "severity": severity,
                    "message": message(r) if callable(message) else message,
                    "related_id": (related(r) if callable(related) else related) or "",
                    "amount_at_risk": float(r[amt_col]) if amt_col and pd.notna(r.get(amt_col)) else 0.0,
                }
            )

    def frame(self) -> pd.DataFrame:
        cols = ["entity", "record_id", "source", "row", "property_id", "rule", "severity",
                "message", "related_id", "amount_at_risk"]
        return pd.DataFrame(self.rows, columns=cols)


def _business_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if not c.startswith("_")]


def _excluded_rows(c: _Collector, entity: str) -> set:
    return {r["row"] for r in c.rows if r["entity"] == entity and r["severity"] == EXCLUDE}


def _check_duplicates(c: _Collector, entity: str, df: pd.DataFrame, version_col: str | None = None) -> None:
    id_col = ID_COL[entity]
    cols = _business_cols(df)
    exact = df[df.duplicated(cols, keep="first")]
    c.add(entity, exact, "DUP_EXACT",
          lambda r: f"Полная копия {r[id_col]} (строка {r['_row']}) — исключена, первая копия остаётся")

    rest = df.drop(exact.index)
    multi = rest[rest[id_col].duplicated(keep=False)]
    for _, g in multi.groupby(id_col):
        if version_col and g[version_col].notna().all() and g[version_col].nunique() == len(g):
            older = g.sort_values(version_col).iloc[:-1]
            latest = g.sort_values(version_col).iloc[-1]
            c.add(entity, older, "ID_SUPERSEDED",
                  f"Заменена версией от {latest[version_col]:%Y-%m-%d} (строка {latest['_row']})")
        else:
            c.add(entity, g, "ID_CONFLICT", "Несколько разных записей с одним ID")


def _check_common(c: _Collector, entity: str, df: pd.DataFrame, props: pd.DataFrame,
                  date_cols: list[str], amount_cols: list[str], allow_zero: bool = False) -> None:
    base_ccy = props.set_index("property_id")["base_currency"]
    unknown = df[~df["property_id"].isin(props["property_id"])]
    c.add(entity, unknown, "UNKNOWN_PROPERTY", lambda r: f"Объект {r['property_id']} не найден в properties.csv")

    bad_date = df[df[date_cols].isna().any(axis=1)]
    c.add(entity, bad_date, "BAD_DATE", "Пустая или нераспознанная дата")

    neg = (df[amount_cols] < 0) if allow_zero else (df[amount_cols] <= 0)
    bad_amt = df[df[amount_cols].isna().any(axis=1) | neg.any(axis=1)]
    if entity == "reservations":  # для брони отдельно ловим отрицательную комиссию
        bad_amt = df[df["gross_booking_value"].isna() | (df["gross_booking_value"] <= 0)
                     | df["ota_fee"].isna() | df["refund_amount"].isna() | (df["refund_amount"] < 0)]
    c.add(entity, bad_amt, "BAD_AMOUNT", "Некорректная сумма")

    known = df[df["property_id"].isin(base_ccy.index)]
    ccy = known[known["currency"] != known["property_id"].map(base_ccy)]
    c.add(entity, ccy, "CURRENCY_MISMATCH",
          lambda r: f"Валюта {r['currency']} при базовой {base_ccy[r['property_id']]}; "
                    f"сумма {r[AMOUNT_COL[entity]]:,.0f} по порядку величины похожа на "
                    f"{base_ccy[r['property_id']]} — вероятно, ошибка кода валюты")


def _check_reservations(c: _Collector, ds: Dataset) -> None:
    res, e = ds.reservations, "reservations"
    _check_duplicates(c, e, res, version_col="updated_at")
    _check_common(c, e, res, ds.properties, ["check_in", "check_out"], ["gross_booking_value"])

    bad_range = res[res["check_in"].notna() & res["check_out"].notna() & (res["check_out"] <= res["check_in"])]
    c.add(e, bad_range, "BAD_DATE", "check_out не позже check_in")
    c.add(e, res[~res["status"].isin(KNOWN_STATUSES)], "UNKNOWN_STATUS", lambda r: f"Статус '{r['status']}'")

    c.add(e, res[res["refund_amount"] > res["gross_booking_value"]], "REFUND_GT_GROSS",
          lambda r: f"refund {r['refund_amount']:,.0f} > gross {r['gross_booking_value']:,.0f} "
                    f"(разница {r['refund_amount'] - r['gross_booking_value']:,.0f}) при статусе '{r['status']}'")
    c.add(e, res[res["ota_fee"] < 0], "NEGATIVE_OTA_FEE", lambda r: f"ota_fee = {r['ota_fee']:,.0f}")

    rate = res["ota_fee"] / res["gross_booking_value"]
    c.add(e, res[rate > MAX_PLAUSIBLE_OTA_RATE], "OTA_RATE_OUTLIER",
          lambda r: f"OTA fee = {r['ota_fee'] / r['gross_booking_value']:.0%} от gross")

    partial = res[
        (res["status"].isin(ACTIVE_STATUSES) & (res["refund_amount"] > 0) & (res["refund_amount"] <= res["gross_booking_value"]))
        | ((res["status"] == "cancelled") & (res["refund_amount"] < res["gross_booking_value"]))
    ]
    c.add(e, partial, "PARTIAL_REFUND",
          lambda r: f"Статус '{r['status']}', refund {r['refund_amount']:,.0f} из {r['gross_booking_value']:,.0f}")

    c.add(e, res[(res["channel"] == "Direct") & (res["ota_fee"] > 0)], "DIRECT_WITH_OTA_FEE",
          lambda r: f"channel=Direct, ota_fee {r['ota_fee']:,.0f} — это комиссия эквайринга/канала? нужно правило")

    # Ставки и пересечения считаем только по записям, которые не исключены выше —
    # иначе, например, дубль R0015 «пересекается» сам с собой.
    excluded = _excluded_rows(c, e)
    valid = res[~res["_row"].isin(excluded)]

    nightly = attach_rates(expand_nights(valid), _valid_rules(ds.commission_rules))
    no_rate = nightly[nightly["management_fee_rate"].isna()]
    if not no_rate.empty:
        cnt = no_rate.groupby("_row").size()
        miss = valid[valid["_row"].isin(cnt.index)]
        c.add(e, miss, "NO_COMMISSION_RULE", lambda r: f"Без ставки {cnt[r['_row']]} ноч.")

    _check_overlaps(c, valid[valid["status"].isin(ACTIVE_STATUSES)])


def _check_overlaps(c: _Collector, active: pd.DataFrame) -> None:
    """Вилла одна — две активные брони не могут занимать одну ночь.

    Касание (check_out одной = check_in другой) пересечением не считается.
    Отменённые брони не занимают объект и в проверке не участвуют.
    """
    a = active[["_row", "_source", "reservation_id", "property_id", "check_in", "check_out", "status",
                "gross_booking_value", "updated_at"]]
    pairs = a.merge(a, on="property_id", suffixes=("", "_o"))
    pairs = pairs[(pairs["_row"] != pairs["_row_o"])
                  & (pairs["check_in"] < pairs["check_out_o"]) & (pairs["check_in_o"] < pairs["check_out"])]

    def describe(r):
        kind = "пересечение"
        if r["check_in"] == r["check_in_o"] and r["check_out"] == r["check_out_o"]:
            kind = "те же даты — возможный дубль"
        elif "modified" in (r["status"], r["status_o"]):
            kind = "modified-запись — возможно, старая версия не удалена"
        overlap_nights = (min(r["check_out"], r["check_out_o"]) - max(r["check_in"], r["check_in_o"])).days
        return (f"{kind}: {r['reservation_id_o']} ({r['status_o']}, {r['check_in_o']:%m-%d}–{r['check_out_o']:%m-%d}), "
                f"общих ночей: {overlap_nights}")

    for _, g in pairs.groupby("_row", sort=False):
        first = g.iloc[[0]]
        msg = "; ".join(describe(r) for _, r in g.iterrows())
        related = ", ".join(g["reservation_id_o"])
        c.add("reservations", first, "OVERLAP", msg, related)


def _valid_rules(rules: pd.DataFrame) -> pd.DataFrame:
    return rules[(rules["management_fee_rate"] > 0) & (rules["management_fee_rate"] < 1)]


def _check_rules(c: _Collector, ds: Dataset) -> None:
    rules, e = ds.commission_rules, "commission_rules"
    c.add(e, rules[~((rules["management_fee_rate"] > 0) & (rules["management_fee_rate"] < 1))], "RULE_BAD_RATE",
          lambda r: f"management_fee_rate = {r['management_fee_rate']}")
    c.add(e, rules[~rules["fee_basis"].isin(KNOWN_FEE_BASES)], "RULE_UNKNOWN_BASIS",
          lambda r: f"fee_basis = '{r['fee_basis']}'")
    c.add(e, rules[~rules["property_id"].isin(ds.properties["property_id"])], "UNKNOWN_PROPERTY",
          lambda r: f"Ставка для неизвестного объекта {r['property_id']}")
    pairs = rules.merge(rules, on="property_id", suffixes=("", "_o"))
    pairs = pairs[(pairs["_row"] < pairs["_row_o"]) & (pairs["effective_from"] <= pairs["effective_to_o"])
                  & (pairs["effective_from_o"] <= pairs["effective_to"])]
    c.add(e, pairs, "RULE_OVERLAP", lambda r: f"Пересекается со строкой {r['_row_o']}")


def _check_payments(c: _Collector, ds: Dataset, res_status: pd.DataFrame) -> None:
    pay, e = ds.payments, "payments"
    _check_duplicates(c, e, pay)
    _check_common(c, e, pay, ds.properties, ["payment_date"], ["amount"], allow_zero=True)
    c.add(e, pay[pay["status"] != "received"], "PAYMENT_NOT_RECEIVED", lambda r: f"Статус '{r['status']}'")
    c.add(e, pay[pay["amount"] == 0], "PAYMENT_ZERO", "amount = 0")

    excluded = _excluded_rows(c, e)
    live = pay[~pay["_row"].isin(excluded)]
    res = res_status.drop_duplicates("reservation_id", keep="last").set_index("reservation_id")
    known = live["reservation_id"].isin(res.index)
    c.add(e, live[~known], "PAYMENT_UNKNOWN_RESERVATION", lambda r: f"Бронирование {r['reservation_id']} не найдено")

    lk = live[known]
    r_prop, r_status, r_dq = (lk["reservation_id"].map(res[k]) for k in ("property_id", "status", "dq_status"))
    c.add(e, lk[lk["property_id"] != r_prop], "PAYMENT_PROPERTY_MISMATCH",
          lambda r: f"Платёж на {r['property_id']}, бронь на {res.at[r['reservation_id'], 'property_id']}",
          lambda r: r["reservation_id"])
    c.add(e, lk[r_dq == "excluded"], "PAYMENT_FOR_EXCLUDED_RES",
          lambda r: f"Бронирование {r['reservation_id']} исключено ({res.at[r['reservation_id'], 'dq_rules']}); "
                    f"деньги учтены в cash, но не сверяются с начислением",
          lambda r: r["reservation_id"])
    c.add(e, lk[(r_status == "cancelled") & (lk["amount"] > 0)], "PAYMENT_ON_CANCELLED",
          "Получены деньги по отменённой брони — проверить, был ли возврат гостю", lambda r: r["reservation_id"])


def _check_expenses(c: _Collector, ds: Dataset) -> None:
    exp, e = ds.expenses, "expenses"
    _check_duplicates(c, e, exp)
    _check_common(c, e, exp, ds.properties, ["expense_date"], ["amount"])
    c.add(e, exp[~exp["payment_status"].isin({"paid", "pending"})], "EXPENSE_UNKNOWN_STATUS",
          lambda r: f"payment_status = '{r['payment_status']}'")
    key = ["property_id", "expense_date", "category", "amount"]
    live = exp[~exp["_row"].isin(_excluded_rows(c, e))]
    sus = live[live.duplicated(key, keep=False) & ~live.duplicated(_business_cols(live), keep=False)]
    c.add(e, sus, "EXPENSE_SUSPECT_DUP", "Совпадают объект/дата/категория/сумма при разных ID")


def _check_systemic(c: _Collector, ds: Dataset) -> None:
    """Паттерн «платежи есть только у чётных ID» — признак неполной выгрузки, а не неплатежей."""
    res_ids = ds.reservations["reservation_id"].drop_duplicates()
    paid = set(ds.payments["reservation_id"])
    num = pd.to_numeric(res_ids.str.extract(r"(\d+)$")[0], errors="coerce")
    unpaid = res_ids[~res_ids.isin(paid)]
    if len(unpaid) == 0:
        return
    unpaid_num = num[~res_ids.isin(paid)]
    paid_num = num[res_ids.isin(paid)]
    if (unpaid_num % 2 == 1).all() and (paid_num % 2 == 0).all():
        msg = (f"Платежи есть только у бронирований с чётным номером; у всех {len(unpaid)} нечётных — нет. "
               f"Такой паттерн похож на неполную выгрузку payments, а не на реальные неплатежи")
    else:
        msg = f"У {len(unpaid)} бронирований нет ни одного платежа"
    c.rows.append({"entity": "dataset", "record_id": "payments.csv", "source": "payments.csv", "row": None,
                   "property_id": "", "rule": "SYS_PAYMENT_COVERAGE", "severity": INFO, "message": msg,
                   "related_id": "", "amount_at_risk": 0.0})


def _status(df: pd.DataFrame, issues: pd.DataFrame, entity: str) -> pd.DataFrame:
    df = df.copy()
    own = issues[issues["entity"] == entity]
    by_row = own.groupby("row")
    sev = by_row["severity"].agg(set)
    rules = by_row["rule"].agg(lambda s: ", ".join(sorted(set(s))))

    def status(row):
        s = sev.get(row, set())
        return "excluded" if EXCLUDE in s else "review" if REVIEW in s else "ok"

    df["dq_status"] = df["_row"].map(status)
    df["dq_rules"] = df["_row"].map(rules).fillna("")
    return df


def run_checks(ds: Dataset) -> QualityResult:
    c = _Collector()
    _check_rules(c, ds)
    _check_reservations(c, ds)
    res = _status(ds.reservations, c.frame(), "reservations")
    _check_payments(c, ds, res)
    _check_expenses(c, ds)
    _check_systemic(c, ds)

    issues = c.frame()
    data = Dataset(
        properties=ds.properties.copy(),
        reservations=_status(ds.reservations, issues, "reservations"),
        payments=_status(ds.payments, issues, "payments"),
        expenses=_status(ds.expenses, issues, "expenses"),
        commission_rules=_status(ds.commission_rules, issues, "commission_rules"),
    )
    order = {EXCLUDE: 0, REVIEW: 1, INFO: 2}
    issues = issues.sort_values(["severity", "entity", "row"], key=lambda s: s.map(order) if s.name == "severity" else s)
    return QualityResult(issues=issues.reset_index(drop=True), data=data)


def summary(issues: pd.DataFrame) -> pd.DataFrame:
    """Сводка: правило → решение → число записей и сумма под риском."""
    if issues.empty:
        return issues
    s = (issues.groupby(["severity", "entity", "rule"], as_index=False)
         .agg(records=("record_id", "count"), amount_at_risk=("amount_at_risk", "sum"),
              examples=("record_id", lambda x: ", ".join(list(dict.fromkeys(x))[:6]))))
    s["description"] = s["rule"].map(lambda r: RULES[r][2])
    order = {EXCLUDE: 0, REVIEW: 1, INFO: 2}
    return s.sort_values(["severity", "records"], key=lambda col: col.map(order) if col.name == "severity" else -col)
