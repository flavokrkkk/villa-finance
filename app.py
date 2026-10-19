"""Dashboard: streamlit run app.py"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from villa_finance import finance as fin
from villa_finance.load import DATA_DIR, FILES, load_dataset
from villa_finance.quality import EXCLUDE, INFO, REVIEW, run_checks, summary

st.set_page_config(page_title="Villa Finance — прототип", layout="wide")


@st.cache_data
def _load(_stamp: tuple):
    return run_checks(load_dataset())


def _files_stamp() -> tuple:
    return tuple((DATA_DIR / f).stat().st_mtime for f in FILES.values())


def money(x: float) -> str:
    if abs(x) >= 1e9:
        return f"{x / 1e9:,.2f} млрд"
    if abs(x) >= 1e6:
        return f"{x / 1e6:,.1f} млн"
    return f"{x:,.0f}"


def fmt_table(df: pd.DataFrame, pct: tuple = (), dates: tuple = ()):
    num = [c for c in df.select_dtypes("number").columns if c not in pct]
    fmt = {c: "{:,.0f}" for c in num} | {c: "{:.0%}" for c in pct}
    fmt |= {c: (lambda v: "" if pd.isna(v) else f"{v:%Y-%m-%d}") for c in dates}
    return df.style.format(fmt, na_rep="")


q = _load(_files_stamp())
data = q.data
props = data.properties

# ---------------------------------------------------------------- фильтры
st.sidebar.header("Фильтры")
min_d = data.reservations["check_in"].min().date()
max_d = (data.reservations["check_out"].max() - pd.Timedelta(days=1)).date()
preset = st.sidebar.selectbox("Период", ["Весь период данных", "Сентябрь 2026", "Октябрь 2026", "Произвольный"])
if preset == "Сентябрь 2026":
    start, end = pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-30")
elif preset == "Октябрь 2026":
    start, end = pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-31")
elif preset == "Произвольный":
    rng = st.sidebar.date_input("С — по (включительно)", (min_d, max_d))
    start, end = (pd.Timestamp(rng[0]), pd.Timestamp(rng[-1]))
else:
    start, end = pd.Timestamp(min_d), pd.Timestamp(max_d)

clusters = st.sidebar.multiselect("Кластер", sorted(props["cluster"].unique()))
pool = props[props["cluster"].isin(clusters)] if clusters else props
chosen = st.sidebar.multiselect("Объект", pool["property_id"].tolist(),
                                format_func=lambda v: f"{v} · {props.set_index('property_id').at[v, 'property_name']}")
selected = chosen or pool["property_id"].tolist()

attribution = st.sidebar.radio("Признание выручки", list(fin.ATTRIBUTIONS), format_func=fin.ATTRIBUTIONS.get)
include_review = st.sidebar.checkbox("Учитывать записи «на проверке»", value=True,
                                     help="Выкл. — консервативный сценарий: только записи без замечаний")
as_of = st.sidebar.date_input("Дата оценки поступлений (as-of)", data.payments["payment_date"].max().date(),
                              help="Деньги по броням с выездом до этой даты считаются ожидаемыми")
grace = st.sidebar.number_input("Отсрочка платежа после выезда, дней", 0, 60, 0)

p = fin.Params(start=start, end=end, properties=selected, attribution=attribution,
               include_review=include_review, as_of=pd.Timestamp(as_of), grace_days=int(grace))

prop = fin.by_property(q, p)
tot = fin.totals(prop, p)
recon = fin.reconciliation(q, p)

st.title("Портфель вилл — финансовый прототип")
st.caption(f"Период {start:%Y-%m-%d} — {end:%Y-%m-%d} · объектов: {len(selected)} · валюта: IDR · "
           f"признание: {fin.ATTRIBUTIONS[attribution].lower()}")

n_ex = (data.reservations["dq_status"] == "excluded").sum()
n_rv = (data.reservations["dq_status"] == "review").sum()
review_share = tot["review_gross"] / tot["gross_bookings"] if tot["gross_bookings"] else 0
st.warning(
    f"**Owner balance — оценочный.** Исключено бронирований: {n_ex}, на проверке: {n_rv}. "
    + (f"{review_share:.0%} gross bookings периода приходится на записи «на проверке» (в основном "
       f"пересечения дат на одном объекте)." if include_review else "Записи «на проверке» не учтены.")
    + " Нет данных о налогах, выплатах собственникам и условиях договоров — см. README.",
)

tab_over, tab_cash, tab_dq, tab_res, tab_method = st.tabs(
    ["Обзор", "Начисления vs деньги", "Качество данных", "Бронирования", "Методология"])

# ---------------------------------------------------------------- обзор
with tab_over:
    c = st.columns(5)
    c[0].metric("Gross bookings", money(tot["gross_bookings"]),
                help="Стоимость активных (не отменённых) бронирований, признанная в периоде")
    c[1].metric("OTA fees", money(tot["ota_fees"]))
    c[2].metric("Management fees", money(tot["mgmt_fees"]), help="Ставка × (gross − refund), см. commission_rules")
    c[3].metric("Операционные расходы", money(tot["opex"]), help="paid + pending по дате расхода")
    c[4].metric("Owner balance (оценка)", money(tot["owner_balance_est"]))
    c = st.columns(5)
    c[0].metric("Ожидается к получению", money(tot["expected_cash"]), help="net revenue − OTA fees по начислению")
    c[1].metric("Получено (cash)", money(tot["cash_received"]), help="Платежи по дате платежа в периоде; не зависят от переключателя «на проверке» — деньги это факт")
    c[2].metric("Разница начисления − cash", money(tot["accrual_vs_cash_gap"]))
    c[3].metric("Загрузка", f"{tot['occupancy']:.0%}", help=">100% по объекту = пересекающиеся брони")
    c[4].metric("Отмены (gross)", money(tot["cancelled_gross"]), help="Справочно; в выручку не входят")

    wf = pd.DataFrame({
        "step": ["Gross bookings", "Возвраты", "Удержано при отмене", "OTA fees", "Management fees", "Opex",
                 "Owner balance"],
        "amount": [tot["gross_bookings"], -tot["refunds"], tot["cancel_retained"], -tot["ota_fees"],
                   -tot["mgmt_fees"], -tot["opex"], tot["owner_balance_est"]],
    })
    wf = wf[(wf["amount"] != 0) | wf["step"].isin(["Gross bookings", "Owner balance"])].reset_index(drop=True)
    wf["end"] = wf["amount"].cumsum()
    wf.loc[wf.index[-1], "end"] = wf["amount"].iloc[-1]
    wf["start"] = wf["end"] - wf["amount"]
    wf.loc[wf.index[-1], "start"] = 0
    wf["kind"] = ["итог" if s in ("Gross bookings", "Owner balance") else ("плюс" if a > 0 else "минус")
                  for s, a in zip(wf["step"], wf["amount"])]
    wf["label"] = wf["amount"].map(money)
    chart = alt.Chart(wf).mark_bar().encode(
        x=alt.X("step:N", sort=None, title=None),
        y=alt.Y("start:Q", title="IDR"), y2="end:Q",
        color=alt.Color("kind:N", scale=alt.Scale(domain=["итог", "плюс", "минус"],
                                                  range=["#4c78a8", "#54a24b", "#e45756"]), legend=None),
        tooltip=["step", "label"],
    )
    left, right = st.columns([1, 1])
    left.subheader("От gross bookings к owner balance")
    left.altair_chart(chart + chart.mark_text(dy=-8).encode(text="label:N", y="end:Q"), width="stretch")

    right.subheader("Owner balance по объектам")
    right.altair_chart(alt.Chart(prop).mark_bar().encode(
        x=alt.X("owner_balance_est:Q", title="IDR"), y=alt.Y("property_id:N", sort="-x", title=None),
        color=alt.condition(alt.datum.double_booked_nights > 0, alt.value("#f58518"), alt.value("#4c78a8")),
        tooltip=["property_name", alt.Tooltip("owner_balance_est:Q", format=",.0f"), "double_booked_nights"],
    ), width="stretch")
    right.caption("Оранжевым — объекты с пересекающимися бронированиями в периоде.")

    st.subheader("По объектам")
    cols = ["property_id", "property_name", "cluster", "owner_id", "nights_sold", "double_booked_nights", "occupancy",
            "gross_bookings", "refunds", "net_revenue", "ota_fees", "mgmt_fees", "opex", "owner_balance_est",
            "expected_cash", "cash_received", "accrual_vs_cash_gap", "review_gross"]
    st.dataframe(fmt_table(prop[cols], pct=("occupancy",)), width="stretch", hide_index=True)
    st.download_button("Скачать CSV", prop[cols].to_csv(index=False).encode("utf-8-sig"), "by_property.csv")

# ---------------------------------------------------------------- деньги
with tab_cash:
    st.markdown(
        "Начисление и деньги — разные оси. **Ожидается к получению** = net revenue − OTA fees "
        "(канал перечисляет сумму за вычетом своей комиссии). Сверка ниже — по бронированиям, у которых "
        "есть ночи в периоде, на полную сумму брони, против всех полученных по ней платежей.")
    g = recon.groupby("recon_status").agg(брони=("reservation_id", "count"), ожидается=("expected_cash", "sum"),
                                          получено=("received", "sum"), разница=("diff", "sum")).reset_index()
    st.dataframe(fmt_table(g), width="stretch", hide_index=True)
    status_f = st.multiselect("Статус сверки", sorted(recon["recon_status"].unique()))
    view = recon[recon["recon_status"].isin(status_f)] if status_f else recon
    st.dataframe(fmt_table(view, dates=("check_in", "check_out", "last_payment")), width="stretch",
                 hide_index=True)
    st.subheader("Платежи вне сверки")
    st.caption("Деньги пришли, но не сопоставляются с учитываемым бронированием (или сами исключены).")
    st.dataframe(fmt_table(fin.unmatched_payments(q, p), dates=("payment_date",)), width="stretch",
                 hide_index=True)

# ---------------------------------------------------------------- качество
with tab_dq:
    iss = q.issues
    c = st.columns(3)
    c[0].metric("Исключено (exclude)", (iss["severity"] == EXCLUDE).sum())
    c[1].metric("На проверку (review)", (iss["severity"] == REVIEW).sum())
    c[2].metric("Наблюдения (info)", (iss["severity"] == INFO).sum())
    st.subheader("Сводка по правилам")
    st.dataframe(fmt_table(summary(iss)), width="stretch", hide_index=True)
    st.subheader("Журнал замечаний")
    sev = st.multiselect("Решение", [EXCLUDE, REVIEW, INFO], default=[EXCLUDE, REVIEW])
    st.dataframe(fmt_table(iss[iss["severity"].isin(sev)]), width="stretch", hide_index=True)
    st.download_button("Скачать журнал CSV", iss.to_csv(index=False).encode("utf-8-sig"), "dq_issues.csv")

# ---------------------------------------------------------------- брони
with tab_res:
    facts = fin.reservation_facts(q, fin.Params(start=start, end=end, properties=selected, include_review=True))
    facts = facts[(facts["check_in"] <= end) & (facts["check_out"] > start)]
    st.caption("Полные суммы по брони (без обрезки периодом). Исключённые записи — отдельной таблицей ниже.")
    st.dataframe(fmt_table(facts[["reservation_id", "property_id", "status", "channel", "check_in", "check_out",
                                  "nights", "gross_booking_value", "ota_fees", "net_revenue", "mgmt_fees",
                                  "owner_share", "expected_cash", "dq_status", "dq_rules"]],
                           dates=("check_in", "check_out")), width="stretch", hide_index=True)
    st.subheader("Исключены из расчёта")
    st.dataframe(fmt_table(data.reservations[data.reservations["dq_status"] == "excluded"]
                           .drop(columns=["_source"]), dates=("check_in", "check_out", "updated_at")),
                 width="stretch", hide_index=True)

# ---------------------------------------------------------------- методология
with tab_method:
    st.markdown("""
| Показатель | Формула |
|---|---|
| Gross bookings | Σ gross_booking_value активных броней (confirmed, modified) × доля ночей в периоде |
| Net revenue | gross − частичные возвраты + удержанное при отмене (gross − refund по отменам) |
| OTA fees | Σ ota_fee активных броней × доля ночей; по отменам = 0 (платёж по ним 0) |
| Management fee | ставка, действующая на дату ночи × (gross − refund) — база `gross_booking_value`, OTA не вычитается |
| Opex | Σ expenses по expense_date в периоде, статусы paid + pending (начисление) |
| **Owner balance (оценка)** | net revenue − OTA fees − management fee − opex |
| Ожидается к получению | net revenue − OTA fees |
| Получено | Σ payments.amount по payment_date в периоде |

Подробно — в README.md: правила отмен, дат, комиссий, допущения и вопросы к финансовому директору.
""")
