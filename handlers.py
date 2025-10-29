import math
from datetime import timezone
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from telegram.ext import ContextTypes
from config import MOD_GROUP_ID, UNCHECK_CHANNEL_ID, APPROVED_CHANNEL_ID, MEDIA_GROUP_WAIT, TIMEZONE
from state import save_state
from utils import fmt_size, human_speed, compute_stats, now_utc
from energy import EnergyInput, estimate_energy
from moderation import decision_keyboard, upsert_control_message
def get_scheduler(context):
    # если есть «родной» PTB JobQueue — используй его, иначе наш фолбэк из bot_data
    jq = getattr(context, "job_queue", None)
    return jq if jq is not None else context.application.bot_data.get("scheduler")
# ====== Вспомогательные ======
def payload_size_bytes(payload) -> int:
    t = payload.get('type')
    try:
        if t == 'text':
            return len((payload.get('text') or "").encode("utf-8"))
        if t in ('photo','video','document'):
            return int(payload.get('file_size') or 0)
        if t == 'media_group':
            return sum(int(it.get('file_size') or 0) for it in payload.get('items', []))
    except Exception:
        pass
    return 0

def dedup_key(chat_id, msg_id):
    return f"{chat_id}:{msg_id}"

async def send_user_receipt_once(context, state, user_chat_id: int, key: str, *,
                                 size_b: int, speed_bps: float | None,
                                 delivery_seconds: float | None,
                                 rtt_ms: float | None):
    """Отправить пользователю сводку (со скоростью и энергией) только 1 раз на доставку."""
    if state["dedup_receipts"].get(key):
        return
    state["dedup_receipts"][key] = True
    save_state(state)

    # энергомодель (auto сеть)
    energy = estimate_energy(EnergyInput(total_bytes=size_b, duration_s=delivery_seconds, rtt_ms=rtt_ms, network="auto"))
    net_line = f", сеть: {energy.get('network')}" if energy.get("has_duration") else ""
    if energy.get("has_duration"):
        energy_line = f"Энергия (оценка): {energy['total_j']:.2f} Дж (~{(energy['j_per_mb'] or 0):.4f} Дж/MB){net_line}"
    else:
        energy_line = f"Энергия: нужна длительность для точной оценки"

    text = (
        "Сводка доставки:\n"
        f" • Размер: {fmt_size(size_b)}\n"
        f" • Скорость: {human_speed(speed_bps)}\n"
        f" • Длительность: {delivery_seconds:.3f}s\n"
        f" • {energy_line}\n"
    )

    # отбивка (если активна)
    bumper = state.get("bumper", {})
    if bumper.get("active") and bumper.get("text"):
        text += f"\n{bumper['text']}"
        # учёт охвата
        reach = set(bumper.get("reach_user_ids", []))
        reach.add(int(user_chat_id))
        bumper["reach_user_ids"] = list(reach)
        save_state(state)
        # обновим закреп (чтобы в модчате росло число)
        try:
            await upsert_control_message(context.application, state)
        except Exception:
            pass

    try:
        await context.bot.send_message(chat_id=user_chat_id, text=text)
    except Exception:
        pass

# ====== Старт и режим ======
async def cmd_start(update, context, state):
    if update.effective_chat.id == MOD_GROUP_ID:
        await upsert_control_message(context.application, state)
    else:
        if update.message:
            await update.message.reply_text("Привет! Пришли сообщение — текст/фото/видео/документ. Оно уйдёт в канал после модерации.")

async def cb_mode_toggle(update, context, state):
    q = update.callback_query
    await q.answer()
    new_mode = q.data.split("_",1)[1]
    state["mode"] = new_mode
    save_state(state)
    await upsert_control_message(context.application, state)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

# ====== Пользовательские сообщения ======
async def handle_private(update, context, state):
    msg = update.effective_message
    if not msg: return
    user = msg.from_user
    uid = str(user.id)

    # учёт счётчика
    count = int(state["counts"].get(uid, 0)) + 1
    state["counts"][uid] = count
    save_state(state)

    # определяем payload + размер
    payload = {"type":"unknown"}
    sent_dt = msg.date  # datetime (naive UTC в PTB), приведём к aware
    if sent_dt and sent_dt.tzinfo is None:
        sent_dt = sent_dt.replace(tzinfo=timezone.utc)  # type: ignore

    if getattr(msg, "media_group_id", None):
        mgid = msg.media_group_id
        item = None
        if getattr(msg, "photo", None):
            ph = msg.photo[-1]; item = {"subtype":"photo","file_id":ph.file_id,"file_size":ph.file_size,"caption":msg.caption or ""}
        elif getattr(msg, "video", None):
            v = msg.video; item = {"subtype":"video","file_id":v.file_id,"file_size":v.file_size,"caption":msg.caption or ""}
        elif getattr(msg, "document", None):
            d = msg.document; item = {"subtype":"document","file_id":d.file_id,"file_size":d.file_size,"caption":msg.caption or ""}
        else:
            item = {"subtype":"unknown","file_size":0,"caption":msg.caption or ""}

        item["date"] = sent_dt.isoformat() if sent_dt else None
        state["media_groups"].setdefault(mgid, []).append(item)
        save_state(state)
        # форвард в модчат (без клавы)
        try:
            forwarded = await context.bot.copy_message(chat_id=MOD_GROUP_ID, from_chat_id=msg.chat_id, message_id=msg.message_id)
            state["media_groups_forwarded"].setdefault(mgid, []).append(forwarded.message_id)
            save_state(state)
        except Exception:
            pass
        # планируем флеш альбома
        context.job_queue.run_once(flush_media_group, when=MEDIA_GROUP_WAIT, data={"mgid": mgid, "user": {"id": user.id, "username": user.username, "full_name": user.full_name}})
        try:
            await msg.reply_text("Принял альбом — собираю файлы…")
        except Exception:
            pass
        return

    # одиночные
    if getattr(msg, "text", None):
        payload = {"type":"text","text":msg.text}
    elif getattr(msg, "photo", None):
        ph = msg.photo[-1]
        payload = {"type":"photo","file_id":ph.file_id,"file_size":ph.file_size,"caption":msg.caption or ""}
    elif getattr(msg, "video", None):
        v = msg.video
        payload = {"type":"video","file_id":v.file_id,"file_size":v.file_size,"caption":msg.caption or ""}
    elif getattr(msg, "document", None):
        d = msg.document
        payload = {"type":"document","file_id":d.file_id,"file_size":d.file_size,"caption":msg.caption or ""}

    size_b = payload_size_bytes(payload)

    # метрики
    now = now_utc()
    delivery_seconds = (now - sent_dt).total_seconds() if sent_dt else None
    speed_bps = (size_b / delivery_seconds) if delivery_seconds and delivery_seconds>0 else (float("inf") if delivery_seconds == 0 and size_b>0 else None)

    # история пользователя
    user_hist = state.get("history", {}).get(uid, [])
    stats = compute_stats(user_hist)
    hist_bytes = sum((e.get("bytes") or 0) for e in user_hist)
    total_user_bytes = hist_bytes + size_b

    # заголовок в модчат
    lines = [
        f"Пользователь: {user.full_name} ({f'@{user.username}' if user.username else '—'}) ID {user.id}",
        f"Это его {count}-е сообщение.",
        f"Размер: {fmt_size(size_b)}; суммарно от него: {fmt_size(total_user_bytes)}",
        f"Доставка: {delivery_seconds:.3f}s; скорость: {human_speed(speed_bps)}" if delivery_seconds is not None else "Доставка: —"
    ]
    # исторические скорости
    sp = stats.get("speeds_bps", {})
    if sp.get("count"):
        try:
            dev = ((speed_bps - sp["mean"]) / sp["mean"] * 100.0) if (speed_bps and sp["mean"]>0 and not math.isinf(speed_bps)) else None
        except Exception:
            dev = None
        lines += [
            f"Истор. средняя скорость: {human_speed(sp.get('mean'))}",
            f"Отклонение текущей: {dev:+.1f}%" if dev is not None else "Отклонение: —"
        ]
    header = "\n".join(lines)

    # всегда шлём в модчат заголовок + оригинал
    try:
        await context.bot.send_message(chat_id=MOD_GROUP_ID, text=header)
        await context.bot.copy_message(chat_id=MOD_GROUP_ID, from_chat_id=msg.chat_id, message_id=msg.message_id)
    except Exception:
        pass

    # РЕЖИМЫ: CHECK/UNCHECK
    if state.get("mode") == "CHECK":
        # кладём в pending с кнопками решения
        try:
            kbd = decision_keyboard()
            sent = await context.bot.send_message(chat_id=MOD_GROUP_ID, text="Решение по сообщению:", reply_markup=kbd)
            pend_payload = {"type": payload.get("type"), "data": payload}
            state["pending"][str(sent.message_id)] = {"user_id": user.id, "payload": pend_payload}
            save_state(state)
        except Exception:
            pass
    else:
        # сразу в канал
        try:
            if payload["type"] == "text":
                await context.bot.send_message(chat_id=UNCHECK_CHANNEL_ID, text=payload["text"])
            elif payload["type"] == "photo":
                await context.bot.send_photo(chat_id=UNCHECK_CHANNEL_ID, photo=payload["file_id"], caption=payload.get("caption",""))
            elif payload["type"] == "video":
                await context.bot.send_video(chat_id=UNCHECK_CHANNEL_ID, video=payload["file_id"], caption=payload.get("caption",""))
            elif payload["type"] == "document":
                await context.bot.send_document(chat_id=UNCHECK_CHANNEL_ID, document=payload["file_id"], caption=payload.get("caption",""))
        except Exception:
            pass

    # запись в историю
    entry = {
        "bytes": int(size_b or 0),
        "delivery_seconds": delivery_seconds,
        "speed_bps": speed_bps,
        "timestamp": now.isoformat(),
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
    }
    state.setdefault("history", {}).setdefault(uid, []).append(entry)
    save_state(state)

    # отправим пользователю **одну** сводку (с энергией) — dedup по chat_id:msg_id
    key = dedup_key(msg.chat_id, msg.message_id)
    await send_user_receipt_once(context, state, user_chat_id=msg.chat_id, key=key,
                                 size_b=size_b, speed_bps=speed_bps, delivery_seconds=delivery_seconds, rtt_ms=None)

# ====== Флеш альбомов ======
async def flush_media_group(context: ContextTypes.DEFAULT_TYPE):
    job = context.job; data = job.data or {}
    mgid = data.get("mgid"); user = data.get("user") or {}
    state = context.application.bot_data["state"]
    items = state["media_groups"].pop(mgid, [])
    save_state(state)
    if not items: return

    now = now_utc()
    dates = []
    total_bytes = 0
    for it in items:
        if it.get("date"):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(it["date"])
                dates.append(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))  # type: ignore
            except Exception:
                pass
        try:
            total_bytes += int(it.get("file_size") or 0)
        except Exception:
            pass

    earliest = min(dates) if dates else None
    delivery_seconds = (now - earliest).total_seconds() if earliest else None
    speed_bps = (total_bytes / delivery_seconds) if delivery_seconds and delivery_seconds>0 else None

    # сводка для модчата
    lines = [
        f"Альбом от {user.get('full_name')} ({'@'+user.get('username') if user.get('username') else '—'})",
        f"Файлов: {len(items)}, общий вес: {fmt_size(total_bytes)}",
        f"Доставка: {delivery_seconds:.3f}s; скорость: {human_speed(speed_bps)}" if delivery_seconds is not None else "Доставка: —",
    ]
    try:
        await context.bot.send_message(chat_id=MOD_GROUP_ID, text="\n".join(lines))
    except Exception:
        pass

    # режим
    if state.get("mode") == "CHECK":
        try:
            kbd = decision_keyboard()
            sent = await context.bot.send_message(chat_id=MOD_GROUP_ID, text="Решение по альбому:", reply_markup=kbd)
            state["pending"][str(sent.message_id)] = {"user_id": user.get("id"), "payload": {"type":"media_group","items":items}}
            save_state(state)
        except Exception:
            pass
    else:
        # отправляем в канал
        media = []
        docs = []
        for it in items:
            if it.get("subtype") == "photo":
                media.append(InputMediaPhoto(media=it["file_id"], caption=it.get("caption","")))
            elif it.get("subtype") == "video":
                media.append(InputMediaVideo(media=it["file_id"], caption=it.get("caption","")))
            elif it.get("subtype") == "document":
                docs.append(it)
        if media:
            try:
                await context.bot.send_media_group(chat_id=UNCHECK_CHANNEL_ID, media=media)
            except Exception:
                pass
        for d in docs:
            try:
                await context.bot.send_document(chat_id=UNCHECK_CHANNEL_ID, document=d["file_id"], caption=d.get("caption",""))
            except Exception:
                pass

    # история
    uid = str(user.get("id"))
    entry = {"bytes": int(total_bytes), "delivery_seconds": delivery_seconds, "speed_bps": speed_bps, "timestamp": now.isoformat(),
             "user_id": user.get("id"), "username": user.get("username"), "full_name": user.get("full_name")}
    state.setdefault("history", {}).setdefault(uid, []).append(entry)
    save_state(state)

    # сводка пользователю 1 раз
    key = f"album:{mgid}"
    await send_user_receipt_once(context, state, user_chat_id=user.get("id"), key=key,
                                 size_b=total_bytes, speed_bps=speed_bps, delivery_seconds=delivery_seconds, rtt_ms=None)

# ====== Решения модерации ======
async def cb_decision(update, context, state):
    q = update.callback_query
    await q.answer()
    msg_id = str(q.message.message_id)
    entry = state.get("pending", {}).pop(msg_id, None)
    save_state(state)
    try:
        await context.bot.edit_message_reply_markup(chat_id=q.message.chat_id, message_id=q.message.message_id, reply_markup=None)
    except Exception:
        pass
    if not entry: return
    allow = (q.data == "allow")
    user_id = entry.get("user_id")
    payload = entry.get("payload") or {}
    try:
        await context.bot.send_message(chat_id=MOD_GROUP_ID, text=f"Решение: {'допущено' if allow else 'не допущено'}.")
    except Exception:
        pass
    try:
        await context.bot.send_message(chat_id=user_id, text="Ваша идея одобрена!" if allow else "Ваша идея не прошла модерацию.")
    except Exception:
        pass
    if not allow: return

    # публикация
    t = payload.get("type")
    d = (payload.get("data") or {}) if t != "media_group" else {}
    try:
        if t == "text":
            await context.bot.send_message(chat_id=APPROVED_CHANNEL_ID, text=d.get("text",""))
        elif t == "photo":
            await context.bot.send_photo(chat_id=APPROVED_CHANNEL_ID, photo=d.get("file_id"), caption=d.get("caption",""))
        elif t == "video":
            await context.bot.send_video(chat_id=APPROVED_CHANNEL_ID, video=d.get("file_id"), caption=d.get("caption",""))
        elif t == "document":
            await context.bot.send_document(chat_id=APPROVED_CHANNEL_ID, document=d.get("file_id"), caption=d.get("caption",""))
        elif t == "media_group":
            items = payload.get("items", [])
            media, docs = [], []
            for it in items:
                if it.get("subtype")=="photo":
                    media.append(InputMediaPhoto(media=it["file_id"], caption=it.get("caption","")))
                elif it.get("subtype")=="video":
                    media.append(InputMediaVideo(media=it["file_id"], caption=it.get("caption","")))
                elif it.get("subtype")=="document":
                    docs.append(it)
            if media:
                await context.bot.send_media_group(chat_id=APPROVED_CHANNEL_ID, media=media)
            for d in docs:
                await context.bot.send_document(chat_id=APPROVED_CHANNEL_ID, document=d["file_id"], caption=d.get("caption",""))
    except Exception:
        pass