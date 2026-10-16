#!/usr/bin/env python3
"""Lucas v1.0: Kronos-base forecast-to-Telegram alert prototype.

This software produces research alerts only. It never places exchange orders.
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yaml

LOGGER = logging.getLogger("lucas")
INTERVALS = {
    "15m": pd.Timedelta(minutes=15),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
}


@dataclass
class ForecastResult:
    history: pd.DataFrame
    forecast: pd.DataFrame


class LucasError(RuntimeError):
    """Expected runtime error with a readable message."""


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    for key in ("project", "model", "market_data", "strategy", "alerts", "runtime"):
        if key not in config:
            raise LucasError(f"Missing required config section: {key}")
    if config["model"].get("model_id") != "NeoQuasar/Kronos-base":
        LOGGER.warning("Configured model is %s, not Kronos-base", config["model"].get("model_id"))
    return config


def load_state(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"version": 1, "alerts": {}, "swing_anchors": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("version", 1)
        data.setdefault("alerts", {})
        data.setdefault("swing_anchors", {})
        return data
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("State file could not be read; starting with fresh state: %s", exc)
        return {"version": 1, "alerts": {}, "swing_anchors": {}}


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix(p.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(p)


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _get_json_from_bases(config: dict[str, Any], path: str, params: dict[str, Any]) -> Any:
    errors: list[str] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Lucas-v1.0-market-alerts/1.0"})
    for base in config["market_data"]["base_urls"]:
        url = f"{base.rstrip('/')}{path}"
        try:
            response = session.get(
                url,
                params=params,
                timeout=int(config["market_data"].get("request_timeout_seconds", 25)),
            )
            if response.status_code == 429:
                errors.append(f"{base}: HTTP 429 rate limit")
                time.sleep(1.0)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{base}: {exc}")
    raise LucasError(f"Market-data request failed for {path}. " + " | ".join(errors))


def fetch_klines(config: dict[str, Any], symbol: str, interval: str) -> pd.DataFrame:
    if interval not in INTERVALS:
        raise LucasError(f"Unsupported interval: {interval}")
    raw = _get_json_from_bases(
        config,
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": int(config["market_data"].get("candles_to_request", 520))},
    )
    if not isinstance(raw, list) or len(raw) < 50:
        raise LucasError(f"Not enough candles returned for {symbol} {interval}")

    columns = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trade_count", "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    frame = pd.DataFrame(raw, columns=columns)
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["open_time"] = pd.to_numeric(frame["open_time"], errors="coerce")
    frame["close_time"] = pd.to_numeric(frame["close_time"], errors="coerce")

    # Never feed an incomplete candle to the model. Binance timestamps are UTC.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    frame = frame[frame["close_time"] <= now_ms].copy()
    frame = frame.dropna(subset=["open_time", "open", "high", "low", "close"])
    frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last")
    frame["timestamp"] = pd.to_datetime(frame["open_time"].astype("int64"), unit="ms", utc=True).dt.tz_localize(None)
    frame = frame.rename(columns={"quote_volume": "amount"})
    result = frame[["timestamp", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)
    if len(result) < 50:
        raise LucasError(f"Only {len(result)} closed candles for {symbol} {interval}")
    return result.tail(int(config["runtime"].get("lookback_candles", 512))).reset_index(drop=True)


def fetch_current_price(config: dict[str, Any], symbol: str) -> float:
    result = _get_json_from_bases(config, "/api/v3/ticker/price", {"symbol": symbol})
    price = float(result["price"])
    if not np.isfinite(price) or price <= 0:
        raise LucasError(f"Invalid current price for {symbol}: {price}")
    return price


def load_kronos_predictor(config: dict[str, Any]):
    repo_dir = Path(os.getenv("LUCAS_KRONOS_REPO", "/tmp/Kronos"))
    if not (repo_dir / "model").is_dir():
        raise LucasError(
            f"Official Kronos source was not found at {repo_dir}. "
            "The GitHub workflow must clone https://github.com/shiyu-coder/Kronos first."
        )
    sys.path.insert(0, str(repo_dir))
    try:
        import torch
        from model import Kronos, KronosPredictor, KronosTokenizer
    except Exception as exc:  # include useful package/import error in logs
        raise LucasError(f"Could not import Kronos. Check dependencies. Details: {exc}") from exc

    # Keep CPU use within standard GitHub-hosted runner limits.
    try:
        torch.set_num_threads(max(1, min(2, os.cpu_count() or 1)))
    except Exception:
        pass
    model_cfg = config["model"]
    LOGGER.info("Loading model %s on %s", model_cfg["model_id"], model_cfg.get("device", "cpu"))
    tokenizer = KronosTokenizer.from_pretrained(model_cfg["tokenizer_id"])
    model = Kronos.from_pretrained(model_cfg["model_id"])
    predictor = KronosPredictor(
        model,
        tokenizer,
        device=model_cfg.get("device", "cpu"),
        max_context=int(model_cfg.get("max_context", 512)),
    )
    return predictor


def predict(
    predictor,
    history: pd.DataFrame,
    interval: str,
    pred_len: int,
    model_config: dict[str, Any],
) -> ForecastResult:
    if interval not in INTERVALS:
        raise LucasError(f"Unsupported forecast interval {interval}")
    if len(history) < 50:
        raise LucasError(f"Too few historical candles for {interval}: {len(history)}")
    source = history.tail(512).copy().reset_index(drop=True)
    x_df = source[["open", "high", "low", "close", "volume", "amount"]].astype(float)
    x_timestamp = pd.Series(pd.to_datetime(source["timestamp"], errors="raise"))
    future_times = pd.Series(
        pd.date_range(
            start=x_timestamp.iloc[-1] + INTERVALS[interval],
            periods=int(pred_len),
            freq=INTERVALS[interval],
        )
    )
    predicted = predictor.predict(
        df=x_df,
        x_timestamp=x_timestamp,
        y_timestamp=future_times,
        pred_len=int(pred_len),
        T=float(model_config.get("temperature", 1.0)),
        top_p=float(model_config.get("top_p", 0.9)),
        sample_count=int(model_config.get("sample_count", 1)),
    )
    predicted = pd.DataFrame(predicted).reset_index(drop=True)
    required = {"open", "high", "low", "close"}
    if not required.issubset(predicted.columns):
        raise LucasError(f"Kronos output missing OHLC columns: {sorted(required - set(predicted.columns))}")
    predicted["timestamp"] = future_times.to_numpy()
    for col in ("open", "high", "low", "close"):
        predicted[col] = pd.to_numeric(predicted[col], errors="coerce")
    predicted = predicted.dropna(subset=["high", "low", "close"])
    if len(predicted) != int(pred_len):
        raise LucasError(f"Kronos returned {len(predicted)} valid rows; expected {pred_len}")
    return ForecastResult(history=source, forecast=predicted)


def calculate_atr(history: pd.DataFrame, period: int = 14) -> float:
    high = history["high"].astype(float)
    low = history["low"].astype(float)
    close = history["close"].astype(float)
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = true_range.rolling(window=period, min_periods=period).mean().dropna()
    if atr.empty or not np.isfinite(float(atr.iloc[-1])):
        raise LucasError("Could not calculate ATR from the available candles")
    return float(atr.iloc[-1])


def volatility_label(history: pd.DataFrame, atr_period: int = 14, history_bars: int = 100) -> tuple[str, float]:
    high = history["high"].astype(float)
    low = history["low"].astype(float)
    close = history["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_series = tr.rolling(atr_period, min_periods=atr_period).mean()
    atr_pct = (atr_series / close * 100.0).dropna()
    if atr_pct.empty:
        raise LucasError("Not enough data to classify volatility")
    current = float(atr_pct.iloc[-1])
    reference = atr_pct.tail(max(20, int(history_bars)))
    q33, q67 = reference.quantile([0.33, 0.67]).tolist()
    label = "LOW" if current <= q33 else ("MEDIUM" if current <= q67 else "HIGH")
    return label, current


def forecast_direction(forecast: pd.DataFrame, starting_close: float, deadband_pct: float) -> int:
    """Return +1 for up, -1 for down, 0 for neutral based on terminal close."""
    if starting_close <= 0 or forecast.empty:
        return 0
    move_pct = (float(forecast["close"].iloc[-1]) / starting_close - 1.0) * 100.0
    if move_pct >= deadband_pct:
        return 1
    if move_pct <= -deadband_pct:
        return -1
    return 0


def primary_candidate(
    forecast: pd.DataFrame,
    current_price: float,
    threshold_pct: float,
    deadband_pct: float,
) -> tuple[str | None, float, float]:
    """Use an intrahorizon extreme plus same-direction terminal close.

    Return (direction, maximum favourable excursion %, terminal projected return %).
    A projected high/low alone is not enough if the final forecast candle reverses
    to the other side of current price.
    """
    if forecast.empty or current_price <= 0:
        return None, 0.0, 0.0
    max_up = (float(forecast["high"].max()) / current_price - 1.0) * 100.0
    max_down = (float(forecast["low"].min()) / current_price - 1.0) * 100.0
    terminal = (float(forecast["close"].iloc[-1]) / current_price - 1.0) * 100.0
    if max_up >= threshold_pct and terminal >= deadband_pct:
        return "LONG", max_up, terminal
    if max_down <= -threshold_pct and terminal <= -deadband_pct:
        return "SHORT", abs(max_down), terminal
    return None, max(max_up, abs(max_down)), terminal


def find_target_time(forecast: pd.DataFrame, direction: str, entry: float, tp_pct: float, timezone_name: str) -> str:
    target = entry * (1.0 + tp_pct / 100.0) if direction == "LONG" else entry * (1.0 - tp_pct / 100.0)
    column = "high" if direction == "LONG" else "low"
    for _, row in forecast.iterrows():
        crossed = float(row[column]) >= target if direction == "LONG" else float(row[column]) <= target
        if crossed:
            ts = pd.Timestamp(row["timestamp"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            return ts.tz_convert(ZoneInfo(timezone_name)).strftime("%d %b %Y, %H:%M %Z")
    return "Not reached inside the forecast path"


def fmt_price(value: float) -> str:
    return f"${value:,.2f}" if value >= 1 else f"${value:.6f}"


def create_signal(
    *,
    config: dict[str, Any],
    symbol_info: dict[str, str],
    category: str,
    primary_result: ForecastResult,
    confirm_result: ForecastResult,
    current_price: float,
) -> dict[str, Any] | None:
    strat = config["strategy"]
    threshold = float(strat["forecast_move_threshold_pct"])
    deadband = float(strat["confirmation_deadband_pct"])
    direction, excursion_pct, terminal_pct = primary_candidate(
        primary_result.forecast, current_price, threshold, deadband
    )
    if direction is None:
        LOGGER.info("%s %s: no projected %.2f%% directional move", symbol_info["symbol"], category, threshold)
        return None

    confirmation_start = float(confirm_result.history["close"].iloc[-1])
    confirmation_dir = forecast_direction(confirm_result.forecast, confirmation_start, deadband)
    expected_confirmation = 1 if direction == "LONG" else -1
    if confirmation_dir != expected_confirmation:
        LOGGER.info(
            "%s %s: %s forecast conflicts with confirmation timeframe; suppressing signal",
            symbol_info["symbol"], category, direction,
        )
        return None

    atr = calculate_atr(primary_result.history, int(strat.get("atr_period", 14)))
    sl_distance = float(strat.get("stop_atr_multiple", 1.5)) * atr
    tp_pct = float(strat.get("take_profit_pct", 2.0))
    risk_pct = (sl_distance / current_price) * 100.0
    rr = tp_pct / risk_pct if risk_pct > 0 else 0.0
    minimum_rr = float(strat.get("minimum_reward_to_risk", 1.2))
    if rr < minimum_rr:
        LOGGER.info("%s %s: reward/risk %.2f below minimum %.2f; suppressing signal", symbol_info["symbol"], category, rr, minimum_rr)
        return None

    if direction == "LONG":
        tp = current_price * (1 + tp_pct / 100.0)
        sl = current_price - sl_distance
    else:
        tp = current_price * (1 - tp_pct / 100.0)
        sl = current_price + sl_distance

    gross_cost_pct = 2.0 * (
        float(strat.get("assumed_fee_pct_per_side", 0.10))
        + float(strat.get("assumed_slippage_pct_per_side", 0.05))
    )
    net_tp_pct = tp_pct - gross_cost_pct
    high_threshold = float(config["alerts"].get("high_confidence_extra_forecast_move_pct", 2.5))
    high_terminal = float(config["alerts"].get("high_confidence_min_terminal_move_pct", 1.0))
    confidence = "HIGH" if (excursion_pct >= high_threshold and abs(terminal_pct) >= high_terminal) else "MODERATE"
    vol_label, atr_pct = volatility_label(
        primary_result.history,
        int(strat.get("atr_period", 14)),
        int(strat.get("volatility_history_bars", 100)),
    )
    strategy_config = config["strategy"]["intraday" if category == "INTRADAY" else "swing"]
    timezone_name = config["project"].get("timezone", "Asia/Karachi")
    generated = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
    target_time = find_target_time(primary_result.forecast, direction, current_price, tp_pct, timezone_name)

    return {
        "symbol": symbol_info["symbol"],
        "pair": symbol_info["pair"],
        "display_name": symbol_info["display_name"],
        "category": category,
        "direction": direction,
        "current_price": current_price,
        "entry_reference": current_price,
        "take_profit": tp,
        "stop_loss": sl,
        "gross_tp_pct": tp_pct,
        "estimated_net_tp_pct": net_tp_pct,
        "stop_distance_pct": risk_pct,
        "reward_to_risk": rr,
        "primary_timeframe": strategy_config["primary_timeframe"],
        "confirmation_timeframe": strategy_config["confirmation_timeframe"],
        "forecast_candles": int(strategy_config["forecast_candles"]),
        "forecast_excursion_pct": excursion_pct,
        "terminal_forecast_pct": terminal_pct,
        "target_time": target_time,
        "generated_at": generated.strftime("%d %b %Y, %H:%M %Z"),
        "horizon_text": f"up to {int(strategy_config['forecast_candles'])} x {strategy_config['primary_timeframe']} candles",
        "confidence": confidence,
        "confidence_note": "heuristic; not a calibrated win probability",
        "volatility": vol_label,
        "atr_pct": atr_pct,
        "rr": rr,
    }


def format_alert(signal: dict[str, Any]) -> str:
    arrow = "🟢" if signal["direction"] == "LONG" else "🔴"
    return (
        f"Lucas v1.0 | {arrow} {signal['direction']}\n"
        f"1. Token & category: {signal['display_name']} ({signal['pair']}) · {signal['category']}\n"
        f"2. Current price & entry reference: {fmt_price(signal['current_price'])} / {fmt_price(signal['entry_reference'])}\n"
        f"3. TP / SL & estimated profit: TP {fmt_price(signal['take_profit'])} ({signal['gross_tp_pct']:+.2f}% gross; ~{signal['estimated_net_tp_pct']:+.2f}% after assumed costs); SL {fmt_price(signal['stop_loss'])}; R:R {signal['reward_to_risk']:.2f}:1\n"
        f"4. Signal time: {signal['generated_at']} · horizon {signal['horizon_text']} · earliest modelled TP touch: {signal['target_time']}\n"
        f"5. Confidence: {signal['confidence']} · {signal['primary_timeframe']} + {signal['confirmation_timeframe']} agree ({signal['confidence_note']})\n"
        f"6. Volatility: {signal['volatility']} · ATR {signal['atr_pct']:.2f}% of price\n\n"
        "Research alert only. Forecasts are uncertain; entry is a reference price, not a guaranteed fill."
    )


def send_telegram_message(text: str, token: str, chat_id: str, timeout: int = 20) -> None:
    if not token or not chat_id:
        raise LucasError("Telegram credentials are missing (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise LucasError(f"Telegram returned an error: {payload}")
    except requests.RequestException as exc:
        raise LucasError(f"Telegram message failed: {exc}") from exc


def _alert_key(signal: dict[str, Any]) -> str:
    return f"{signal['symbol']}|{signal['category']}"


def allowed_by_cooldown(signal: dict[str, Any], state: dict[str, Any], cooldown_hours: float) -> bool:
    previous = state.get("alerts", {}).get(_alert_key(signal))
    if not previous:
        return True
    if previous.get("direction") != signal["direction"]:
        # Confirmed direction reversals can be sent immediately; both configured
        # timeframes must already agree before a signal is created.
        return True
    try:
        previous_ts = datetime.fromisoformat(previous["timestamp"])
        now = datetime.now(timezone.utc)
        if previous_ts.tzinfo is None:
            previous_ts = previous_ts.replace(tzinfo=timezone.utc)
        return (now - previous_ts).total_seconds() >= float(cooldown_hours) * 3600
    except (ValueError, TypeError, KeyError):
        return True


def mark_alert_sent(signal: dict[str, Any], state: dict[str, Any]) -> None:
    state.setdefault("alerts", {})[_alert_key(signal)] = {
        "direction": signal["direction"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "price": signal["current_price"],
    }


def run_telegram_test(config: dict[str, Any]) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOGGER.error("Telegram test requested but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return 2
    tz = ZoneInfo(config["project"].get("timezone", "Asia/Karachi"))
    message = f"Lucas v1.0 — Telegram setup test\nStatus: connected\nTime: {datetime.now(timezone.utc).astimezone(tz).strftime('%d %b %Y, %H:%M %Z')}\nNo forecast or trade signal was generated."
    send_telegram_message(message, token, chat_id)
    LOGGER.info("Telegram test sent successfully")
    return 0


def run(config: dict[str, Any]) -> int:
    state_path = os.getenv("LUCAS_STATE_FILE", ".lucas_state.json")
    state = load_state(state_path)
    force_swing = parse_bool(os.getenv("LUCAS_FORCE_SWING", "false"))
    if parse_bool(os.getenv("LUCAS_TEST_TELEGRAM", "false")):
        return run_telegram_test(config)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    can_send_telegram = bool(token and chat_id and config["alerts"].get("telegram_enabled", True))
    if not can_send_telegram:
        LOGGER.warning("Telegram secrets are not configured. Alerts will be printed but not sent.")

    predictor = load_kronos_predictor(config)
    strategy_root = config["strategy"]
    swing_anchors = state.setdefault("swing_anchors", {})
    pending_swing_anchor_updates: dict[str, str] = {}
    pending_alerts: list[tuple[dict[str, Any], float]] = []

    for symbol_info in config["market_data"]["symbols"]:
        symbol = symbol_info["symbol"]
        try:
            current_price = fetch_current_price(config, symbol)
            intraday_cfg = strategy_root["intraday"]
            if intraday_cfg.get("enabled", True):
                p_hist = fetch_klines(config, symbol, intraday_cfg["primary_timeframe"])
                c_hist = fetch_klines(config, symbol, intraday_cfg["confirmation_timeframe"])
                p_result = predict(predictor, p_hist, intraday_cfg["primary_timeframe"], intraday_cfg["forecast_candles"], config["model"])
                c_result = predict(predictor, c_hist, intraday_cfg["confirmation_timeframe"], intraday_cfg["confirmation_candles"], config["model"])
                signal = create_signal(
                    config=config, symbol_info=symbol_info, category="INTRADAY",
                    primary_result=p_result, confirm_result=c_result, current_price=current_price,
                )
                if signal:
                    if allowed_by_cooldown(signal, state, float(intraday_cfg.get("repeat_cooldown_hours", 4))):
                        pending_alerts.append((signal, float(intraday_cfg.get("repeat_cooldown_hours", 4))))
                    else:
                        LOGGER.info("%s intraday duplicate suppressed by cooldown", symbol)

            swing_cfg = strategy_root["swing"]
            if swing_cfg.get("enabled", True):
                p_hist_4h = fetch_klines(config, symbol, swing_cfg["primary_timeframe"])
                latest_anchor = pd.Timestamp(p_hist_4h["timestamp"].iloc[-1]).isoformat()
                prev_anchor = swing_anchors.get(symbol)
                should_run_swing = force_swing or not swing_cfg.get("run_only_when_new_4h_candle_closes", True) or latest_anchor != prev_anchor
                if should_run_swing:
                    c_hist_day = fetch_klines(config, symbol, swing_cfg["confirmation_timeframe"])
                    p_result = predict(predictor, p_hist_4h, swing_cfg["primary_timeframe"], swing_cfg["forecast_candles"], config["model"])
                    c_result = predict(predictor, c_hist_day, swing_cfg["confirmation_timeframe"], swing_cfg["confirmation_candles"], config["model"])
                    signal = create_signal(
                        config=config, symbol_info=symbol_info, category="SWING",
                        primary_result=p_result, confirm_result=c_result, current_price=current_price,
                    )
                    if signal:
                        if allowed_by_cooldown(signal, state, float(swing_cfg.get("repeat_cooldown_hours", 24))):
                            pending_alerts.append((signal, float(swing_cfg.get("repeat_cooldown_hours", 24))))
                        else:
                            LOGGER.info("%s swing duplicate suppressed by cooldown", symbol)
                    pending_swing_anchor_updates[symbol] = latest_anchor
                else:
                    LOGGER.info("%s swing forecast skipped: same closed 4h candle as previous scan", symbol)
        except Exception as exc:
            # A failure for one symbol/timeframe must not prevent the other asset
            # from being evaluated. The failed swing anchor is not advanced.
            LOGGER.exception("Error processing %s: %s", symbol, exc)

    for signal, cooldown_hours in pending_alerts:
        message = format_alert(signal)
        print("\n" + message + "\n", flush=True)
        if can_send_telegram:
            try:
                send_telegram_message(message, token, chat_id)
                mark_alert_sent(signal, state)
                LOGGER.info("Telegram alert sent: %s %s %s", signal["symbol"], signal["category"], signal["direction"])
            except Exception as exc:
                # Do not mark alert sent if Telegram did not confirm delivery.
                LOGGER.exception("Failed to send Telegram alert: %s", exc)
        else:
            LOGGER.warning("Alert not sent because Telegram credentials are missing")

    swing_anchors.update(pending_swing_anchor_updates)
    save_state(state_path, state)
    LOGGER.info("Lucas v1.0 scan finished. Candidate alerts: %d", len(pending_alerts))
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LUCAS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    config_path = os.getenv("LUCAS_CONFIG", "config.yaml")
    try:
        config = load_config(config_path)
        return run(config)
    except Exception as exc:
        LOGGER.exception("Lucas v1.0 failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
