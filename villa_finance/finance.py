"""Финансовые расчёты. Формулы и допущения описаны в README («Методология»).

Две независимые оси, которые сознательно не смешиваются:

* начисления (accrual) — по ночам проживания: что заработано за период;
* денежный поток (cash) — по дате платежа: что фактически пришло.

Сверка показывает разницу между ними на уровне каждого бронирования.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .nights import attach_rates, expand_nights
from .quality import QualityResult

ATTRIBUTIONS = {
    "stay_night": "По ночам проживания (pro-rata)",
    "check_in": "Целиком на дату заезда",
    "check_out": "Целиком на дату выезда",
}
MONEY_COLS = ["gross_bookings", "refunds", "cancel_retained", "net_revenue", "ota_fees", "mgmt_fees"]


@dataclass
class Params:
    start: pd.Timestamp
    end: pd.Timestamp  # включительно
    properties: list[str] | None = None  # None = все
    attribution: str = "stay_night"
    include_review: bool = True
    as_of: pd.Timestamp | None = None  # дата, на которую оцениваем «должно было прийти»
    grace_days: int = 0  # сколько дней после выезда ждём деньги, прежде чем считать их просроченными
    _statuses: tuple = field(init=False, default=())

    def __post_init__(self):
        self.start, self.end = pd.Timestamp(self.start), pd.Timestamp(self.end)
        self._statuses = ("ok", "review") if self.include_review else ("ok",)


def _usable(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    out = df[df["dq_status"].isin(p._statuses)]
    if p.properties is not None:
        out = out[out["property_id"].isin(p.properties)]
    return out


def _real_cash(pay: pd.DataFrame, p: Params) -> pd.DataFrame:
    out = pay[pay["dq_status"] != "excluded"]
    if p.properties is not None:
        out = out[out["property_id"].isin(p.properties)]
    return out


def nightly_ledger(res: pd.DataFrame, rules: pd.DataFrame, attribution: str = "stay_night") -> pd.DataFrame:
    """Одна строка = одна ночь бронирования с долей всех сумм.

    Ставка management fee берётся на дату ночи. Дата признания (`recognition_date`)
    зависит от метода атрибуции; суммы за бронирование при этом не меняются.
    """
    n = attach_rates(expand_nights(res), rules[rules["dq_status"] != "excluded"])
    share = 1.0 / n["nights"]
    active = n["status"] != "cancelled"
    n["gross_bookings"] = n["gross_booking_value"].where(active, 0.0) * share
    # refunds: для активных — частичный возврат; для отмен — возврат за вычетом удержанного
    n["refunds"] = n["refund_amount"].where(active, 0.0) * share
    # отмена с неполным возвратом: удержанная часть — доход (штраф за отмену)
    n["cancel_retained"] = (n["gross_booking_value"] - n["refund_amount"]).where(~active, 0.0) * share
    n["net_revenue"] = n["gross_bookings"] - n["refunds"] + n["cancel_retained"]
    # Отмена: комиссию OTA считаем несписанной — по отменам пришли нулевые платежи,
    # т.е. канал ничего не удержал (допущение A4 в README).
    n["ota_fees"] = n["ota_fee"].where(active, 0.0) * share
    n["cancelled_gross"] = n["gross_booking_value"].where(~active, 0.0) * share
    base = n["net_revenue"].where(n["fee_basis"] != "net_of_ota_fee", n["net_revenue"] - n["ota_fees"])
    n["mgmt_fees"] = n["management_fee_rate"].fillna(0.0) * base
    n["occupied"] = active.astype(int)
    n["recognition_date"] = {"stay_night": n["night"], "check_in": n["check_in"],
                             "check_out": n["check_out"]}[attribution]
    return n


def reservation_facts(q: QualityResult, p: Params) -> pd.DataFrame:
    """Полные (не урезанные периодом) суммы по каждому бронированию."""
    res = _usable(q.data.reservations, p)
    n = nightly_ledger(res, q.data.commission_rules, "stay_night")
    agg = n.groupby("_row")[MONEY_COLS].sum()
    out = res.set_index("_row").join(agg).reset_index()
    out[MONEY_COLS] = out[MONEY_COLS].fillna(0.0)
    out["nights"] = (out["check_out"] - out["check_in"]).dt.days
    # Сколько должно прийти от канала/гостя: всё, кроме комиссии OTA и возвратов.
    out["expected_cash"] = out["net_revenue"] - out["ota_fees"]
    out["owner_share"] = out["net_revenue"] - out["ota_fees"] - out["mgmt_fees"]
    return out


def _in_period(s: pd.Series, p: Params) -> pd.Series:
    return (s >= p.start) & (s <= p.end)


def by_property(q: QualityResult, p: Params) -> pd.DataFrame:
    """Витрина по объектам за период. Owner balance — оценка (см. README)."""
    props = q.data.properties
    if p.properties is not None:
        props = props[props["property_id"].isin(p.properties)]
    base = props[["property_id", "property_name", "cluster", "owner_id"]].set_index("property_id")

    res = _usable(q.data.reservations, p)
    led = nightly_ledger(res, q.data.commission_rules, p.attribution)
    led_p = led[_in_period(led["recognition_date"], p)]
    acc = led_p.groupby("property_id")[MONEY_COLS + ["cancelled_gross"]].sum()
    review_gross = led_p[led_p["dq_status"] == "review"].groupby("property_id")["gross_bookings"].sum()

    # Загрузка — всегда по физическим ночам, независимо от метода атрибуции денег.
    stay = nightly_ledger(res, q.data.commission_rules, "stay_night")
    stay = stay[_in_period(stay["night"], p) & (stay["occupied"] == 1)]
    nights_sold = stay.groupby("property_id").size()
    double_nights = (stay.groupby(["property_id", "night"]).size() - 1).clip(lower=0).groupby("property_id").sum()

    exp = _usable(q.data.expenses, p)
    exp = exp[_in_period(exp["expense_date"], p)]
    opex = exp.groupby("property_id")["amount"].sum()
    opex_paid = exp[exp["payment_status"] == "paid"].groupby("property_id")["amount"].sum()

    # Деньги — факт: переключатель «на проверке» влияет только на начисления.
    # В cash не попадают лишь исключённые платежи (дубли, неизвестный объект).
    pay = _real_cash(q.data.payments, p)
    pay = pay[_in_period(pay["payment_date"], p)]
    cash = pay.groupby("property_id")["amount"].sum()

    out = base.join(acc).fillna(0.0)
    out["opex"] = opex
    out["opex_paid"] = opex_paid
    out["cash_received"] = cash
    out["review_gross"] = review_gross
    out["nights_sold"] = nights_sold
    out["double_booked_nights"] = double_nights
    num_cols = MONEY_COLS + ["cancelled_gross", "opex", "opex_paid", "cash_received", "review_gross",
                             "nights_sold", "double_booked_nights"]
    out[num_cols] = out[num_cols].fillna(0.0)
    days = (p.end - p.start).days + 1
    out["occupancy"] = out["nights_sold"] / days
    out["owner_balance_est"] = out["net_revenue"] - out["ota_fees"] - out["mgmt_fees"] - out["opex"]
    out["expected_cash"] = out["net_revenue"] - out["ota_fees"]
    out["accrual_vs_cash_gap"] = out["expected_cash"] - out["cash_received"]
    return out.reset_index()


def totals(prop: pd.DataFrame, p: Params) -> pd.Series:
    t = prop.select_dtypes("number").sum()
    available = len(prop) * ((p.end - p.start).days + 1)
    t["occupancy"] = t["nights_sold"] / available if available else 0.0
    return t


def reconciliation(q: QualityResult, p: Params) -> pd.DataFrame:
    """Сверка «начислено к получению» ↔ «получено» по каждому бронированию.

    В выборку попадают брони, у которых хотя бы одна ночь в периоде. Суммы —
    полные по брони (деньги приходят за бронь целиком, а не за ночь).
    """
    facts = reservation_facts(q, p)
    nights_in = (facts["check_in"] <= p.end) & (facts["check_out"] > p.start)
    facts = facts[nights_in]

    pay = _real_cash(q.data.payments, p)
    received = pay.groupby("reservation_id").agg(received=("amount", "sum"),
                                                 payments=("payment_id", lambda s: ", ".join(s)),
                                                 last_payment=("payment_date", "max"))
    r = facts.join(received, on="reservation_id")
    r["received"] = r["received"].fillna(0.0)
    r["payments"] = r["payments"].fillna("")
    r["diff"] = r["received"] - r["expected_cash"]

    as_of = p.as_of if p.as_of is not None else q.data.payments["payment_date"].max()
    due = r["check_out"] + pd.Timedelta(days=p.grace_days) <= as_of

    def status(row, is_due):
        if abs(row["diff"]) < 1:
            return "отмена, денег нет" if row["status"] == "cancelled" else "сходится"
        if row["received"] == 0:
            return "не получено, срок прошёл" if is_due else "не получено, срок не наступил"
        return "недоплата" if row["diff"] < 0 else "переплата"

    r["recon_status"] = [status(row, d) for (_, row), d in zip(r.iterrows(), due)]
    cols = ["reservation_id", "property_id", "status", "channel", "check_in", "check_out", "dq_status", "dq_rules",
            "gross_booking_value", "refund_amount", "ota_fees", "expected_cash", "received", "diff",
            "recon_status", "payments", "last_payment"]
    return r[cols].sort_values(["recon_status", "reservation_id"]).reset_index(drop=True)


def unmatched_payments(q: QualityResult, p: Params) -> pd.DataFrame:
    """Деньги, которые пришли, но не сопоставляются с учитываемым бронированием."""
    pay = q.data.payments
    if p.properties is not None:
        pay = pay[pay["property_id"].isin(p.properties)]
    pay = pay[_in_period(pay["payment_date"], p)]
    usable_res = set(_usable(q.data.reservations, p)["reservation_id"])
    bad = pay[(pay["dq_status"] != "ok") | ~pay["reservation_id"].isin(usable_res)]
    return bad[["payment_id", "reservation_id", "property_id", "payment_date", "amount", "dq_status", "dq_rules"]]
