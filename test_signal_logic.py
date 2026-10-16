import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lucas import allowed_by_cooldown, forecast_direction, primary_candidate


def test_primary_long_requires_threshold_and_positive_terminal_close():
    forecast = pd.DataFrame({
        "high": [101.0, 102.5, 102.2],
        "low": [99.5, 100.4, 100.8],
        "close": [100.5, 101.8, 101.4],
    })
    direction, excursion, terminal = primary_candidate(forecast, 100.0, 2.0, 0.05)
    assert direction == "LONG"
    assert excursion >= 2.0
    assert terminal > 0


def test_primary_short_requires_threshold_and_negative_terminal_close():
    forecast = pd.DataFrame({
        "high": [100.2, 99.8, 99.6],
        "low": [99.0, 97.7, 97.5],
        "close": [99.5, 98.2, 98.6],
    })
    direction, excursion, terminal = primary_candidate(forecast, 100.0, 2.0, 0.05)
    assert direction == "SHORT"
    assert excursion >= 2.0
    assert terminal < 0


def test_no_signal_if_price_never_reaches_two_percent():
    forecast = pd.DataFrame({
        "high": [100.4, 101.0, 101.5],
        "low": [99.5, 99.8, 100.1],
        "close": [100.1, 100.5, 100.8],
    })
    direction, _, _ = primary_candidate(forecast, 100.0, 2.0, 0.05)
    assert direction is None


def test_neutral_confirmation_is_not_directional():
    forecast = pd.DataFrame({"close": [100.01, 100.02]})
    assert forecast_direction(forecast, 100.0, 0.05) == 0


def test_opposite_confirming_direction_can_be_identified():
    forecast = pd.DataFrame({"close": [99.8, 99.5]})
    assert forecast_direction(forecast, 100.0, 0.05) == -1


def test_new_direction_is_not_blocked_by_same_direction_cooldown():
    state = {"alerts": {"BTCUSDT|INTRADAY": {"direction": "LONG", "timestamp": pd.Timestamp.now(tz="UTC").isoformat()}}}
    signal = {"symbol": "BTCUSDT", "category": "INTRADAY", "direction": "SHORT"}
    assert allowed_by_cooldown(signal, state, 4.0) is True
