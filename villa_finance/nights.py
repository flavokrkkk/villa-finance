"""Разворот бронирования в ночи и привязка ставки комиссии к каждой ночи."""
from __future__ import annotations

import pandas as pd


def expand_nights(res: pd.DataFrame) -> pd.DataFrame:
    """Одна строка на каждую ночь проживания: [check_in, check_out).

    Ночь датируется днём заезда в эту ночь; день выезда ночью не является.
    Бронирования с некорректными датами сюда не попадают (их отсекает DQ).
    """
    ok = res[res["check_in"].notna() & res["check_out"].notna() & (res["check_out"] > res["check_in"])]
    if ok.empty:
        return ok.assign(night=pd.Series(dtype="datetime64[ns]"), nights=pd.Series(dtype=int))
    out = ok.assign(
        night=[list(pd.date_range(a, b - pd.Timedelta(days=1), freq="D")) for a, b in zip(ok["check_in"], ok["check_out"])],
        nights=(ok["check_out"] - ok["check_in"]).dt.days,
    ).explode("night")
    out["night"] = pd.to_datetime(out["night"])
    return out.reset_index(drop=True)


def attach_rates(nightly: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    """Ставка определяется правилом, действующим на дату ночи (а не на дату брони).

    Если правил на ночь несколько — это ошибка справочника (её ловит DQ),
    здесь берём правило с самой поздней effective_from, чтобы не задвоить ночь.
    """
    r = rules[["property_id", "effective_from", "effective_to", "management_fee_rate", "fee_basis"]]
    m = nightly.reset_index().merge(r, on="property_id", how="left")
    hit = (m["night"] >= m["effective_from"]) & (m["night"] <= m["effective_to"])
    m = m[hit].sort_values("effective_from").drop_duplicates("index", keep="last")
    out = nightly.join(m.set_index("index")[["management_fee_rate", "fee_basis"]])
    return out
