from datetime import date

from app import insights


def monthly(values, start=date(2015, 1, 1)):
    return [(insights._add_months(start, i), v) for i, v in enumerate(values)]


def test_poisson_and_chi2_helpers():
    assert insights.poisson_sf(0, 3) == 1.0
    assert abs(insights.poisson_sf(1, 2) - (1 - 0.1353352832)) < 1e-6
    assert insights.chi2_sf(0, 11) == 1.0
    assert 0.04 < insights.chi2_sf(19.675, 11) < 0.06  # 95th percentile of chi2(11)


def test_bh_adjust_is_monotonic_and_bounded():
    q = insights.bh_adjust([0.01, 0.04, 0.03, 0.2])
    assert [round(v, 4) for v in q] == [0.04, 0.0533, 0.0533, 0.2]


def test_seasonality_detects_a_real_tick_season():
    summer_heavy = [5, 5, 8, 15, 30, 40, 45, 40, 20, 10, 5, 5]
    result = insights.seasonality_test(summer_heavy)
    assert result["significant"] is True
    assert result["peak_months"][0] == "Jul"

    flat = insights.seasonality_test([20] * 12)
    assert flat["significant"] is False


def test_yearly_trend_detects_growth():
    rising = insights.yearly_trend({2015 + i: 50 + 10 * i for i in range(8)})
    assert rising["direction"] == "increasing"
    flat = insights.yearly_trend({2015 + i: 60 + (i % 2) for i in range(8)})
    assert flat["direction"] == "stable"


def test_forecast_prefers_seasonal_model_on_seasonal_data():
    season = [5, 5, 8, 15, 30, 40, 45, 40, 20, 10, 5, 5]
    result = insights.forecast(monthly(season * 5), horizon=12)
    assert result["model"] in {"seasonal_average", "trend_x_seasonal"}
    july = next(p for p in result["points"] if p["period"].endswith("-07-01"))
    assert july["predicted"] > 35


def test_forecast_needs_enough_history():
    assert insights.forecast(monthly([3] * 20), horizon=3)["available"] is False


def test_hotspot_flags_city_with_recent_surge():
    calm = monthly([2] * 48)
    surge = monthly([2] * 36 + [8] * 12)
    result = {r["city"]: r for r in insights.hotspots({"Calm": calm, "Surge": surge}, window_months=12)}
    assert result["Surge"]["risk_level"] == "high"
    assert result["Calm"]["risk_level"] == "normal"


def test_anomaly_detection_finds_spike():
    series = monthly([5] * 36)
    series[20] = (series[20][0], 25)
    flagged = insights.anomalies(series)["anomalies"]
    assert [a["period"] for a in flagged] == [series[20][0].isoformat()]
    assert flagged[0]["significant_after_correction"] is True
