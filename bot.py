import html
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("nezzx-deal-bot")

WEBHOOK_PATH = "/telegram/webhook"
MAX_OFFER_LENGTH = 1500

# Telegram custom emoji IDs supplied by the project owner.
EMOJI_OFFER = "5330237710655306682"
EMOJI_CONFIRM = "5206607081334906820"
EMOJI_SAFE_DEAL = "5251203410396458957"
EMOJI_STARS = "5438496463044752972"
EMOJI_WARNING = "5251340119205501791"


def custom_emoji(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def warning_footer() -> str:
    # Keep the warning clear without falsely claiming Telegram officially protects this bot.
    return (
        f"\n\n{custom_emoji(EMOJI_WARNING, '⚠️')} <b>Внимание!</b> Предупреждение о мошенниках: "
        "не доверяйте незнакомым участникам и самостоятельно проверяйте оплату. "
        "Telegram не подтверждает и не гарантирует сделки через этого бота."
    )


def welcome_text() -> str:
    return (
        "<b>NEZZX — предложения и сделки</b>\n\n"
        "Создайте предложение с описанием условий, затем отправьте карточку второму участнику.\n\n"
        f"{custom_emoji(EMOJI_SAFE_DEAL, '🛡️')} <b>Важно:</b> бот не принимает, не хранит и не переводит деньги. "
        "Он не является эскроу-сервисом и не подтверждает факт оплаты."
        + warning_footer()
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Создать предложение", callback_data="create_offer")],
            [InlineKeyboardButton("Безопасная сделка", callback_data="safe_deal")],
            [InlineKeyboardButton("Как это работает", callback_data="how_it_works")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Отмена", callback_data="cancel_offer")]]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Назад в меню", callback_data="main_menu")]]
    )


async def safe_edit_text(query: Any, text: str, **kwargs: Any) -> bool:
    """Edit the existing bot message in place; never delete/repost for menu navigation."""
    if query.message is None:
        return False
    try:
        await query.edit_message_text(text=text, **kwargs)
        return True
    except BadRequest as exc:
        # Telegram returns this when the content already matches; that's not a real failure.
        if "message is not modified" in str(exc).lower():
            return True
        logger.info("Could not edit current message: %s", exc)
        return False
    except TelegramError as exc:
        logger.warning("Could not edit current message: %s", exc)
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_offer", None)
    context.user_data.pop("flow_message_id", None)
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            welcome_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "Нажмите «Создать предложение» и отправьте текст с условиями. "
            "Бот создаст карточку с кнопками подтверждения и отклонения.\n\n"
            "Подтверждение означает только согласие с текстом карточки. Оно не доказывает оплату. "
            "Не сообщайте коды входа, пароли или данные банковских карт."
        )


async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    data = query.data

    if data == "create_offer":
        context.user_data["awaiting_offer"] = True
        if query.message is not None:
            context.user_data["flow_chat_id"] = query.message.chat_id
            context.user_data["flow_message_id"] = query.message.message_id
        await query.answer()
        edited = await safe_edit_text(
            query,
            "<b>Создание предложения</b>\n\n"
            "Отправьте одним сообщением описание: что предлагается и какие условия.\n\n"
            "Не указывайте пароли, коды подтверждения или полные данные банковских карт."
            + warning_footer(),
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_keyboard(),
            disable_web_page_preview=True,
        )
        if not edited:
            # Fallback only if Telegram does not permit editing the current message.
            if query.message is not None:
                sent = await query.message.reply_text(
                    "Отправьте одним сообщением описание предложения. Для отмены используйте /cancel."
                    + warning_footer(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=cancel_keyboard(),
                )
                context.user_data["flow_chat_id"] = sent.chat_id
                context.user_data["flow_message_id"] = sent.message_id
        return

    if data == "main_menu":
        context.user_data.pop("awaiting_offer", None)
        context.user_data.pop("flow_chat_id", None)
        context.user_data.pop("flow_message_id", None)
        await query.answer()
        await safe_edit_text(
            query,
            welcome_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "safe_deal":
        await query.answer(
            "Бот не хранит деньги и не гарантирует исполнение сделки. Самостоятельно проверяйте оплату "
            "в официальном приложении и не доверяйте скриншотам переводов.",
            show_alert=True,
        )
        return

    if data == "how_it_works":
        await query.answer()
        await safe_edit_text(
            query,
            "<b>Как это работает</b>\n\n"
            "1. Создайте карточку предложения.\n"
            "2. Передайте карточку второму участнику.\n"
            "3. Участник может подтвердить условия или отклонить предложение.\n\n"
            "Подтверждение означает только согласие с текстом карточки. Оно не доказывает оплату, "
            "подлинность товара или выполнение обязательств."
            + warning_footer(),
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "cancel_offer":
        context.user_data.pop("awaiting_offer", None)
        context.user_data.pop("flow_chat_id", None)
        context.user_data.pop("flow_message_id", None)
        await query.answer("Создание предложения отменено.")
        await safe_edit_text(
            query,
            welcome_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )
        return

    parts = data.split(":", maxsplit=1)
    if len(parts) != 2 or parts[0] not in {"confirm", "reject", "safe"}:
        await query.answer("Неизвестное действие.", show_alert=True)
        return

    action, creator_text = parts
    try:
        creator_id = int(creator_text)
    except ValueError:
        await query.answer("Некорректное предложение.", show_alert=True)
        return

    if action == "safe":
        await query.answer(
            "Этот бот не является гарантом или эскроу-сервисом. Деньги через него не проходят.",
            show_alert=True,
        )
        return

    actor = query.from_user
    if actor.id == creator_id:
        await query.answer(
            "Создатель предложения не может подтвердить или отклонить его сам.",
            show_alert=True,
        )
        return

    message = query.message
    if message is None:
        await query.answer("Сообщение недоступно.", show_alert=True)
        return

    offer_key = f"{message.chat_id}:{message.message_id}"
    states: dict[str, dict[str, Any]] = context.bot_data.setdefault("offer_states", {})
    if offer_key in states:
        await query.answer("Для этого предложения действие уже зарегистрировано.", show_alert=True)
        return

    states[offer_key] = {"action": action, "user_id": actor.id}
    display_name = html.escape(actor.full_name or actor.username or "Участник")
    if actor.username:
        display_name += f" (@{html.escape(actor.username)})"

    if action == "confirm":
        status = (
            f"\n\n{custom_emoji(EMOJI_CONFIRM, '✅')} <b>Условия подтверждены</b>\n"
            f"Участник: {display_name}\n"
            "Это не подтверждает оплату или передачу товара."
        )
        answer_text = "Подтверждение зарегистрировано."
    else:
        status = f"\n\n<b>Предложение отклонено</b>\nУчастник: {display_name}"
        answer_text = "Отказ зарегистрирован."

    await query.answer(answer_text)
    # Update the offer card itself, removing its buttons without posting a second status message.
    cards: dict[str, str] = context.bot_data.setdefault("offer_cards", {})
    original_html = cards.get(offer_key)
    if original_html:
        final_html = original_html + status
    else:
        # Fallback for old messages created before a process restart.
        final_html = html.escape(message.text or "Предложение") + status

    await safe_edit_text(
        query,
        final_html,
        parse_mode=ParseMode.HTML,
        reply_markup=None,
        disable_web_page_preview=True,
    )


async def receive_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_offer"):
        return

    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return

    description = message.text.strip()
    if description.lower() == "/cancel":
        context.user_data.pop("awaiting_offer", None)
        context.user_data.pop("flow_chat_id", None)
        context.user_data.pop("flow_message_id", None)
        await message.reply_text("Создание предложения отменено.", reply_markup=main_keyboard())
        return

    if len(description) > MAX_OFFER_LENGTH:
        await message.reply_text(
            f"Слишком длинный текст. Максимум — {MAX_OFFER_LENGTH} символов. Сократите описание и отправьте снова."
        )
        return

    context.user_data.pop("awaiting_offer", None)
    creator_name = html.escape(user.full_name or user.username or "Участник")
    if user.username:
        creator_name += f" (@{html.escape(user.username)})"

    card = (
        f"<b>{custom_emoji(EMOJI_OFFER, '📨')} Предложение</b>\n\n"
        f"<b>Описание:</b>\n{html.escape(description)}\n\n"
        f"<b>Создатель:</b> {creator_name}\n"
        f"{custom_emoji(EMOJI_CONFIRM, '✅')} <b>Подтверждение:</b> ожидается\n"
        f"{custom_emoji(EMOJI_SAFE_DEAL, '🛡️')} <b>Безопасная сделка:</b> проверяйте условия самостоятельно\n"
        f"{custom_emoji(EMOJI_STARS, '⭐')} <b>Рейтинг:</b> не указан"
        + warning_footer()
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Подтвердить условия", callback_data=f"confirm:{user.id}"),
                InlineKeyboardButton("Отклонить", callback_data=f"reject:{user.id}"),
            ],
            [InlineKeyboardButton("Безопасная сделка — информация", callback_data=f"safe:{user.id}")],
        ]
    )

    # The card is the one expected new message. No additional "card created" message is sent.
    try:
        sent_card = await message.reply_text(
            card,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        logger.exception("Could not send offer card: %s", exc)
        await message.reply_text("Не удалось создать карточку. Попробуйте ещё раз через /start.")
        return

    key = f"{sent_card.chat_id}:{sent_card.message_id}"
    cards: dict[str, str] = context.bot_data.setdefault("offer_cards", {})
    cards[key] = card

    # Reuse the existing menu/prompt message as a compact status, rather than deleting it.
    flow_chat_id = context.user_data.pop("flow_chat_id", None)
    flow_message_id = context.user_data.pop("flow_message_id", None)
    if flow_chat_id and flow_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=flow_chat_id,
                message_id=flow_message_id,
                text="Предложение создано. Карточка отправлена выше.\n\n" + warning_footer(),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.info("Could not update flow message in place: %s", exc)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_offer", None)
    context.user_data.pop("flow_chat_id", None)
    context.user_data.pop("flow_message_id", None)
    message = update.effective_message
    if message is not None:
        await message.reply_text("Создание предложения отменено.", reply_markup=main_keyboard())


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    token = os.getenv("BOT_TOKEN", "").strip()
    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    base_url = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")

    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Add your Telegram bot token in Render Environment.")
    if not secret or len(secret) < 8:
        raise RuntimeError("Set WEBHOOK_SECRET in Render Environment (at least 8 characters; use letters, digits, _ or -).")
    if not base_url.startswith("https://"):
        raise RuntimeError("RENDER_EXTERNAL_URL was not found. Set WEBHOOK_URL to your Render HTTPS service URL.")

    telegram_app = Application.builder().token(token).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("cancel", cancel_command))
    telegram_app.add_handler(CallbackQueryHandler(button_router))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_offer))

    await telegram_app.initialize()
    await telegram_app.start()
    webhook_url = f"{base_url}{WEBHOOK_PATH}"
    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    fastapi_app.state.webhook_secret = secret
    fastapi_app.state.telegram_app = telegram_app
    logger.info("Telegram webhook configured at %s", webhook_url)
    try:
        yield
    finally:
        await telegram_app.bot.delete_webhook(drop_pending_updates=False)
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="NEZZX Telegram Bot", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "NEZZX Telegram Bot", "status": "running"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    expected_secret = getattr(request.app.state, "webhook_secret", "")
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected_secret or supplied_secret != expected_secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    tg_app: Application = request.app.state.telegram_app
    try:
        payload = await request.json()
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
    except Exception as exc:
        logger.exception("Failed to process update: %s", exc)
        return JSONResponse(status_code=500, content={"ok": False})
    return {"ok": True}
