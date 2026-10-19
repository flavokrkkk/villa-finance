"""Smoke-тест дашборда: страница строится без исключений при разных фильтрах."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.mark.parametrize("period, attribution, review", [
    ("Весь период данных", "stay_night", True),
    ("Октябрь 2026", "check_out", False),
])
def test_dashboard_renders(period, attribution, review):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    at.sidebar.selectbox[0].select(period)
    at.sidebar.radio[0].set_value(attribution)
    at.sidebar.checkbox[0].set_value(review)
    at.sidebar.multiselect[1].select("V003")
    at.run()
    assert not at.exception
    assert any("Owner balance" in m.label for m in at.metric)
