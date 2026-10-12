import html
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    BusinessConnectionHandler,
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
    # The private bot is only an entry point; deals are handled in Business chats.
    return "Сделки тут."


def main_keyboard() -> InlineKeyboardMarkup | None:
    # No deal-creation menu in the bot's private chat.
    return None


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
    # Business messages must be edited in the context of their connection.
    business_connection_id = getattr(query.message, "business_connection_id", None)
    if business_connection_id and "business_connection_id" not in kwargs:
        kwargs["business_connection_id"] = business_connection_id
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
    context.user_data.pop("awaiting_business_offer", None)
    context.user_data.pop("business_connection_id", None)
    context.user_data.pop("flow_message_id", None)
    message = update.effective_message
    if message is not None:
        await message.reply_text("Сделки тут.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text("Сделки оформляются в личном чате через подключённый Telegram Business.")


async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    data = query.data

    if data == "create_offer":
        is_business_message = bool(
            query.message is not None
            and getattr(query.message, "business_connection_id", None)
        )
        if is_business_message:
            context.user_data.pop("awaiting_offer", None)
            context.user_data["awaiting_business_offer"] = True
            context.user_data["business_connection_id"] = query.message.business_connection_id
        else:
            context.user_data.pop("awaiting_business_offer", None)
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
                fallback_text = (
                    "Отправьте одним сообщением описание предложения. Для отмены используйте /cancel."
                    + warning_footer()
                )
                if is_business_message:
                    sent = await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        business_connection_id=query.message.business_connection_id,
                        text=fallback_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=cancel_keyboard(),
                    )
                else:
                    sent = await query.message.reply_text(
                        fallback_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=cancel_keyboard(),
                    )
                context.user_data["flow_chat_id"] = sent.chat_id
                context.user_data["flow_message_id"] = sent.message_id
        return

    if data == "main_menu":
        context.user_data.pop("awaiting_offer", None)
        context.user_data.pop("awaiting_business_offer", None)
        context.user_data.pop("business_connection_id", None)
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
        context.user_data.pop("awaiting_business_offer", None)
        context.user_data.pop("business_connection_id", None)
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

    # Compact NFT purchase workflow: only the connected business-account owner
    # may accept or reject a buyer's offer.
    if data.startswith(("nft_accept:", "nft_reject:")):
        try:
            action, seller_text, buyer_text = data.split(":", maxsplit=2)
            seller_id = int(seller_text)
            buyer_id = int(buyer_text)
        except (ValueError, TypeError):
            await query.answer("Предложение некорректно.", show_alert=True)
            return

        actor = query.from_user
        # seller_id == 0 means this card was generated in the bot's ordinary
        # private chat as a visual preview, where no Business owner can be inferred.
        # In that mode, the buyer cannot accept their own offer; a different account
        # may operate the buttons if the card is forwarded/shared.
        if seller_id and actor.id != seller_id:
            await query.answer("Решение может принять только продавец.", show_alert=True)
            return
        if not seller_id and actor.id == buyer_id:
            await query.answer("Покупатель не может принять собственное предложение.", show_alert=True)
            return

        message = query.message
        if message is None:
            await query.answer("Сообщение недоступно.", show_alert=True)
            return

        offer_key = f"{message.chat_id}:{message.message_id}"
        states: dict[str, dict[str, Any]] = context.bot_data.setdefault("offer_states", {})
        if offer_key in states:
            await query.answer("По этому предложению уже принято решение.", show_alert=True)
            return

        states[offer_key] = {"action": action, "seller_id": seller_id, "buyer_id": buyer_id}
        if action == "nft_accept":
            result_text = (
                "<b>Предложение принято</b>\n\n"
                "Откройте меню подарков Telegram и выберите покупателя вручную. "
                "Бот не может сам подтвердить или выполнить передачу коллекционного NFT."
            )
            result_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Открыть меню подарков", url="tg://settings/send-gift")]
            ])
            await query.answer("Предложение принято")
        else:
            result_text = "<b>Предложение отклонено</b>"
            result_keyboard = None
            await query.answer("Предложение отклонено")

        await safe_edit_text(
            query,
            result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=result_keyboard,
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
        await message.reply_text("Отменено.")
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


async def direct_nft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept .nft in the bot's own private chat as well as Business chats.

    A plain bot DM has no Business connection, so it can only create the offer card
    inside that bot chat. The connected-account workflow remains handled separately
    by business_message_reply().
    """
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return

    text = message.text.strip()
    if not re.search(r"(?i)(?:^|\s)\.nft(?:\s|$)", text):
        return

    parsed = parse_nft_command(text)
    if not parsed:
        await message.reply_text("Формат команды: .nft 1000 t.me/nft/LibertyFigure-14469")
        return

    configured_seller = os.getenv("SELLER_TELEGRAM_ID", "").strip()
    try:
        seller_id = int(configured_seller) if configured_seller else 0
    except ValueError:
        logger.warning("SELLER_TELEGRAM_ID is invalid; using preview mode for direct bot chats")
        seller_id = 0
    if seller_id == user.id:
        seller_id = 0

    username = getattr(user, "username", None)
    first_name = getattr(user, "first_name", None)
    buyer_name = f"@{username}" if username else (first_name or f"Покупатель {user.id}")
    logger.info("Direct .nft command received: chat_id=%s user_id=%s", message.chat_id, user.id)
    await handle_nft_command(
        message, context, None, parsed[0], parsed[1], seller_id, user.id, buyer_name
    )


async def business_connection_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remember connection details and log whether Telegram permits replies."""
    connection = update.business_connection
    if connection is None:
        return

    rights = getattr(connection, "rights", None)
    can_reply = getattr(rights, "can_reply", None) if rights is not None else None
    connections: dict[str, dict[str, Any]] = context.bot_data.setdefault("business_connections", {})
    connections[connection.id] = {
        "user_id": connection.user.id,
        "user_name": connection.user.full_name,
        "is_enabled": connection.is_enabled,
        "can_reply": can_reply,
    }
    logger.info(
        "Business connection update received: id=%s enabled=%s owner_id=%s can_reply=%s",
        connection.id,
        connection.is_enabled,
        connection.user.id,
        can_reply,
    )


async def _get_business_connection_info(
    connection_id: str, context: ContextTypes.DEFAULT_TYPE
) -> dict[str, Any] | None:
    """Use cached connection data; refresh it from Telegram when not cached."""
    connections: dict[str, dict[str, Any]] = context.bot_data.setdefault("business_connections", {})
    info = connections.get(connection_id)
    if info is not None:
        return info
    try:
        connection = await context.bot.get_business_connection(connection_id)
        rights = getattr(connection, "rights", None)
        info = {
            "user_id": connection.user.id,
            "user_name": connection.user.full_name,
            "is_enabled": connection.is_enabled,
            "can_reply": getattr(rights, "can_reply", None) if rights is not None else None,
        }
        connections[connection_id] = info
        logger.info(
            "Business connection details refreshed: id=%s enabled=%s can_reply=%s",
            connection_id,
            info["is_enabled"],
            info["can_reply"],
        )
        return info
    except (AttributeError, TelegramError) as exc:
        logger.warning("Could not refresh Business connection %s: %s", connection_id, exc)
        return None


async def _finish_business_offer(
    message: Any, context: ContextTypes.DEFAULT_TYPE, connection_id: str
) -> None:
    """Create the offer card as a message sent on behalf of the connected Business account."""
    description = (message.text or "").strip()
    if description.lower() == "/cancel":
        context.user_data.pop("awaiting_business_offer", None)
        context.user_data.pop("business_connection_id", None)
        await context.bot.send_message(
            chat_id=message.chat_id,
            business_connection_id=connection_id,
            text="Создание предложения отменено.",
            reply_markup=main_keyboard(),
        )
        return

    if len(description) > MAX_OFFER_LENGTH:
        await context.bot.send_message(
            chat_id=message.chat_id,
            business_connection_id=connection_id,
            text=f"Слишком длинный текст. Максимум — {MAX_OFFER_LENGTH} символов. Сократите описание и отправьте снова.",
        )
        return

    connection_info = await _get_business_connection_info(connection_id, context)
    owner_id = int(connection_info["user_id"]) if connection_info and connection_info.get("user_id") else 0
    owner_name = html.escape(str(connection_info.get("user_name") or "Бизнес-аккаунт")) if connection_info else "Бизнес-аккаунт"
    card = (
        f"<b>{custom_emoji(EMOJI_OFFER, '📨')} Предложение</b>\n\n"
        f"<b>Описание:</b>\n{html.escape(description)}\n\n"
        f"<b>Отправитель:</b> {owner_name}\n"
        f"{custom_emoji(EMOJI_CONFIRM, '✅')} <b>Подтверждение:</b> ожидается\n"
        f"{custom_emoji(EMOJI_SAFE_DEAL, '🛡️')} <b>Безопасная сделка:</b> проверяйте условия самостоятельно\n"
        f"{custom_emoji(EMOJI_STARS, '⭐')} <b>Рейтинг:</b> не указан"
        + warning_footer()
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Подтвердить условия", callback_data=f"confirm:{owner_id}"),
                InlineKeyboardButton("Отклонить", callback_data=f"reject:{owner_id}"),
            ],
            [InlineKeyboardButton("Безопасная сделка — информация", callback_data=f"safe:{owner_id}")],
        ]
    )
    try:
        sent_card = await context.bot.send_message(
            chat_id=message.chat_id,
            business_connection_id=connection_id,
            text=card,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        logger.exception("Could not send Business offer card: %s", exc)
        await context.bot.send_message(
            chat_id=message.chat_id,
            business_connection_id=connection_id,
            text="Не получилось отправить карточку. Проверьте в настройках Telegram Business право бота отвечать на сообщения.",
        )
        return

    key = f"{sent_card.chat_id}:{sent_card.message_id}"
    cards: dict[str, str] = context.bot_data.setdefault("offer_cards", {})
    cards[key] = card
    context.user_data.pop("awaiting_business_offer", None)
    context.user_data.pop("business_connection_id", None)

    # Update the existing prompt in-place where Telegram allows it.
    flow_chat_id = context.user_data.pop("flow_chat_id", None)
    flow_message_id = context.user_data.pop("flow_message_id", None)
    if flow_chat_id and flow_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=flow_chat_id,
                message_id=flow_message_id,
                business_connection_id=connection_id,
                text="Предложение создано. Карточка отправлена выше.\n\n" + warning_footer(),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.info("Could not update Business prompt in place: %s", exc)


def parse_nft_command(text: str) -> tuple[str, str] | None:
    """Parse `.nft <amount> <URL>`, including bare t.me/nft/... links."""
    # Search a whole command line so a heading like "NEZZX NFT СКУП:" before it
    # doesn't break parsing. Accept both `https://t.me/...` and `t.me/...`.
    match = re.search(
        r"(?im)^\s*\.nft\s+(\d+(?:[.,]\d{1,2})?)\s+(\S+)\s*$",
        text.strip(),
    )
    if not match:
        return None

    amount = match.group(1).replace(",", ".")
    raw_url = match.group(2).rstrip(".,;)")
    if raw_url.lower().startswith(("t.me/", "www.t.me/", "telegram.me/", "www.telegram.me/")):
        raw_url = "https://" + raw_url

    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(raw_url)
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.path:
        return None
    # Reject malformed host strings and embedded credentials; don't accept javascript: etc.
    host = (parsed.hostname or "").lower()
    if not host or parsed.username or parsed.password:
        return None
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        return None

    return amount, raw_url


def _nft_details(url: str) -> tuple[str, str]:
    """Return a readable Telegram collectible name and number where available."""
    from urllib.parse import unquote, urlsplit

    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path_parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if host in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"} and len(path_parts) >= 2 and path_parts[0].lower() == "nft":
        slug = path_parts[1]
        found = re.fullmatch(r"(.+?)-(\d+)", slug)
        if found:
            raw_name, serial = found.groups()
            raw_name = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw_name)
            raw_name = re.sub(r"[_-]+", " ", raw_name).strip()
            return raw_name or "Telegram NFT", f"#{serial}"
        return slug or "Telegram NFT", "не указан"
    return "NFT по ссылке", "не указан"


def _format_amount(amount: str) -> str:
    try:
        number = float(amount)
        if number.is_integer():
            return f"{int(number):,}".replace(",", " ")
    except (TypeError, ValueError):
        pass
    return amount


async def handle_nft_command(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    connection_id: str | None,
    amount: str,
    url: str,
    seller_id: int,
    buyer_id: int,
    buyer_name: str,
) -> bool:
    """Send a short NFT purchase offer on behalf of the connected Business account."""
    from urllib.parse import urlsplit

    safe_url = html.escape(url, quote=True)
    collectible_name, collectible_number = _nft_details(url)
    display_amount = html.escape(_format_amount(amount))
    safe_buyer = html.escape(buyer_name or "Покупатель")
    host = (urlsplit(url).hostname or "").lower()
    is_telegram_nft = host in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}

    # Compact, chat-generated offer card. A normal HTML version is kept as a
    # fallback because Telegram may reject custom-emoji entities for a bot.
    card = (
        f"<b>{safe_buyer}</b> хочет купить ваш подарок за "
        f"<b>{custom_emoji(EMOJI_STARS, '⭐')} {display_amount}</b> звёзд\n\n"
        f"<b>{html.escape(collectible_name)}</b> · {html.escape(collectible_number)}\n"
        f"<a href=\"{safe_url}\">Ссылка на подарок</a>"
    )
    fallback_card = (
        f"<b>{safe_buyer}</b> хочет купить ваш подарок за "
        f"<b>⭐ {display_amount}</b> звёзд\n\n"
        f"<b>{html.escape(collectible_name)}</b> · {html.escape(collectible_number)}\n"
        f"<a href=\"{safe_url}\">Ссылка на подарок</a>"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Принять", callback_data=f"nft_accept:{seller_id}:{buyer_id}"),
            InlineKeyboardButton("Отклонить", callback_data=f"nft_reject:{seller_id}:{buyer_id}"),
        ]
    ])

    sent = None
    sent_card = card
    try:
        send_kwargs: dict[str, Any] = {
            "chat_id": message.chat_id,
            "text": card,
            "parse_mode": ParseMode.HTML,
            "reply_markup": keyboard,
            "disable_web_page_preview": True,
        }
        if connection_id:
            send_kwargs["business_connection_id"] = connection_id
        sent = await context.bot.send_message(**send_kwargs)
    except TelegramError as rich_exc:
        # Most common recoverable failure is a rejected custom-emoji entity.
        # Retry the same offer card without custom-emoji markup; don't show a
        # misleading permission warning when a plain card can still be sent.
        logger.warning("Rich NFT card failed; retrying plain card: %s", rich_exc)
        try:
            sent_card = fallback_card
            send_kwargs["text"] = fallback_card
            send_kwargs["parse_mode"] = ParseMode.HTML
            sent = await context.bot.send_message(**send_kwargs)
        except TelegramError as fallback_exc:
            logger.exception("Could not send NFT card even without custom emoji: %s", fallback_exc)
            # Don't mislabel every API error as a permission problem. Log the
            # technical cause and keep the customer-facing message neutral.
            try:
                failure_kwargs: dict[str, Any] = {
                    "chat_id": message.chat_id,
                    "text": "Не получилось отправить карточку предложения. Команда оставлена в чате — попробуйте ещё раз.",
                }
                if connection_id:
                    failure_kwargs["business_connection_id"] = connection_id
                await context.bot.send_message(**failure_kwargs)
            except TelegramError:
                logger.exception("Could not send NFT card failure notice")
            return False

    if sent is not None:
        key = f"{sent.chat_id}:{sent.message_id}"
        cards: dict[str, str] = context.bot_data.setdefault("offer_cards", {})
        cards[key] = sent_card
        offers: dict[str, dict[str, Any]] = context.bot_data.setdefault("nft_offers", {})
        offers[key] = {
            "seller_id": seller_id,
            "buyer_id": buyer_id,
            "buyer_name": buyer_name,
            "amount": amount,
            "url": url,
            "business_connection_id": connection_id,
            "is_telegram_nft": is_telegram_nft,
        }
        logger.info("NFT offer card sent: chat_id=%s message_id=%s buyer_id=%s", sent.chat_id, sent.message_id, buyer_id)
        return True
    return False


async def business_message_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process normal text in Telegram Business chats and reply on behalf of the account."""
    message = update.business_message
    if message is None or not message.business_connection_id or not message.text:
        return

    connection_id = message.business_connection_id
    text = message.text.strip()
    logger.info(
        "Incoming Business text update: connection_id=%s chat_id=%s text_length=%s",
        connection_id,
        message.chat_id,
        len(text),
    )

    connection_info = await _get_business_connection_info(connection_id, context)
    if connection_info and not connection_info.get("is_enabled", True):
        logger.warning("Ignoring Business message because connection is disabled: %s", connection_id)
        return
    if connection_info and connection_info.get("can_reply") is False:
        # Do not silently discard the command. We still attempt the send below; if
        # Telegram rejects it, the command remains visible and the exact API error
        # is written to Render logs.
        logger.warning(
            "Business message received while can_reply is false; attempting card send anyway: connection_id=%s",
            connection_id,
        )

    sender = message.from_user
    is_owner_message = bool(sender and connection_info and sender.id == connection_info.get("user_id"))
    if getattr(message, "sender_business_bot", False):
        return

    parsed_nft = parse_nft_command(text)

    # The BUYER sends `.nft <price> <gift link>` in the private chat with
    # the connected Business account. The seller is the Business account owner.
    if not is_owner_message and parsed_nft:
        if not sender:
            logger.warning("Cannot process buyer NFT command: Business message has no sender user")
            return

        buyer_id = int(sender.id)
        # Prefer the live Business connection owner; if Telegram does not return
        # connection metadata after a restart, use the optional configured seller ID.
        # Zero keeps the offer card renderable instead of silently dropping it.
        owner_value = connection_info.get("user_id") if connection_info else None
        if not owner_value:
            owner_value = os.getenv("SELLER_TELEGRAM_ID", "").strip()
        try:
            seller_id = int(owner_value) if owner_value else 0
        except (TypeError, ValueError):
            logger.warning("SELLER_TELEGRAM_ID is invalid; offer will use preview button permissions")
            seller_id = 0
        buyer_username = getattr(sender, "username", None)
        buyer_first_name = getattr(sender, "first_name", None)
        buyer_name = f"@{buyer_username}" if buyer_username else (buyer_first_name or f"Покупатель {buyer_id}")

        # Send the card first. Never remove the buyer's command unless the card
        # was successfully created; otherwise a temporary API/permission problem
        # would leave the chat with neither the command nor a card.
        sent_ok = await handle_nft_command(
            message, context, connection_id, *parsed_nft,
            seller_id, buyer_id, buyer_name,
        )
        if sent_ok:
            try:
                await context.bot.delete_business_messages(
                    business_connection_id=connection_id,
                    message_ids=[message.message_id],
                )
            except (AttributeError, TelegramError) as exc:
                logger.warning("Could not delete buyer's .nft command in Business chat: %s", exc)
        return

    # Don't answer the owner's outgoing messages, and don't send an away reply
    # to malformed `.nft` attempts from the buyer.
    if is_owner_message:
        return
    if re.search(r"(?i)(?:^|\s)\.nft(?:\s|$)", text):
        try:
            await context.bot.send_message(
                chat_id=message.chat_id,
                business_connection_id=connection_id,
                text="Формат: .nft 3500 t.me/nft/LibertyFigure-14469",
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.warning("Could not send NFT command format hint: %s", exc)
        return

    # Away reply for ordinary incoming messages. A real Premium sticker must be
    # configured via PREMIUM_STICKER_FILE_ID (Telegram sticker file_id), since a
    # custom emoji ID is not a sticker file_id. Never send the generic menu reply.
    try:
        sticker_id = os.getenv("PREMIUM_STICKER_FILE_ID", "").strip()
        if sticker_id:
            try:
                await context.bot.send_sticker(
                    chat_id=message.chat_id,
                    sticker=sticker_id,
                    business_connection_id=connection_id,
                )
            except TelegramError as sticker_exc:
                logger.warning("Could not send configured away sticker: %s", sticker_exc)

        await context.bot.send_message(
            chat_id=message.chat_id,
            business_connection_id=connection_id,
            text="Привет... Незкс сейчас вернётся, либо он спит, а может быть и занят... Просто жди.",
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        logger.exception("Could not send Business away reply: %s", exc)


async def show_sticker_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the file_id for a sticker sent to the bot in its private chat."""
    message = update.effective_message
    if message is None or message.sticker is None:
        return
    await message.reply_text(
        "STICKER_FILE_ID для Render:\n" + message.sticker.file_id,
        disable_web_page_preview=True,
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_offer", None)
    context.user_data.pop("awaiting_business_offer", None)
    context.user_data.pop("business_connection_id", None)
    context.user_data.pop("flow_chat_id", None)
    context.user_data.pop("flow_message_id", None)
    message = update.effective_message
    if message is not None:
        await message.reply_text("Отменено.")


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
    # Use dedicated Business handlers. A catch-all TypeHandler(Update) is too broad
    # and makes it harder to reason about handler priority in this application.
    telegram_app.add_handler(BusinessConnectionHandler(business_connection_update))
    telegram_app.add_handler(
        MessageHandler(filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT, business_message_reply)
    )
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("cancel", cancel_command))
    telegram_app.add_handler(MessageHandler(filters.Sticker.ALL, show_sticker_file_id))
    telegram_app.add_handler(CallbackQueryHandler(button_router))
    # `.nft` is not a Telegram slash-command, so it needs a text handler in the
    # bot's own private chat. Business-chat commands are handled above.
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, direct_nft_command))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_offer))

    await telegram_app.initialize()
    await telegram_app.start()
    webhook_url = f"{base_url}{WEBHOOK_PATH}"
    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        allowed_updates=Update.ALL_TYPES,
        # Do not silently discard Business messages waiting during a Render restart/sleep.
        drop_pending_updates=False,
    )
    fastapi_app.state.webhook_secret = secret
    fastapi_app.state.telegram_app = telegram_app
    logger.info("Telegram webhook configured at %s", webhook_url)
    try:
        yield
    finally:
        # Keep the webhook registered across Render restarts. Deleting it during every
        # shutdown creates a delivery gap and can make Business chats appear unresponsive.
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
        update_keys = [key for key, value in payload.items() if value is not None]
        logger.info("Webhook update received: update_id=%s keys=%s", payload.get("update_id"), update_keys)
        update = Update.de_json(payload, tg_app.bot)
        await tg_app.process_update(update)
    except Exception as exc:
        logger.exception("Failed to process update: %s", exc)
        return JSONResponse(status_code=500, content={"ok": False})
    return {"ok": True}
