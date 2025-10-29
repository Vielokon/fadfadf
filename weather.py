import logging
import io
from time import monotonic
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from telegram.error import BadRequest, Forbidden, RetryAfter

from config import (
    UNCHECK_CHANNEL_ID, WEATHER_API_KEY, WEATHER_LAT, WEATHER_LON, WEATHER_CITY_LABEL,
    WEATHER_MIN_C, WEATHER_MAX_C, TIMEZONE
)
from state import save_state

log = logging.getLogger("weather")

# частоты
MIN_FETCH_SECONDS = 0.1         # реальный опрос API (тики могут быть 0.1s)
MIN_TITLE_UPDATE_SECONDS = 0    # разрешаем обновлять заголовок каждый раз
TEMP_EPS = 0.0                  # не используем EPS — обновляем каждый раз
HUM_EPS = 0.0

def _now_utc():
    return datetime.now(timezone.utc)

def _get_weather():
    """Возвращает dict с temp_c, humidity и pressure_mb.
    Бросает исключение при проблемах с сетевым запросом.
    """
    if not WEATHER_API_KEY:
        raise RuntimeError("WEATHER_API_KEY пуст — укажи ключ от weatherapi.com в .env")
    url = "https://api.weatherapi.com/v1/current.json"
    r = requests.get(url, params={"key": WEATHER_API_KEY, "q": f"{WEATHER_LAT},{WEATHER_LON}", "aqi":"no"}, timeout=10)
    r.raise_for_status()
    j = r.json()
    cur = j.get("current", {})
    return {
        "temp_c": float(cur.get("temp_c")),
        "humidity": float(cur.get("humidity", 0.0)),
        # weatherapi даёт pressure_mb (гектопаскали в миллибарах)
        "pressure_mb": float(cur.get("pressure_mb", 0.0)),
    }

def _channel_title(temp_c: float, humidity: float) -> str:
    sign = "+" if temp_c >= 0 else "-"
    # влажность без десятичных — достаточно целых процентов
    t = f"В 292 ЛЮБЯТ | {sign}{abs(temp_c):.1f} °C · {int(round(humidity))}% {WEATHER_CITY_LABEL}"
    return t[:128]

async def _set_title_safe(context, title: str):
    # Перед вызовом проверим UNCHECK_CHANNEL_ID на корректность
    if not UNCHECK_CHANNEL_ID:
        log.error("UNCHECK_CHANNEL_ID пустой/0 — заголовок не поменять")
        return False
    try:
        await context.bot.set_chat_title(chat_id=UNCHECK_CHANNEL_ID, title=title)
        log.info("set_chat_title OK: %s", title)
        return True
    except RetryAfter as e:
        log.warning("set_chat_title floodwait: retry_after=%s", getattr(e, "retry_after", "?"))
        return False
    except Forbidden as e:
        log.exception("Forbidden: нет прав на изменение заголовка (can_change_info?) — %s", e)
        return False
    except BadRequest as e:
        log.exception("BadRequest при set_chat_title: %s", e)
        return False
    except Exception as e:
        log.exception("Не удалось изменить заголовок канала: %s", e)
        return False

def _render_temp_chart(history: list[dict], min_c: float, max_c: float):
    tz = ZoneInfo(TIMEZONE)
    now = _now_utc()
    cutoff = now - timedelta(hours=24)
    xs, ys = [], []
    for rec in history or []:
        try:
            dt = datetime.fromisoformat(rec["ts"])
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            if dt < cutoff: continue
            xs.append(dt.astimezone(tz))
            ys.append(float(rec["temp_c"]))
        except Exception:
            continue
    if not xs:
        xs = [now.astimezone(tz)]
        ys = [None]

    fig, ax = plt.subplots(figsize=(8, 3))
    ax.plot(xs, ys, linewidth=2)
    ax.axhline(min_c, linestyle="--")
    ax.axhline(max_c, linestyle="--")
    ax.set_title(f"Температура за 24 часа — {WEATHER_CITY_LABEL}")
    ax.set_ylabel("°C")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png", dpi=160)
    plt.close(fig)
    buf.seek(0)
    return buf

def _build_alert_caption(temp: float, humidity: float, min_c: float, max_c: float, status: str):
    tz = ZoneInfo(TIMEZONE)
    now_local = _now_utc().astimezone(tz)
    if status == "below":
        delta = min_c - temp
        kind = "холодовая аномалия"
        sign = "−"
    else:
        delta = temp - max_c
        kind = "тепловая аномалия"
        sign = "+"

    return (
        "Метеоуведомление (на территории Купчино)\n"
        f"T = {temp:.1f} °C; влажность = {humidity:.0f}% ; диапазон [{min_c:.1f}; {max_c:.1f}] °C; Δ{sign}={delta:.1f} °C\n"
        f"Классификация: {kind}\n"
        f"Время (MSK): {now_local:%Y-%m-%d %H:%M}"
    )

async def weather_job(context):
    """
    Основная логика:
      - опрашиваем weatherapi (throttle MIN_FETCH_SECONDS)
      - сохраняем в history
      - каждый успешный fetch — пытаемся сразу поменять заголовок (force)
      - алёрты/графики оставлены
    """
    state = context.application.bot_data["state"]
    w = state.setdefault("weather", {})
    now_mono = monotonic()
    now = _now_utc()

    last_fetch_mono = w.get("last_fetch_mono")

    # 1) fetch throttle
    need_fetch = (last_fetch_mono is None) or ((now_mono - float(last_fetch_mono)) >= MIN_FETCH_SECONDS)
    if need_fetch:
        try:
            data = _get_weather()
            t = data["temp_c"]
            h = data["humidity"]
            pressure_mb = data.get("pressure_mb", 0.0)
            # обновляем кэш
            w["last_temp"] = t
            w["last_humidity"] = h
            w["last_pressure_mb"] = pressure_mb
            w["last_fetch_mono"] = now_mono
            w.setdefault("history", []).append({"ts": now.isoformat(), "temp_c": float(t), "humidity": float(h)})
            save_state(state)
            log.info("Погода: %.2f °C, %.0f%% влажность, %.1f mb (обновил кэш)", t, h, pressure_mb)
        except Exception as e:
            log.exception("Не удалось получить погоду: %s", e)
            return

    # если нет данных — выходим
    if w.get("last_temp") is None:
        return

    temp = float(w["last_temp"])
    humidity = float(w.get("last_humidity", 0.0))

    # 2) Принудительная смена заголовка при каждом fetch (без EPS)
    title = _channel_title(temp, humidity)
    ok = await _set_title_safe(context, title)
    if ok:
        # сохраняем метки успешной смены
        w["last_title_mono"] = now_mono
        w["last_title_temp"] = temp
        w["last_title_humidity"] = humidity
        save_state(state)

    # 3) детекция выхода за рамки и алёрт (как было)
    status = "ok"
    if temp < WEATHER_MIN_C: status = "below"
    elif temp > WEATHER_MAX_C: status = "above"

    prev = w.get("alert_status") or "ok"
    if status != "ok" and status != prev:
        try:
            prev_msg_id = w.get("last_alert_message_id")
            if prev_msg_id is not None:
                try:
                    await context.bot.delete_message(chat_id=UNCHECK_CHANNEL_ID, message_id=prev_msg_id)
                    log.info("deleted previous alert message id=%s", prev_msg_id)
                except Exception:
                    log.warning("could not delete previous alert message id=%s", prev_msg_id)

            chart = _render_temp_chart(w.get("history", []), WEATHER_MIN_C, WEATHER_MAX_C)
            caption = _build_alert_caption(temp, humidity, WEATHER_MIN_C, WEATHER_MAX_C, status)
            msg = await context.bot.send_photo(chat_id=UNCHECK_CHANNEL_ID, photo=chart, caption=caption)
            w["last_alert_message_id"] = getattr(msg, "message_id", None)
            w["alert_status"] = status
            w["last_alert_ts"] = now.isoformat()
            save_state(state)
        except Exception:
            log.exception("Не удалось отправить оповещение с графиком")
    elif status == "ok" and prev != "ok":
        prev_msg_id = w.get("last_alert_message_id")
        if prev_msg_id is not None:
            try:
                await context.bot.delete_message(chat_id=UNCHECK_CHANNEL_ID, message_id=prev_msg_id)
                log.info("deleted previous alert message id=%s on recovery", prev_msg_id)
            except Exception:
                log.warning("could not delete previous alert message id=%s on recovery", prev_msg_id)
            w.pop("last_alert_message_id", None)
        w["alert_status"] = "ok"
        save_state(state)

# Ручная проверка/диагностика
async def cmd_weather_ping(update, context):
    try:
        data = _get_weather()
        t = data["temp_c"]
        h = data["humidity"]
        p = data.get("pressure_mb", 0.0)
        ok = await _set_title_safe(context, _channel_title(t, h))
        await update.message.reply_text(
            f"weather_ping: temp={t:.2f} °C; humidity={h:.0f}% ; pressure={p:.1f} mb ; title_update={'OK' if ok else 'FAIL'}"
        )
    except Exception as e:
        await update.message.reply_text(f"weather_ping FAIL: {e}")
