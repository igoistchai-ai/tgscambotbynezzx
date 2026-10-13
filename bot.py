"""
NEZZX Deal Bot — rewrite
env vars:
  BOT_TOKEN            required
  WEBHOOK_SECRET       required, >= 8 chars
  WEBHOOK_URL          required, https://...
  PREMIUM_STICKER_FILE_ID  optional
"""

import html
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("nezzx-bot")

# ─── Constants ──────────────────────────────────────────────────────────────

WEBHOOK_PATH = "/telegram/webhook"
MAX_OFFER_LEN = 1500

# Custom emoji IDs (Telegram Premium)
E_OFFER   = "5330237710655306682"
E_CONFIRM = "5206607081334906820"
E_SHIELD  = "5251203410396458957"
E_STARS   = "5438496463044752972"
E_WARN    = "5251340119205501791"

TG_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}


def em(eid: str, fb: str) -> str:
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'


# ─── Texts ──────────────────────────────────────────────────────────────────

def _warn() -> str:
    return (
        f"\n\n{em(E_WARN,'⚠️')} <b>Внимание!</b> "
        "Не доверяйте незнакомым участникам и проверяйте оплату самостоятельно. "
        "Telegram не гарантирует сделки через этого бота."
    )


def _welcome() -> str:
    return (
        "<b>NEZZX — предложения и сделки</b>\n\n"
        "Создайте предложение с описанием условий и отправьте карточку второму участнику.\n\n"
        f"{em(E_SHIELD,'🛡️')} <b>Важно:</b> бот не принимает, не хранит и не переводит деньги."
        + _warn()
    )


# ─── Keyboards ──────────────────────────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Создать предложение", callback_data="create_offer")],
        [InlineKeyboardButton("Безопасная сделка",   callback_data="safe_deal")],
        [InlineKeyboardButton("Как это работает",    callback_data="how_it_works")],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Отмена", callback_data="cancel_offer")]
    ])


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Назад в меню", callback_data="main_menu")]
    ])


def kb_offer(creator_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Подтвердить условия", callback_data=f"confirm:{creator_id}"),
            InlineKeyboardButton("Отклонить",           callback_data=f"reject:{creator_id}"),
        ],
        [InlineKeyboardButton("Безопасная сделка — инфо", callback_data=f"safe:{creator_id}")],
    ])


def kb_nft(seller_id: int, buyer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Принять",   callback_data=f"nft_accept:{seller_id}:{buyer_id}"),
            InlineKeyboardButton("Отклонить", callback_data=f"nft_reject:{seller_id}:{buyer_id}"),
        ]
    ])


# ─── NFT helpers ────────────────────────────────────────────────────────────

def parse_nft(text: str) -> tuple[str, str] | None:
    """Return (amount, url) from `.nft <amount> <url>` or None."""
    m = re.search(r"(?im)^\s*\.nft\s+(\d+(?:[.,]\d{1,2})?)\s+(\S+)\s*$", text.strip())
    if not m:
        return None

    amount = m.group(1).replace(",", ".")
    raw = m.group(2).rstrip(".,;)")

    if raw.lower().startswith(("t.me/", "www.t.me/", "telegram.me/", "www.telegram.me/")):
        raw = "https://" + raw

    try:
        p = urlsplit(raw)
    except ValueError:
        return None

    host = (p.hostname or "").lower()
    if p.scheme.lower() not in {"http", "https"} or not host or not p.path:
        return None
    if p.username or p.password or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None

    return amount, raw


def nft_details(url: str) -> tuple[str, str]:
    """(collectible_name, '#number') parsed from a t.me/nft/... URL."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    parts = [unquote(x) for x in p.path.strip("/").split("/") if x]

    if host in TG_HOSTS and len(parts) >= 2 and parts[0].lower() == "nft":
        slug = parts[1]
        m = re.fullmatch(r"(.+?)-(\d+)", slug)
        if m:
            name, serial = m.groups()
            name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
            name = re.sub(r"[_-]+", " ", name).strip()
            return name or "Telegram NFT", f"#{serial}"
        return slug or "Telegram NFT", ""
    return "NFT", ""


def fmt_amount(amount: str) -> str:
    try:
        n = float(amount)
        return f"{int(n):,}".replace(",", " ") if n.is_integer() else amount
    except (ValueError, TypeError):
        return amount


def nft_card(buyer_name: str, amount: str, url: str, with_emoji: bool = True) -> str:
    name, number = nft_details(url)
    safe_url = html.escape(url, quote=True)
    stars = em(E_STARS, "⭐") if with_emoji else "⭐"
    label = f"{html.escape(name)} · {html.escape(number)}" if number else html.escape(name)
    return (
        f"<b>{html.escape(buyer_name)}</b> хочет купить ваш подарок за "
        f"<b>{stars} {html.escape(fmt_amount(amount))}</b> звёзд\n\n"
        f"{label}\n"
        f'<a href="{safe_url}">Ссылка на подарок</a>'
    )


# ─── Business connection cache ───────────────────────────────────────────────

async def get_conn_info(connection_id: str, ctx: ContextTypes.DEFAULT_TYPE) -> dict | None:
    cache: dict = ctx.bot_data.setdefault("biz_connections", {})
    if connection_id in cache:
        return cache[connection_id]
    try:
        conn = await ctx.bot.get_business_connection(connection_id)
        rights = getattr(conn, "rights", None)
        info = {
            "user_id":    conn.user.id,
            "user_name":  conn.user.full_name,
            "is_enabled": conn.is_enabled,
            "can_reply":  getattr(rights, "can_reply", None) if rights else None,
        }
        cache[connection_id] = info
        return info
    except TelegramError as e:
        log.warning("Could not fetch business connection %s: %s", connection_id, e)
        return None


# ─── Safe send helpers ───────────────────────────────────────────────────────

async def safe_send(ctx: ContextTypes.DEFAULT_TYPE, *, chat_id: int,
                    connection_id: str | None = None, **kwargs) -> Any | None:
    """Send a message; return the sent Message or None on failure."""
    kw: dict = {"chat_id": chat_id, **kwargs}
    if connection_id:
        kw["business_connection_id"] = connection_id
    try:
        return await ctx.bot.send_message(**kw)
    except TelegramError as e:
        log.exception("send_message failed chat_id=%s conn=%s: %s", chat_id, connection_id, e)
        return None


async def safe_edit(query: Any, text: str, **kwargs) -> bool:
    try:
        await query.edit_message_text(text=text, **kwargs)
        return True
    except TelegramError as e:
        if "message is not modified" in str(e).lower():
            return True
        log.warning("edit_message_text failed: %s", e)
        return False


# ─── /start  /help  /cancel ──────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.clear()
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            _welcome(),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(),
            disable_web_page_preview=True,
        )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            "Нажмите «Создать предложение» и отправьте текст с условиями.\n\n"
            "Подтверждение означает только согласие с карточкой — не подтверждает оплату.\n"
            "Не сообщайте пароли, коды входа и данные карт.",
            disable_web_page_preview=True,
        )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.clear()
    msg = update.effective_message
    if msg:
        await msg.reply_text("Отменено.", reply_markup=kb_main())


# ─── Inline button router ────────────────────────────────────────────────────

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data

    # ── main menu navigation ─────────────────────────────────────────────────
    if data == "main_menu":
        ctx.user_data.clear()
        await query.answer()
        await safe_edit(query, _welcome(),
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_main(),
                        disable_web_page_preview=True)
        return

    if data == "how_it_works":
        await query.answer()
        await safe_edit(
            query,
            "<b>Как это работает</b>\n\n"
            "1. Создайте карточку предложения.\n"
            "2. Передайте карточку второму участнику.\n"
            "3. Участник подтверждает условия или отклоняет предложение.\n\n"
            "Подтверждение не доказывает оплату или передачу товара." + _warn(),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back(),
            disable_web_page_preview=True,
        )
        return

    if data == "safe_deal":
        await query.answer()
        await safe_edit(
            query,
            "<b>Безопасная сделка</b>\n\n"
            "Бот не является эскроу-сервисом и не принимает деньги. "
            "Он создаёт карточку с условиями, которую обе стороны могут подтвердить. "
            "Проверяйте условия самостоятельно." + _warn(),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back(),
            disable_web_page_preview=True,
        )
        return

    if data == "cancel_offer":
        ctx.user_data.clear()
        await query.answer("Отменено.")
        await safe_edit(query, _welcome(),
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_main(),
                        disable_web_page_preview=True)
        return

    if data == "create_offer":
        is_biz = bool(
            query.message and getattr(query.message, "business_connection_id", None)
        )
        ctx.user_data.clear()
        if is_biz:
            ctx.user_data["awaiting_biz_offer"] = True
            ctx.user_data["biz_conn_id"] = query.message.business_connection_id
        else:
            ctx.user_data["awaiting_offer"] = True

        if query.message:
            ctx.user_data["flow_chat_id"] = query.message.chat_id
            ctx.user_data["flow_msg_id"]  = query.message.message_id

        prompt = (
            "<b>Создание предложения</b>\n\n"
            "Отправьте одним сообщением описание: что предлагается и на каких условиях.\n\n"
            "Не указывайте пароли, коды подтверждения и данные банковских карт." + _warn()
        )
        await query.answer()
        ok = await safe_edit(query, prompt,
                             parse_mode=ParseMode.HTML,
                             reply_markup=kb_cancel(),
                             disable_web_page_preview=True)
        if not ok and query.message:
            conn_id = ctx.user_data.get("biz_conn_id")
            sent = await safe_send(
                ctx,
                chat_id=query.message.chat_id,
                connection_id=conn_id,
                text=prompt,
                parse_mode=ParseMode.HTML,
                reply_markup=kb_cancel(),
            )
            if sent:
                ctx.user_data["flow_chat_id"] = sent.chat_id
                ctx.user_data["flow_msg_id"]  = sent.message_id
        return

    # ── NFT accept / reject ──────────────────────────────────────────────────
    if data.startswith(("nft_accept:", "nft_reject:")):
        try:
            action, s_txt, b_txt = data.split(":", maxsplit=2)
            seller_id = int(s_txt)
            buyer_id  = int(b_txt)
        except (ValueError, TypeError):
            await query.answer("Некорректные данные.", show_alert=True)
            return

        actor = query.from_user
        if seller_id and actor.id != seller_id:
            await query.answer("Решение может принять только продавец.", show_alert=True)
            return
        if not seller_id and actor.id == buyer_id:
            await query.answer("Покупатель не может принять своё предложение.", show_alert=True)
            return

        msg = query.message
        if not msg:
            await query.answer("Сообщение недоступно.", show_alert=True)
            return

        key = f"{msg.chat_id}:{msg.message_id}"
        states: dict = ctx.bot_data.setdefault("offer_states", {})
        if key in states:
            await query.answer("По этому предложению уже принято решение.", show_alert=True)
            return
        states[key] = {"action": action}

        if action == "nft_accept":
            await query.answer("Предложение принято")
            await safe_edit(
                query,
                "<b>Предложение принято</b>\n\n"
                "Откройте меню подарков Telegram и выберите покупателя вручную.\n"
                "Бот не может сам передать коллекционный NFT.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Открыть меню подарков", url="tg://settings/send-gift")
                ]]),
                disable_web_page_preview=True,
            )
        else:
            await query.answer("Предложение отклонено")
            await safe_edit(query, "<b>Предложение отклонено</b>", parse_mode=ParseMode.HTML)
        return

    # ── Regular offer confirm / reject / safe ────────────────────────────────
    parts = data.split(":", maxsplit=1)
    if len(parts) != 2 or parts[0] not in {"confirm", "reject", "safe"}:
        await query.answer("Неизвестное действие.", show_alert=True)
        return

    action, creator_txt = parts
    try:
        creator_id = int(creator_txt)
    except ValueError:
        await query.answer("Некорректное предложение.", show_alert=True)
        return

    if action == "safe":
        await query.answer(
            "Бот не является эскроу-сервисом. Деньги через него не проходят.",
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

    msg = query.message
    if not msg:
        await query.answer("Сообщение недоступно.", show_alert=True)
        return

    key = f"{msg.chat_id}:{msg.message_id}"
    states: dict = ctx.bot_data.setdefault("offer_states", {})
    if key in states:
        await query.answer("Действие уже зарегистрировано.", show_alert=True)
        return
    states[key] = {"action": action, "user_id": actor.id}

    name = html.escape(actor.full_name or actor.username or "Участник")
    if actor.username:
        name += f" (@{html.escape(actor.username)})"

    if action == "confirm":
        suffix = (
            f"\n\n{em(E_CONFIRM,'✅')} <b>Условия подтверждены</b>\n"
            f"Участник: {name}\n"
            "Это не подтверждает оплату или передачу товара."
        )
        await query.answer("Подтверждение зарегистрировано.")
    else:
        suffix = f"\n\n<b>Предложение отклонено</b>\nУчастник: {name}"
        await query.answer("Отказ зарегистрирован.")

    cards: dict = ctx.bot_data.setdefault("offer_cards", {})
    original = cards.get(key, html.escape(msg.text or "Предложение"))
    await safe_edit(
        query, original + suffix,
        parse_mode=ParseMode.HTML,
        reply_markup=None,
        disable_web_page_preview=True,
    )


# ─── Regular offer creation (non-Business DM) ────────────────────────────────

async def receive_offer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.user_data.get("awaiting_offer"):
        return

    msg  = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text:
        return

    text = msg.text.strip()
    if text.lower() == "/cancel":
        ctx.user_data.clear()
        await msg.reply_text("Отменено.", reply_markup=kb_main())
        return

    if len(text) > MAX_OFFER_LEN:
        await msg.reply_text(
            f"Слишком длинный текст — максимум {MAX_OFFER_LEN} символов. Сократите и отправьте снова."
        )
        return

    ctx.user_data.pop("awaiting_offer", None)

    creator = html.escape(user.full_name or user.username or "Участник")
    if user.username:
        creator += f" (@{html.escape(user.username)})"

    card = (
        f"<b>{em(E_OFFER,'📨')} Предложение</b>\n\n"
        f"<b>Описание:</b>\n{html.escape(text)}\n\n"
        f"<b>Создатель:</b> {creator}\n"
        f"{em(E_CONFIRM,'✅')} <b>Подтверждение:</b> ожидается\n"
        f"{em(E_SHIELD,'🛡️')} <b>Безопасная сделка:</b> проверяйте условия самостоятельно"
        + _warn()
    )

    sent = await safe_send(
        ctx,
        chat_id=msg.chat_id,
        text=card,
        parse_mode=ParseMode.HTML,
        reply_markup=kb_offer(user.id),
        disable_web_page_preview=True,
    )
    if not sent:
        await msg.reply_text("Не удалось создать карточку. Попробуйте ещё раз через /start.")
        return

    key = f"{sent.chat_id}:{sent.message_id}"
    ctx.bot_data.setdefault("offer_cards", {})[key] = card

    flow_chat = ctx.user_data.pop("flow_chat_id", None)
    flow_msg  = ctx.user_data.pop("flow_msg_id", None)
    if flow_chat and flow_msg:
        try:
            await ctx.bot.edit_message_text(
                chat_id=flow_chat,
                message_id=flow_msg,
                text="Карточка создана и отправлена выше." + _warn(),
                parse_mode=ParseMode.HTML,
                reply_markup=kb_main(),
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            log.info("Could not update flow message: %s", e)


# ─── Business message handler ────────────────────────────────────────────────

async def on_biz_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.business_message
    if not msg or not msg.business_connection_id or not msg.text:
        return

    conn_id = msg.business_connection_id
    text    = msg.text.strip()

    conn = await get_conn_info(conn_id, ctx)
    if conn and not conn.get("is_enabled", True):
        log.info("Business connection disabled, skipping: %s", conn_id)
        return

    # Ignore messages sent by the bot itself
    if getattr(msg, "sender_business_bot", False):
        return

    sender   = msg.from_user
    # The Business account owner is the person who connected the bot.
    # conn["user_id"] is that owner's Telegram ID.
    owner_id = int(conn["user_id"]) if conn and conn.get("user_id") else None
    is_owner = bool(sender and owner_id and sender.id == owner_id)

    parsed = parse_nft(text)

    # ── OWNER writes .nft <amount> <link> in a chat with their buyer ─────────
    # Any user who connected the bot can do this — no hardcoded seller ID.
    if is_owner and parsed:
        seller_id  = owner_id or 0
        buyer_id   = int(msg.chat_id)
        chat       = msg.chat
        buyer_name = (
            f"@{chat.username}" if getattr(chat, "username", None)
            else (getattr(chat, "first_name", None) or f"Покупатель {buyer_id}")
        )
        sent_ok = await send_nft_card(
            ctx, msg.chat_id, conn_id,
            parsed[0], parsed[1],
            seller_id, buyer_id, buyer_name,
        )
        if sent_ok:
            # Delete the owner's .nft command so the chat stays clean
            try:
                await ctx.bot.delete_business_messages(
                    business_connection_id=conn_id,
                    message_ids=[msg.message_id],
                )
                log.info("Deleted .nft command: chat_id=%s msg_id=%s", msg.chat_id, msg.message_id)
            except (AttributeError, TelegramError) as e:
                log.warning("Could not delete .nft command: %s", e)
        return

    # ── BUYER accidentally types .nft → ignore silently ──────────────────────
    if parsed or re.search(r"(?i)(?:^|\s)\.nft(?:\s|$)", text):
        return

    # ── OWNER is mid-flow creating a regular offer card ──────────────────────
    if is_owner and ctx.user_data.get("awaiting_biz_offer"):
        await _finish_biz_offer(msg, ctx, conn_id)
        return

    # ── OWNER sends any other message → don't auto-reply ─────────────────────
    if is_owner:
        return

    # ── Incoming message from BUYER → away reply ─────────────────────────────
    sticker_id = os.getenv("PREMIUM_STICKER_FILE_ID", "").strip()
    if sticker_id:
        try:
            await ctx.bot.send_sticker(
                chat_id=msg.chat_id,
                sticker=sticker_id,
                business_connection_id=conn_id,
            )
        except TelegramError as e:
            log.warning("Could not send sticker: %s", e)

    await safe_send(
        ctx,
        chat_id=msg.chat_id,
        connection_id=conn_id,
        text="Привет... Незкс сейчас вернётся, либо он спит, а может быть и занят... Просто жди.",
        disable_web_page_preview=True,
    )


async def _finish_biz_offer(msg: Any, ctx: ContextTypes.DEFAULT_TYPE, conn_id: str) -> None:
    """Create an offer card from a Business chat text."""
    text = msg.text.strip() if msg.text else ""
    if text.lower() in {"/cancel", "отмена"}:
        ctx.user_data.clear()
        await safe_send(ctx, chat_id=msg.chat_id, connection_id=conn_id,
                        text="Создание предложения отменено.", reply_markup=kb_main())
        return

    if len(text) > MAX_OFFER_LEN:
        await safe_send(ctx, chat_id=msg.chat_id, connection_id=conn_id,
                        text=f"Слишком длинный текст — максимум {MAX_OFFER_LEN} символов.")
        return

    ctx.user_data.pop("awaiting_biz_offer", None)

    owner_id: int = ctx.user_data.get("biz_owner_id", 0)
    card = (
        f"<b>{em(E_OFFER,'📨')} Предложение</b>\n\n"
        f"<b>Описание:</b>\n{html.escape(text)}"
        + _warn()
    )

    sent = await safe_send(
        ctx,
        chat_id=msg.chat_id,
        connection_id=conn_id,
        text=card,
        parse_mode=ParseMode.HTML,
        reply_markup=kb_offer(owner_id),
        disable_web_page_preview=True,
    )
    if not sent:
        await safe_send(ctx, chat_id=msg.chat_id, connection_id=conn_id,
                        text="Не удалось отправить карточку. Попробуйте ещё раз.")
        return

    key = f"{sent.chat_id}:{sent.message_id}"
    ctx.bot_data.setdefault("offer_cards", {})[key] = card

    flow_chat = ctx.user_data.pop("flow_chat_id", None)
    flow_msg  = ctx.user_data.pop("flow_msg_id", None)
    if flow_chat and flow_msg:
        try:
            await ctx.bot.edit_message_text(
                chat_id=flow_chat,
                message_id=flow_msg,
                business_connection_id=conn_id,
                text="Карточка создана и отправлена." + _warn(),
                parse_mode=ParseMode.HTML,
                reply_markup=kb_main(),
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            log.info("Could not update Business flow message: %s", e)


# ─── NFT card sender ─────────────────────────────────────────────────────────

async def send_nft_card(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    conn_id: str | None,
    amount: str,
    url: str,
    seller_id: int,
    buyer_id: int,
    buyer_name: str,
) -> bool:
    keyboard = kb_nft(seller_id, buyer_id)

    # Try with custom emoji first, fall back to plain stars
    for with_emoji in (True, False):
        card = nft_card(buyer_name, amount, url, with_emoji=with_emoji)
        sent = await safe_send(
            ctx,
            chat_id=chat_id,
            connection_id=conn_id,
            text=card,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        if sent:
            key = f"{sent.chat_id}:{sent.message_id}"
            ctx.bot_data.setdefault("offer_cards", {})[key] = card
            ctx.bot_data.setdefault("nft_offers", {})[key] = {
                "seller_id": seller_id,
                "buyer_id":  buyer_id,
                "amount":    amount,
                "url":       url,
                "conn_id":   conn_id,
            }
            log.info("NFT card sent: chat_id=%s msg_id=%s", sent.chat_id, sent.message_id)
            return True
        log.warning("NFT card send failed (with_emoji=%s), %s",
                    with_emoji, "retrying plain" if with_emoji else "giving up")

    # Both attempts failed — notify owner
    await safe_send(
        ctx,
        chat_id=chat_id,
        connection_id=conn_id,
        text="Не удалось отправить карточку NFT. Проверьте права бота и попробуйте снова.",
    )
    return False


# ─── Direct .nft in bot DM ───────────────────────────────────────────────────

async def on_direct_nft(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    .nft typed directly in the bot's own DM chat.
    The person typing IS the seller; buyer_id = 0 (preview mode, no counter-party).
    """
    msg  = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text:
        return

    parsed = parse_nft(msg.text.strip())
    if not parsed:
        await msg.reply_text(
            "Формат команды:\n<code>.nft 1000 t.me/nft/LibertyFigure-14469</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # In a direct bot chat there is no buyer — seller_id = user, buyer_id = 0
    seller_id  = user.id
    buyer_id   = 0
    buyer_name = "Покупатель"   # placeholder; no real buyer in DM preview

    await send_nft_card(ctx, msg.chat_id, None, parsed[0], parsed[1],
                        seller_id, buyer_id, buyer_name)


# ─── Business connection update ──────────────────────────────────────────────

async def on_biz_connection(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = update.business_connection
    if not conn:
        return
    rights = getattr(conn, "rights", None)
    can_reply = getattr(rights, "can_reply", None) if rights else None
    info = {
        "user_id":    conn.user.id,
        "user_name":  conn.user.full_name,
        "is_enabled": conn.is_enabled,
        "can_reply":  can_reply,
    }
    ctx.bot_data.setdefault("biz_connections", {})[conn.id] = info
    log.info(
        "Business connection: id=%s enabled=%s owner_id=%s can_reply=%s",
        conn.id, conn.is_enabled, conn.user.id, can_reply,
    )
    if can_reply is False:
        log.warning(
            "can_reply is False for connection %s — бот не сможет отвечать в Business чатах. "
            "Переподключите бота и включите разрешение «Управление сообщениями».",
            conn.id,
        )


# ─── Sticker file_id helper ──────────────────────────────────────────────────

async def on_sticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg and msg.sticker:
        await msg.reply_text("PREMIUM_STICKER_FILE_ID для Render:\n" + msg.sticker.file_id)


# ─── App lifecycle ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    token    = os.getenv("BOT_TOKEN", "").strip()
    secret   = os.getenv("WEBHOOK_SECRET", "").strip()
    base_url = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")

    if not token:
        raise RuntimeError("BOT_TOKEN is missing.")
    if not secret or len(secret) < 8:
        raise RuntimeError("WEBHOOK_SECRET must be at least 8 characters.")
    if not base_url.startswith("https://"):
        raise RuntimeError("WEBHOOK_URL / RENDER_EXTERNAL_URL must be an https:// URL.")

    tg = Application.builder().token(token).build()

    tg.add_handler(BusinessConnectionHandler(on_biz_connection))
    tg.add_handler(MessageHandler(
        filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT, on_biz_message
    ))
    tg.add_handler(CommandHandler("start",  cmd_start))
    tg.add_handler(CommandHandler("help",   cmd_help))
    tg.add_handler(CommandHandler("cancel", cmd_cancel))
    tg.add_handler(MessageHandler(filters.Sticker.ALL, on_sticker))
    tg.add_handler(CallbackQueryHandler(on_button))
    tg.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, _route_text
    ))

    await tg.initialize()
    await tg.start()

    webhook_url = f"{base_url}{WEBHOOK_PATH}"
    await tg.bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    log.info("Webhook set: %s", webhook_url)
    fastapi_app.state.tg      = tg
    fastapi_app.state.secret  = secret

    try:
        yield
    finally:
        await tg.stop()
        await tg.shutdown()


async def _route_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route plain text in bot DM: .nft command or regular offer creation."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if re.search(r"(?i)(?:^|\s)\.nft(?:\s|$)", text):
        await on_direct_nft(update, ctx)
    else:
        await receive_offer(update, ctx)


# ─── FastAPI ─────────────────────────────────────────────────────────────────

app = FastAPI(title="NEZZX Bot", lifespan=lifespan)


@app.get("/")
async def root() -> dict:
    return {"service": "NEZZX Telegram Bot", "status": "running"}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    expected = getattr(request.app.state, "secret", "")
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected or supplied != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    tg: Application = request.app.state.tg
    try:
        payload = await request.json()
        log.info(
            "Update received: update_id=%s keys=%s",
            payload.get("update_id"),
            [k for k, v in payload.items() if v is not None],
        )
        update = Update.de_json(payload, tg.bot)
        await tg.process_update(update)
    except Exception as e:
        log.exception("Failed to process update: %s", e)
        return JSONResponse(status_code=500, content={"ok": False})
    return {"ok": True}
