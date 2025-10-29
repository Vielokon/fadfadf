# state.py
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

# используемые константы — меняй при желании
STATE_DIR = os.getenv("STATE_DIR", ".")
STATE_FILE = os.getenv("STATE_FILE", os.path.join(STATE_DIR, "bot_state.json"))

HISTORY_MAX_DAYS = 1            # хранить историю не старше N дней
HISTORY_MAX_PER_USER = 5     # максимум записей истории на одного user_id
HOURLY_PRUNE_INTERVAL = 36    # секунды (1 час)
WEEKLY_PRUNE_INTERVAL = 70  # 7 дней
DICT_MAX_KEEP = 5000            # если нет timestamp — обрезаем до этого числа записей

DEFAULT_STATE = {
    "mode": "UNCHECK",                    # CHECK / UNCHECK
    "control_message_id": None,           # закреп в модчате
    "pending": {},                        # ожидание решения модерации
    "counts": {},                         # счётчик сообщений на человека
    "media_groups": {},                   # буфер альбомов
    "history": {},                        # история по user_id -> [entries]
    "stats": {},                          # агрегаты
    "dedup_receipts": {},                 # чтобы сводку с энергией слать 1 раз на доставку
    "bumper": {                           # конфиг «отбивки» (реклама/сообщение)
        "active": False,
        "text": "",
        "version": 0,
        "reach_user_ids": [],            # уникальные увидевшие с момента активации
    },
    "media_groups_forwarded": {}
}

# ---- utilities -------------------------------------------------------------
def ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)

def _now_utc():
    return datetime.now(timezone.utc)

def _iso_to_dt(s: str):
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

# ---- pruning helpers ------------------------------------------------------
def _prune_history(state: Dict[str, Any]):
    """Обрезаем history: удаляем записи старше HISTORY_MAX_DAYS и лимитируем per-user."""
    if "history" not in state or not isinstance(state["history"], dict):
        return
    cutoff = _now_utc() - timedelta(days=HISTORY_MAX_DAYS)
    new_hist = {}
    for user_id, entries in state["history"].items():
        if not isinstance(entries, list):
            continue
        pruned = []
        for rec in entries:
            # ожидаем, что запись может содержать поле 'ts' с iso-строкой
            ts = None
            if isinstance(rec, dict):
                ts = rec.get("ts") or rec.get("timestamp") or rec.get("time")
            # если есть ts — фильтруем по дате
            if isinstance(ts, str):
                dt = _iso_to_dt(ts)
                if dt is None:
                    # непонятный формат — сохраняем (чтобы не потерять данные)
                    pruned.append(rec)
                else:
                    if dt >= cutoff:
                        pruned.append(rec)
                    # иначе — отбрасываем
            else:
                # нет ts — оставляем (нет возможности отсечь по времени)
                pruned.append(rec)
        # обрезаем по количеству (оставляем последние записи)
        if len(pruned) > HISTORY_MAX_PER_USER:
            pruned = pruned[-HISTORY_MAX_PER_USER:]
        if pruned:
            new_hist[user_id] = pruned
    state["history"] = new_hist

def _prune_dict_by_ts_or_size(d: Dict, keep_days: int):
    """
    Для словарей вида id -> record пытаемся удалить записи старше keep_days,
    если рекорды содержат 'ts'/'time'/'timestamp'. Если нет временных меток — обрезаем по size.
    Возвращает новый словарь.
    """
    if not isinstance(d, dict):
        return {}
    cutoff = _now_utc() - timedelta(days=keep_days)
    kept = {}
    # если значения — не dict/str с ts, соберём пары (k, approx_ts) для сортировки
    fallback_items = []
    for k, v in d.items():
        ts = None
        if isinstance(v, dict):
            ts = v.get("ts") or v.get("timestamp") or v.get("time")
        if isinstance(ts, str):
            dt = _iso_to_dt(ts)
            if dt is not None and dt >= cutoff:
                kept[k] = v
        else:
            # нет ts — отложим на случай, если окажется мало данных с ts
            fallback_items.append((k, v))
    # если мало kept и много fallback — обрежем fallback по количеству
    if len(kept) < DICT_MAX_KEEP and fallback_items:
        # добавим последние (по неопределённому порядку) до лимита
        to_add = DICT_MAX_KEEP - len(kept)
        for k, v in fallback_items[:to_add]:
            kept[k] = v
    return kept

def _truncate_list_if_needed(lst: list, max_items: int):
    if not isinstance(lst, list):
        return lst
    if len(lst) > max_items:
        return lst[-max_items:]
    return lst

# ---- top-level prune orchestrator -----------------------------------------
def _hourly_prune(state: Dict[str, Any]):
    """Лёгкая очистка, делаем каждый час."""
    # 1) history: удаляем старые записи и обрезаем per-user
    try:
        _prune_history(state)
    except Exception:
        # не ломаем сохранение, но не даём исключению всплыть
        pass

    # 2) media_groups: убрать очень старые / большое число элементов
    try:
        if "media_groups" in state and isinstance(state["media_groups"], dict):
            state["media_groups"] = _prune_dict_by_ts_or_size(state["media_groups"], keep_days=2)
    except Exception:
        pass

def _weekly_prune(state: Dict[str, Any]):
    """Глубокая очистка, раз в 7 дней."""
    try:
        # history уже поддерживается hourly; дополнительно — убеждаемся в общем лимите по пользователям
        if isinstance(state.get("history"), dict):
            # если пользователей слишком много — оставим только самых активных по количеству записей
            hist = state["history"]
            if len(hist) > DICT_MAX_KEEP:
                # сортируем по длине записей и оставляем топ DICT_MAX_KEEP
                items = sorted(hist.items(), key=lambda kv: len(kv[1]) if isinstance(kv[1], list) else 0)
                # keep last DICT_MAX_KEEP (самые большие)
                keep = dict(items[-DICT_MAX_KEEP:])
                state["history"] = keep
    except Exception:
        pass

    # prune big-ish dict-like structures with 7-day window
    try:
        state["dedup_receipts"] = _prune_dict_by_ts_or_size(state.get("dedup_receipts", {}), keep_days=HISTORY_MAX_DAYS)
    except Exception:
        pass
    try:
        state["media_groups_forwarded"] = _prune_dict_by_ts_or_size(state.get("media_groups_forwarded", {}), keep_days=HISTORY_MAX_DAYS)
    except Exception:
        pass

    # bumper.reach_user_ids: если очень длинный — обрежем до разумного
    try:
        if isinstance(state.get("bumper", {}).get("reach_user_ids"), list):
            state["bumper"]["reach_user_ids"] = _truncate_list_if_needed(state["bumper"]["reach_user_ids"], max_items=DICT_MAX_KEEP)
    except Exception:
        pass

# ---- load/save with atomic write ------------------------------------------
def load_state() -> Dict[str, Any]:
    ensure_dir()
    if not os.path.exists(STATE_FILE):
        # возвращаем копию, чтобы изменения в DEFAULT_STATE не ломали структуру
        return {k: (v.copy() if isinstance(v, dict) else (v[:] if isinstance(v, list) else v)) for k, v in DEFAULT_STATE.items()}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        # если файл битый — возвращаем дефолт (без ошибок)
        return {k: (v.copy() if isinstance(v, dict) else (v[:] if isinstance(v, list) else v)) for k, v in DEFAULT_STATE.items()}
    # минимальная миграция — ensure keys
    for k, v in DEFAULT_STATE.items():
        d.setdefault(k, v)
    return d

def save_state(s: Dict[str, Any]) -> None:
    ensure_dir()
    now_ts = int(_now_utc().timestamp())
    # hourly prune if needed
    last_hourly = s.get("_last_prune_hourly", 0)
    if now_ts - int(last_hourly) >= HOURLY_PRUNE_INTERVAL:
        try:
            _hourly_prune(s)
        except Exception:
            pass
        s["_last_prune_hourly"] = now_ts

    # weekly prune if needed
    last_weekly = s.get("_last_prune_weekly", 0)
    if now_ts - int(last_weekly) >= WEEKLY_PRUNE_INTERVAL:
        try:
            _weekly_prune(s)
        except Exception:
            pass
        s["_last_prune_weekly"] = now_ts

    # атомарная запись
    dirpath = os.path.dirname(STATE_FILE) or "."
    os.makedirs(dirpath, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmpf:
            json.dump(s, tmpf, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
