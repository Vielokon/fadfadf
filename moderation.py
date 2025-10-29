from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from config import MOD_GROUP_ID
from state import save_state
from utils import compute_reach_stats

def mode_keyboard(cur: str):
    btn = "UNCHECK" if cur == "CHECK" else "CHECK"
    return InlineKeyboardMarkup([[InlineKeyboardButton(btn, callback_data=f"set_{btn}")]])

def decision_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ДОПУСТИТЬ", callback_data="allow"),
         InlineKeyboardButton("НЕ ДОПУСТИТЬ", callback_data="deny")]
    ])

async def is_admin(context, user_id: int) -> bool:
    try:
        admins = await context.bot.get_chat_administrators(MOD_GROUP_ID)
        return any(a.user.id == user_id for a in admins)
    except Exception:
        return False

async def upsert_control_message(app, state):
    """Создаёт/обновляет закреп с режимом и статистикой + статусом отбивки."""
    reach = compute_reach_stats(state.get("history", {}))
    bumper = state.get("bumper", {})
    lines = [
        f"Режим модерации: {state.get('mode')}",
        f"Уникальных пользователей в истории: {reach['total_unique_users']}",
        "",
        f"Отбивка: {'АКТИВНА' if bumper.get('active') else 'выключена'}",
        f"Текст: {bumper.get('text')!r}" if bumper.get("text") else "Текст: —",
        f"Охват с активации: {len(set(bumper.get('reach_user_ids', [])))} чел.",
        "",
        "Охват по дням (последние):"
    ]
    for day, cnt in reach["per_day_counts"][:14]:
        lines.append(f"{day}: {cnt}")
    lines.append("")
    lines.append("Охват по часам:")
    hours = {h:c for h,c in reach["per_hour_counts"]}
    for h in range(24):
        lines.append(f"{h:02d}:00 — {hours.get(h,0)}")

    text = "\n".join(lines)[:4096]
    cmid = state.get("control_message_id")
    try:
        if cmid:
            await app.bot.edit_message_text(chat_id=MOD_GROUP_ID, message_id=cmid, text=text,
                                            reply_markup=mode_keyboard(state.get("mode")))
        else:
            msg = await app.bot.send_message(chat_id=MOD_GROUP_ID, text=text, reply_markup=mode_keyboard(state.get("mode")))
            state["control_message_id"] = msg.message_id
            try:
                await app.bot.pin_chat_message(chat_id=MOD_GROUP_ID, message_id=msg.message_id, disable_notification=True)
            except Exception:
                pass
            save_state(state)
    except Exception:
        # если не удалось отредактировать (удалён?), создаём новый
        msg = await app.bot.send_message(chat_id=MOD_GROUP_ID, text=text, reply_markup=mode_keyboard(state.get("mode")))
        state["control_message_id"] = msg.message_id
        try:
            await app.bot.pin_chat_message(chat_id=MOD_GROUP_ID, message_id=msg.message_id, disable_notification=True)
        except Exception:
            pass
        save_state(state)

# команды отбивки (только админы в модчате)
async def cmd_bumper_set(update, context, state):
    if update.effective_chat.id != MOD_GROUP_ID: return
    if not await is_admin(context, update.effective_user.id): return
    text = " ".join(context.args) if context.args else ""
    state["bumper"]["text"] = (text or "").strip()
    await update.message.reply_text("Текст отбивки установлен." if text else "Текст отбивки очищен.")
    await upsert_control_message(context.application, state); save_state(state)

async def cmd_bumper_on(update, context, state):
    if update.effective_chat.id != MOD_GROUP_ID: return
    if not await is_admin(context, update.effective_user.id): return
    b = state["bumper"]; b["active"] = True; b["version"] += 1; b["reach_user_ids"] = []
    await update.message.reply_text("Отбивка активирована.")
    await upsert_control_message(context.application, state); save_state(state)

async def cmd_bumper_off(update, context, state):
    if update.effective_chat.id != MOD_GROUP_ID: return
    if not await is_admin(context, update.effective_user.id): return
    state["bumper"]["active"] = False
    await update.message.reply_text("Отбивка выключена.")
    await upsert_control_message(context.application, state); save_state(state)

async def cmd_bumper_status(update, context, state):
    if update.effective_chat.id != MOD_GROUP_ID: return
    await upsert_control_message(context.application, state)