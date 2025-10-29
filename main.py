import asyncio
import logging
import time
from types import SimpleNamespace

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import (
    BOT_TOKEN, ENABLE_WEATHER, DAILY_ENABLE, DAILY_MORNING, DAILY_EVENING, TIMEZONE,
    UNCHECK_CHANNEL_ID
)
from state import load_state, save_state
from moderation import (
    upsert_control_message,
    cmd_bumper_set,
    cmd_bumper_on,
    cmd_bumper_off,
    cmd_bumper_status,
)
from handlers import (
    cmd_start,
    cb_mode_toggle,
    handle_private,
    cb_decision,
)

# ========= Мини-планировщик =========
class MiniJobQueue:
    def __init__(self, app, logger):
        self.app = app
        self.logger = logger
        self._tasks = set()

    def _track(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def run_once(self, callback, when: float, data=None, name: str | None = None):
        async def _runner():
            try:
                await asyncio.sleep(float(when))
                ctx = SimpleNamespace(application=self.app, bot=self.app.bot, job=SimpleNamespace(data=data))
                await callback(ctx)
            except Exception:
                self.logger.exception("MiniJobQueue run_once error")
        return self._track(_runner())

    def run_repeating(self, callback, interval: float, first: float | int = 0, name: str | None = None, data=None):
        async def _loop():
            try:
                await asyncio.sleep(float(first or 0))
                while True:
                    try:
                        ctx = SimpleNamespace(application=self.app, bot=self.app.bot, job=SimpleNamespace(data=data))
                        await callback(ctx)
                    except Exception:
                        self.logger.exception("MiniJobQueue run_repeating tick error")
                    await asyncio.sleep(float(interval))
            except Exception:
                self.logger.exception("MiniJobQueue run_repeating loop error")
        return self._track(_loop())

# ========================= Логирование =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("bot")

# ========================= Обработчик: удаляем service-сообщения о смене названия =========================
async def delete_new_title_service_message(update, context):
    """
    Ловим системное service-сообщение NEW_CHAT_TITLE и пытаемся его удалить сразу.
    Для успешного удаления бот должен иметь право can_delete_messages в этом чате.
    """
    msg = update.effective_message
    if not msg:
        return
    # Проверим, что это именно служебное сообщение о смене названия
    if getattr(msg, "new_chat_title", None) is None:
        return

    try:
        await msg.delete()
        logger.info(f"Deleted new_chat_title service message {msg.message_id} in chat {msg.chat.id}")
    except Exception:
        logger.exception("Failed to delete new_chat_title service message")

# ========================= Диагностическая команда =========================
async def cmd_check_title(update, context):
    """
    Возвращает информацию о целевом чате и правах бота.
    Вызвать можно в личке боту.
    """
    try:
        chat = await context.bot.get_chat(UNCHECK_CHANNEL_ID)
    except Exception as e:
        await update.message.reply_text(f"get_chat FAILED: {e}")
        return

    try:
        me = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(UNCHECK_CHANNEL_ID, me.id)
    except Exception as e:
        await update.message.reply_text(f"get_chat_member FAILED: {e}")
        return

    perms = []
    for attr in ("status", "can_change_info", "can_delete_messages", "can_post_messages", "can_edit_messages", "is_member"):
        v = getattr(bot_member, attr, None)
        if v is None:
            # Возможные места хранения прав в разных версиях PTB
            v = getattr(getattr(bot_member, "privileges", None), attr, None)
        perms.append(f"{attr}={v}")

    text = (
        f"Chat: id={getattr(chat, 'id', None)}, type={getattr(chat, 'type', None)}, title={getattr(chat, 'title', None)}\n"
        f"Bot member info:\n" + "\n".join(perms)
    )
    await update.message.reply_text(text)

# ========================= Точка входа =========================
async def main():
    state = load_state()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.bot_data["state"] = state
    scheduler = MiniJobQueue(app, logger)
    app.bot_data["scheduler"] = scheduler

    # Регистрируем обработчик service-сообщений о смене названия (делать до старта)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_TITLE, delete_new_title_service_message))

    # Команды и колбэки
    # Используем лямбды чтобы передать state в старые обработчики
    app.add_handler(CommandHandler("start", lambda u, c: cmd_start(u, c, app.bot_data["state"])))
    app.add_handler(CommandHandler("check_title", cmd_check_title))
    app.add_handler(CallbackQueryHandler(lambda u, c: cb_mode_toggle(u, c, app.bot_data["state"]), pattern=r"^set_"))
    app.add_handler(CallbackQueryHandler(lambda u, c: cb_decision(u, c, app.bot_data["state"]), pattern=r"^(allow|deny)$"))

    # Отбивка
    app.add_handler(CommandHandler("bumper_set", lambda u, c: cmd_bumper_set(u, c, app.bot_data["state"])))
    app.add_handler(CommandHandler("bumper_on",  lambda u, c: cmd_bumper_on(u, c, app.bot_data["state"])))
    app.add_handler(CommandHandler("bumper_off", lambda u, c: cmd_bumper_off(u, c, app.bot_data["state"])))
    app.add_handler(CommandHandler("bumper_status", lambda u, c: cmd_bumper_status(u, c, app.bot_data["state"])))

    # Личные сообщения
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND),
                                   lambda u, c: handle_private(u, c, app.bot_data["state"])))

    # Погода и обновление закрепа (если включено)
    if ENABLE_WEATHER:
        # импортируем здесь, чтобы избежать лишних импортов когда погода отключена
        from weather import weather_job, cmd_weather_ping

        logger.info("Weather enabled: scheduling weather_job and pin updates.")
        # Твой тикинг для weather_job (как и раньше)
        # Прим: интервал=0.1 в примере — вероятно для теста. В реале поставьте 60 или больше.
        scheduler.run_repeating(weather_job, interval=0.1, first=0.1)
        # Добавим команду для ручной проверки погоды
        app.add_handler(CommandHandler("weather_ping", cmd_weather_ping))

    # Ежедневные сообщения
    from daily import schedule_daily, cmd_daily_on, cmd_daily_off, cmd_daily_status, cmd_daily_set
    app.add_handler(CommandHandler("daily_on", cmd_daily_on))
    app.add_handler(CommandHandler("daily_off", cmd_daily_off))
    app.add_handler(CommandHandler("daily_status", cmd_daily_status))
    app.add_handler(CommandHandler("daily_set", cmd_daily_set))

    await app.initialize()
    await upsert_control_message(app, state)

    # Планируем ежедневные только если включено
    if DAILY_ENABLE:
        logger.info(f"Daily enabled: morning {DAILY_MORNING}, evening {DAILY_EVENING} ({TIMEZONE})")
        schedule_daily(scheduler, TIMEZONE, DAILY_MORNING, DAILY_EVENING)
        state.setdefault("daily", {})["enabled"] = True
    else:
        state.setdefault("daily", {})["enabled"] = False

    try:
        await app.start()
        # В старых версиях PTB использовался updater; если у вас современная версия — этот вызов может быть лишним.
        try:
            await app.updater.start_polling()
        except Exception:
            # Игнорируем, если updater отсутствует/не используется
            pass

        logger.info("Bot is running.")
        # Блокируем навсегда — процесс останется живым
        await asyncio.Event().wait()
    finally:
        try:
            await app.updater.stop_polling()
        except Exception:
            pass
        try:
            await app.stop()
        except Exception:
            pass
        save_state(state)


if __name__ == "__main__":
    # Небольшой защитный цикл перезапуска — скрипт будет автоматически перезапускаться при падениях.
    if not BOT_TOKEN or BOT_TOKEN.strip() == "":
        raise SystemExit("Заполни BOT_TOKEN в .env!")

    backoff = 1
    max_backoff = 60
    while True:
        try:
            asyncio.run(main())
            # Если main() завершился нормально (намеренное завершение) — выйдем
            break
        except KeyboardInterrupt:
            logger.info("Received KeyboardInterrupt — exiting")
            break
        except Exception:
            logger.exception("Bot crashed — restarting after %s seconds", backoff)
            time.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)
