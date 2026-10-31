"""Statistical / ML insights (extension task), implemented in pure Python.

The point of this module is not just to produce numbers but to say how much
they can be trusted. Each insight comes with a significance test or a
back-test, so a front end can say "tick activity is rising" only when the data
actually supports it.

  * forecast    - picks the best of three models by back-testing on the most
                  recent 12 months, then forecasts with a prediction interval.
  * seasonality - chi-square test of whether sightings really cluster by month.
  * trend       - least-squares slope of yearly counts with a significance test.
  * hotspots    - cities whose recent activity is above their own baseline
                  (Poisson test), i.e. an outbreak-style early warning.
  * anomalies   - months with surprisingly many sightings versus a robust
                  (median) baseline for that time of year.
"""

import math
import statistics
from datetime import date

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# -- small stats helpers -------------------------------------------------------


def _normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def chi2_sf(x: float, k: int) -> float:
    """Upper-tail p-value of chi-square (Wilson-Hilferty approximation; good for k >= 3)."""
    if x <= 0:
        return 1.0
    h = 2 / (9 * k)
    z = ((x / k) ** (1 / 3) - (1 - h)) / math.sqrt(h)
    return _normal_sf(z)


def poisson_sf(observed: int, expected: float) -> float:
    """P(X >= observed) for X ~ Poisson(expected)."""
    if observed <= 0:
        return 1.0
    if expected <= 0:
        return 0.0
    term = math.exp(-expected)
    cdf = term
    for k in range(1, observed):
        term *= expected / k
        cdf += term
    return max(0.0, 1.0 - cdf)


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Return (slope, intercept, standard error of slope)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    intercept = my - slope * mx
    if n <= 2 or not sxx:
        return slope, intercept, float("inf")
    resid = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    return slope, intercept, math.sqrt(resid / (n - 2) / sxx)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


# -- forecasting -----------------------------------------------------------------


def _seasonal_index(series: list[tuple[date, int]]) -> dict[int, float]:
    overall = statistics.fmean(c for _, c in series) or 1.0
    by_month: dict[int, list[int]] = {}
    for d, c in series:
        by_month.setdefault(d.month, []).append(c)
    return {m: (statistics.fmean(v) / overall if v else 1.0) for m, v in by_month.items()}


def _model_mean(train, future_dates):
    mean = statistics.fmean(c for _, c in train)
    return [mean for _ in future_dates]


def _model_seasonal_naive(train, future_dates):
    """Average of the same calendar month across all training years."""
    by_month: dict[int, list[int]] = {}
    for d, c in train:
        by_month.setdefault(d.month, []).append(c)
    fallback = statistics.fmean(c for _, c in train)
    return [statistics.fmean(by_month[d.month]) if d.month in by_month else fallback for d in future_dates]


def _model_trend_seasonal(train, future_dates):
    """Linear trend on de-seasonalised counts, multiplied back by the seasonal index."""
    index = _seasonal_index(train)
    ys = [c / index[d.month] if index.get(d.month) else c for d, c in train]
    slope, intercept, _ = linear_fit(list(range(len(ys))), ys)
    start = train[0][0]
    out = []
    for d in future_dates:
        t = (d.year - start.year) * 12 + d.month - start.month
        out.append(max(0.0, (intercept + slope * t) * index.get(d.month, 1.0)))
    return out


MODELS = {
    "historical_mean": _model_mean,
    "seasonal_average": _model_seasonal_naive,
    "trend_x_seasonal": _model_trend_seasonal,
}


def forecast(series: list[tuple[date, int]], horizon: int, holdout: int = 12) -> dict:
    if len(series) < holdout + 24:
        return {"available": False, "reason": f"need at least {holdout + 24} months of history, have {len(series)}"}

    train, test = series[:-holdout], series[-holdout:]
    test_dates = [d for d, _ in test]
    backtest = {}
    for name, model in MODELS.items():
        predicted = model(train, test_dates)
        backtest[name] = round(statistics.fmean(abs(p - c) for p, (_, c) in zip(predicted, test)), 3)
    best = min(backtest, key=backtest.get)

    # Refit the winning model on all history for the real forecast.
    fitted = MODELS[best](series, [d for d, _ in series])
    residual_sd = statistics.pstdev(c - f for (_, c), f in zip(series, fitted))
    last = series[-1][0]
    future = [_add_months(last, h) for h in range(1, horizon + 1)]
    points = [
        {
            "period": d.isoformat(),
            "predicted": round(p, 2),
            "lower_95": round(max(0.0, p - 1.96 * residual_sd), 2),
            "upper_95": round(p + 1.96 * residual_sd, 2),
        }
        for d, p in zip(future, MODELS[best](series, future))
    ]
    return {
        "available": True,
        "model": best,
        "backtest_mae_last_12_months": backtest,
        "history_months": len(series),
        "points": points,
        "note": "Model chosen by lowest mean absolute error on the most recent 12 months held out from training.",
    }


# -- descriptive tests -------------------------------------------------------------


def seasonality_test(month_counts: list[int], alpha: float = 0.05) -> dict:
    total = sum(month_counts)
    if total < 60:
        return {"significant": None, "reason": "not enough sightings to test seasonality"}
    expected = total / 12
    chi2 = sum((c - expected) ** 2 / expected for c in month_counts)
    p = chi2_sf(chi2, 11)
    ranked = sorted(range(12), key=lambda i: month_counts[i], reverse=True)
    return {
        "chi_square": round(chi2, 3),
        "p_value": round(p, 4),
        "significant": p < alpha,
        "peak_months": [MONTHS[i] for i in ranked[:3]],
        "quietest_months": [MONTHS[i] for i in ranked[-3:]],
        "interpretation": (
            "Sightings cluster in particular months - plan awareness campaigns around the peak months."
            if p < alpha
            else "No statistically significant seasonal pattern: sightings are spread fairly evenly across the year."
        ),
    }


def yearly_trend(year_counts: dict[int, int], alpha: float = 0.05) -> dict:
    """Caller should pass complete years only, so a part-year doesn't look like a decline."""
    years = sorted(year_counts)
    if len(years) < 4:
        return {"direction": None, "reason": "need at least 4 years of data"}
    xs, ys = [float(y) for y in years], [float(year_counts[y]) for y in years]
    slope, intercept, se = linear_fit(xs, ys)
    if se == 0:  # perfect fit: any non-zero slope is as significant as it gets
        p = 0.0 if slope else 1.0
    else:
        p = 2 * _normal_sf(abs(slope / se)) if math.isfinite(se) else 1.0
    mean = statistics.fmean(ys)
    direction = "stable" if p >= alpha else ("increasing" if slope > 0 else "decreasing")
    return {
        "slope_per_year": round(slope, 3),
        "percent_change_per_year": round(100 * slope / mean, 2) if mean else None,
        "p_value": round(p, 4),
        "direction": direction,
        "years": [{"year": y, "count": year_counts[y]} for y in years],
    }


def bh_adjust(p_values: list[float]) -> list[float]:
    """Benjamini-Hochberg q-values: controls the false discovery rate when we
    test many cities or months at once (otherwise ~5% of them look 'significant'
    by pure chance)."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, p_values[i] * m / rank)
        q[i] = running
    return q


def hotspots(city_monthly: dict[str, list[tuple[date, int]]], window_months: int, alpha: float = 0.05) -> list[dict]:
    """Compare each city's last `window_months` with its own earlier monthly rate."""
    results = []
    for city, series in city_monthly.items():
        if len(series) <= window_months + 6:
            continue
        baseline, recent = series[:-window_months], series[-window_months:]
        expected = statistics.fmean(c for _, c in baseline) * window_months
        observed = sum(c for _, c in recent)
        results.append({
            "city": city,
            "recent_sightings": observed,
            "expected_sightings": round(expected, 2),
            "ratio_to_baseline": round(observed / expected, 3) if expected else None,
            "p_value": poisson_sf(observed, expected),
            "window": {"from": recent[0][0].isoformat(), "to": recent[-1][0].isoformat()},
        })
    for r, q in zip(results, bh_adjust([r["p_value"] for r in results])):
        r["q_value"] = round(q, 4)
        r["p_value"] = round(r["p_value"], 4)
        if q < alpha:
            r["risk_level"] = "high"
        elif r["p_value"] < alpha:
            r["risk_level"] = "watch"  # unusual on its own, but could be chance across this many cities
        else:
            r["risk_level"] = "normal"
    return sorted(results, key=lambda r: (r["p_value"], -(r["ratio_to_baseline"] or 0)))


def anomalies(series: list[tuple[date, int]], alpha: float = 0.01) -> dict:
    """Months with significantly more sightings than expected for that time of year."""
    if len(series) < 24:
        return {"available": False, "reason": "need at least 24 months of history"}
    # Median, not mean: a spike must not inflate its own baseline and hide itself.
    by_month: dict[int, list[int]] = {}
    for d, c in series:
        by_month.setdefault(d.month, []).append(c)
    baseline = {m: statistics.median(v) for m, v in by_month.items()}
    expected = [baseline[d.month] for d, _ in series]
    p_values = [poisson_sf(c, e) for (_, c), e in zip(series, expected)]
    q_values = bh_adjust(p_values)
    flagged = [
        {"period": d.isoformat(), "count": c, "expected": round(e, 2), "p_value": round(p, 5),
         "q_value": round(q, 4), "significant_after_correction": q < alpha}
        for (d, c), e, p, q in zip(series, expected, p_values, q_values)
        if p < alpha
    ]
    return {
        "available": True,
        "alpha": alpha,
        "months_checked": len(series),
        "expected_false_alarms": round(alpha * len(series), 2),
        "anomalies": flagged,
    }
