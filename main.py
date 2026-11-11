
import asyncio
import html
import logging
import os
import secrets
import sqlite3
import time

from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramRetryAfter,
    TelegramBadRequest
)
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LinkPreviewOptions
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)

log = logging.getLogger("PortalMarket")


BOT_TOKEN = "9270560858:AAGIkDFXbTnfeHHHPVgt_cku1UAhhVAHGaE"
OWNER_ID = 534069614

CHANNEL_ID = "@tetofferbot"
CHANNEL_URL = "https://t.me/tetofferbot"

PEER_FLOOD_DELAY = 600
RETRY_SCAN_INTERVAL = 5

OFFER_LIFETIME = 6 * 60 * 60

DB_PATH = "offers.db"


if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения.")


bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
)

db.row_factory = sqlite3.Row

lock = asyncio.Lock()


def execute(sql, params=()):
    cur = db.execute(sql, params)
    db.commit()
    return cur


def fetchone(sql, params=()):
    return db.execute(sql, params).fetchone()


def fetchall(sql, params=()):
    return db.execute(sql, params).fetchall()


def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        seller_name TEXT,
        created_at INTEGER,
        banned INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS connections (
        connection_id TEXT PRIMARY KEY,
        owner_id INTEGER,
        enabled INTEGER DEFAULT 0,
        can_reply INTEGER DEFAULT 0,
        updated_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS offers (
        code TEXT PRIMARY KEY,
        owner_id INTEGER,
        language TEXT,
        gift_url TEXT,
        amount TEXT,
        currency TEXT,
        seller_name TEXT,
        status TEXT DEFAULT 'created',
        target_user_id INTEGER,
        connection_id TEXT,
        retry_at INTEGER,
        retry_count INTEGER DEFAULT 0,
        created_at INTEGER,
        sent_at INTEGER,
        chat_id INTEGER,
        message_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS business_users (
        owner_id INTEGER,
        connection_id TEXT,
        user_id INTEGER,
        chat_id INTEGER,
        username TEXT,
        first_name TEXT,
        last_seen INTEGER,
        PRIMARY KEY(connection_id, user_id)
    );
    """)

    columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(offers)"
        ).fetchall()
    }

    migrations = {
        "chat_id": "INTEGER",
        "message_id": "INTEGER",
        "retry_at": "INTEGER",
        "retry_count": "INTEGER DEFAULT 0",
        "sent_at": "INTEGER",
        "target_user_id": "INTEGER",
        "connection_id": "TEXT"
    }

    for name, definition in migrations.items():
        if name not in columns:
            db.execute(
                f"ALTER TABLE offers ADD COLUMN {name} {definition}"
            )

    db.commit()


def register_user(user):
    if not user:
        return

    old = fetchone(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,)
    )

    username = user.username or ""
    first_name = user.first_name or ""

    seller = f"@{username}" if username else first_name

    if old:
        execute(
            """
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
            """,
            (username, first_name, user.id)
        )
    else:
        execute(
            """
            INSERT INTO users (
                user_id, username, first_name,
                seller_name, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user.id,
                username,
                first_name,
                seller,
                int(time.time())
            )
        )


def seller_name(user_id):
    item = fetchone(
        "SELECT seller_name FROM users WHERE user_id=?",
        (user_id,)
    )

    return item["seller_name"] if item else "Seller"


async def is_subscribed(user_id):
    try:
        member = await bot.get_chat_member(
            CHANNEL_ID,
            user_id
        )

        return (
            member.status in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR
            )
            or (
                member.status == ChatMemberStatus.RESTRICTED
                and getattr(member, "is_member", False)
            )
        )

    except TelegramAPIError as exc:
        log.warning(
            "Subscription check failed for %s: %s",
            user_id,
            exc
        )
        return False


def subscribe_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подписаться",
                    url=CHANNEL_URL
                )
            ],
            [
                InlineKeyboardButton(
                    text="Проверить подписку",
                    callback_data="sub:check"
                )
            ]
        ]
    )


def menu_keyboard(user_id):
    buttons = [
        [
            InlineKeyboardButton(
                text="📦 Офферы",
                callback_data="panel:offers"
            ),
            InlineKeyboardButton(
                text="👥 Клиенты",
                callback_data="panel:clients"
            )
        ],
        [
            InlineKeyboardButton(
                text="⏱ Таймеры",
                callback_data="panel:timers"
            ),
            InlineKeyboardButton(
                text="📊 Статистика",
                callback_data="panel:stats"
            )
        ],
        [
            InlineKeyboardButton(
                text="❔ Помощь",
                callback_data="panel:help"
            )
        ]
    ]

    if user_id == OWNER_ID:
        buttons.append([
            InlineKeyboardButton(
                text="🛠 Админ-панель",
                callback_data="admin:home"
            )
        ])

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


def back_to_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ В меню",
                    callback_data="panel:menu"
                )
            ]
        ]
    )


async def check_access(message):
    user = message.from_user

    if not user:
        return False

    register_user(user)

    account = fetchone(
        "SELECT banned FROM users WHERE user_id=?",
        (user.id,)
    )

    if account and account["banned"] and user.id != OWNER_ID:
        await message.answer("⛔ Доступ ограничен.")
        return False

    return True


async def check_callback(callback):
    register_user(callback.from_user)

    account = fetchone(
        "SELECT banned FROM users WHERE user_id=?",
        (callback.from_user.id,)
    )

    if account and account["banned"] and callback.from_user.id != OWNER_ID:
        await callback.answer(
            "Доступ ограничен.",
            show_alert=True
        )
        return False

    return True


def gift_valid(url):
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme == "https"
            and parsed.netloc.lower() in (
                "t.me",
                "telegram.me"
            )
            and parsed.path.startswith("/nft/")
            and len(parsed.path.split("/")) >= 3
        )

    except Exception:
        return False


def gift_name(url):
    return (
        url.rstrip("/")
        .split("/")[-1]
        .replace("-", " #")
    )


def valid_amount(value):
    try:
        amount = Decimal(
            value.replace(",", ".")
        )

        if not amount.is_finite() or amount <= 0:
            return None

        return format(amount.normalize(), "f")

    except (InvalidOperation, AttributeError):
        return None


def make_code():
    return secrets.token_hex(4).upper()


def get_connection(owner_id):
    return fetchone(
        """
        SELECT connection_id
        FROM connections
        WHERE owner_id=?
          AND enabled=1
          AND can_reply=1
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (owner_id,)
    )


def offer_keyboard(code):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"client:decline:{code}"
                ),
                InlineKeyboardButton(
                    text="Принять",
                    callback_data=f"client:accept:{code}"
                )
            ]
        ]
    )


def accepted_keyboard(code, gift_url):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Передать NFT",
                    url=gift_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="Как передать?",
                    callback_data=f"client:how:{code}"
                )
            ]
        ]
    )


def offer_text(offer):
    currency = (
        "⭐️"
        if offer["currency"] == "stars"
        else "💎"
    )

    gift_url = html.escape(
        offer["gift_url"],
        quote=True
    )

    nft_title = html.escape(
        gift_name(offer["gift_url"])
    )

    seller = html.escape(
        offer["seller_name"] or "Seller"
    )

    if offer["language"] == "ru":
        return (
            f"Пользователь <b>{seller}</b> "
            f"предлагает Вам "
            f"<b>{html.escape(offer['amount'])} {currency}</b> "
            f"за подарок "
            f'<a href="{gift_url}"><b>{nft_title}</b></a>.\n\n'
            f"<b>Действует еще 6 часов.</b>"
        )

    return (
        f"User <b>{seller}</b> "
        f"offers you "
        f"<b>{html.escape(offer['amount'])} {currency}</b> "
        f"for the gift "
        f'<a href="{gift_url}"><b>{nft_title}</b></a>.\n\n'
        f"<b>Valid for another 6 hours.</b>"
    )


def accepted_text(offer):
    gift_url = html.escape(
        offer["gift_url"],
        quote=True
    )

    nft_title = html.escape(
        gift_name(offer["gift_url"])
    )

    return (
        f'<a href="{gift_url}">&#8203;</a>'
        f"🎁 <b>Подарок:</b> "
        f'<a href="{gift_url}"><b>{nft_title}</b></a> '
        f"<b>успешно продан!</b>\n\n"
        f"<b>Передайте подарок покупателю для зачисления звёзд.</b>"
    )


async def notify_owner(owner_id, text):
    try:
        await bot.send_message(
            owner_id,
            text,
            link_preview_options=LinkPreviewOptions(
                is_disabled=True
            )
        )

    except TelegramAPIError as exc:
        log.error(
            "Owner notification error: %s",
            exc
        )


async def schedule_retry(offer):
    retry_at = int(time.time()) + PEER_FLOOD_DELAY

    execute(
        """
        UPDATE offers
        SET status='retry_wait',
            retry_at=?,
            retry_count=retry_count+1
        WHERE code=? AND owner_id=?
        """,
        (
            retry_at,
            offer["code"],
            offer["owner_id"]
        )
    )

    await notify_owner(
        offer["owner_id"],
        f"⚠️ PEER_FLOOD для оффера "
        f"<code>{offer['code']}</code>.\n"
        f"Повторная отправка через "
        f"<b>{PEER_FLOOD_DELAY} секунд.</b>"
    )


async def send_offer(owner_id, code, target_id):
    async with lock:
        offer = fetchone(
            """
            SELECT *
            FROM offers
            WHERE code=? AND owner_id=?
            """,
            (code, owner_id)
        )

        if not offer:
            return False, "Оффер не найден."

        if offer["status"] not in (
            "created",
            "retry_wait",
            "retry_failed"
        ):
            return False, "Оффер уже отправлен или обработан."

        connection = get_connection(owner_id)

        if not connection:
            return False, (
                "Нет активного Telegram Business подключения "
                "с правом отвечать."
            )

        connection_id = connection["connection_id"]

        execute(
            """
            UPDATE offers
            SET status='sending',
                target_user_id=?,
                connection_id=?,
                retry_at=NULL
            WHERE code=? AND owner_id=?
            """,
            (
                target_id,
                connection_id,
                code,
                owner_id
            )
        )

        try:
            sent = await bot.send_message(
                chat_id=target_id,
                business_connection_id=connection_id,
                text=offer_text(offer),
                reply_markup=offer_keyboard(code),
                link_preview_options=LinkPreviewOptions(
                    url=offer["gift_url"],
                    prefer_large_media=True,
                    show_above_text=True
                )
            )

            execute(
                """
                UPDATE offers
                SET status='sent',
                    sent_at=?,
                    chat_id=?,
                    message_id=?
                WHERE code=? AND owner_id=?
                """,
                (
                    int(time.time()),
                    sent.chat.id,
                    sent.message_id,
                    code,
                    owner_id
                )
            )

            await notify_owner(
                owner_id,
                "<b>✅ ПРЕДЛОЖЕНИЕ ОТПРАВЛЕНО</b>\n\n"
                f"🎁 NFT: <code>{html.escape(gift_name(offer['gift_url']))}</code>\n"
                f"💰 Сумма: <b>{html.escape(offer['amount'])} "
                f"{'⭐️' if offer['currency'] == 'stars' else '💎'}</b>\n"
                f"🔑 Код: <code>{code}</code>"
            )

            log.info(
                "Offer %s sent to %s",
                code,
                target_id
            )

            return True, "Оффер отправлен."

        except TelegramRetryAfter as exc:
            await schedule_retry(offer)

            log.warning(
                "Telegram flood limit: %s",
                exc
            )

            return False, (
                f"Ограничение Telegram. Повтор через "
                f"{PEER_FLOOD_DELAY} секунд."
            )

        except TelegramAPIError as exc:
            if "PEER_FLOOD" in str(exc).upper():
                await schedule_retry(offer)

                return False, (
                    f"PEER_FLOOD. Повтор через "
                    f"{PEER_FLOOD_DELAY} секунд."
                )

            execute(
                """
                UPDATE offers
                SET status='retry_failed'
                WHERE code=? AND owner_id=?
                """,
                (code, owner_id)
            )

            await notify_owner(
                owner_id,
                f"❌ Ошибка отправки {code}:\n"
                f"<code>{html.escape(str(exc)[:1000])}</code>"
            )

            log.exception(
                "Offer sending failed: %s",
                code
            )

            return False, str(exc)

        except Exception as exc:
            execute(
                """
                UPDATE offers
                SET status='retry_failed'
                WHERE code=? AND owner_id=?
                """,
                (code, owner_id)
            )

            log.exception(
                "Unexpected send error: %s",
                code
            )

            return False, str(exc)


@dp.message(Command("start"))
async def start(message: types.Message):
    if not await check_access(message):
        return

    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "Для использования бота подпишись на канал.",
            reply_markup=subscribe_keyboard()
        )
        return

    await message.answer(
        "👋 <b>PortalMarket</b>\n\n"
        "Панель управления.",
        reply_markup=menu_keyboard(
            message.from_user.id
        )
    )


@dp.message(Command("menu"))
async def menu(message: types.Message):
    if not await check_access(message):
        return

    await message.answer(
        "Панель управления:",
        reply_markup=menu_keyboard(
            message.from_user.id
        )
    )


@dp.message(Command("help"))
async def help_command(message: types.Message):
    if not await check_access(message):
        return

    await message.answer(
        "<b>Команды бота</b>\n\n"
        "<b>Создание предложения:</b>\n"
        "<code>.buy ru URL SUM stars</code>\n"
        "<code>.buy eng URL SUM Gram</code>\n\n"
        "<b>Отправка покупателю:</b>\n"
        "<code>/ref CODE USER_ID</code>\n\n"
        "<b>Имя продавца:</b>\n"
        "<code>/sellername @name</code>\n\n"
        "<code>/menu</code>"
    )


@dp.message(Command("sellername"))
async def sellername_command(message: types.Message):
    if not await check_access(message):
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "Формат: <code>/sellername @username</code>"
        )
        return

    name = parts[1].strip()[:64]

    if not name:
        await message.answer("❌ Имя не может быть пустым.")
        return

    execute(
        """
        UPDATE users
        SET seller_name=?
        WHERE user_id=?
        """,
        (name, message.from_user.id)
    )

    await message.answer(
        f"✅ Имя продавца изменено: "
        f"<b>{html.escape(name)}</b>"
    )


@dp.message(F.text.startswith(".buy"))
async def buy_command(message: types.Message):
    if not await check_access(message):
        return

    parts = (message.text or "").split()

    if len(parts) != 5:
        await message.answer(
            "❌ <b>Неверный формат.</b>\n\n"
            "<code>.buy ru URL SUM stars</code>\n"
            "<code>.buy eng URL SUM Gram</code>"
        )
        return

    _, language, gift_url, amount_raw, currency_raw = parts

    language = language.lower()
    currency = currency_raw.lower()

    amount = valid_amount(amount_raw)

    if language not in ("ru", "eng"):
        await message.answer(
            "❌ Язык должен быть ru или eng."
        )
        return

    if not gift_valid(gift_url):
        await message.answer(
            "❌ <b>Неверная NFT-ссылка.</b>\n"
            "Пример: <code>https://t.me/nft/...</code>"
        )
        return

    if amount is None:
        await message.answer("❌ Неверная сумма.")
        return

    if currency not in ("stars", "gram"):
        await message.answer(
            "❌ Валюта должна быть stars или Gram."
        )
        return

    code = make_code()

    while fetchone(
        "SELECT code FROM offers WHERE code=?",
        (code,)
    ):
        code = make_code()

    execute(
        """
        INSERT INTO offers (
            code, owner_id, language, gift_url,
            amount, currency, seller_name,
            status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'created', ?)
        """,
        (
            code,
            message.from_user.id,
            language,
            gift_url,
            amount,
            currency,
            seller_name(message.from_user.id),
            int(time.time())
        )
    )

    currency_symbol = (
        "⭐️" if currency == "stars" else "💎"
    )

    await message.answer(
        "<b>✅ ПРЕДЛОЖЕНИЕ СОЗДАНО</b>\n\n"
        f"🎁 NFT: <code>{html.escape(gift_name(gift_url))}</code>\n"
        f"💰 Сумма: <b>{html.escape(amount)} {currency_symbol}</b>\n"
        f"🌐 Язык: <b>{language.upper()}</b>\n\n"
        f"🔑 Код: <code>{code}</code>\n\n"
        "Отправить покупателю:\n"
        f"<code>/ref {code} USER_ID</code>"
    )


@dp.message(Command("ref"))
async def ref_command(message: types.Message):
    if not await check_access(message):
        return

    parts = (message.text or "").split()

    if len(parts) != 3:
        await message.answer(
            "❌ Формат:\n"
            "<code>/ref CODE USER_ID</code>"
        )
        return

    code = parts[1].upper()

    try:
        target_id = int(parts[2])

        if target_id <= 0:
            raise ValueError

    except ValueError:
        await message.answer("❌ Неверный USER_ID.")
        return

    offer = fetchone(
        """
        SELECT code
        FROM offers
        WHERE code=? AND owner_id=?
        """,
        (code, message.from_user.id)
    )

    if not offer:
        await message.answer(
            "❌ Оффер не найден в твоём аккаунте."
        )
        return

    ok, result = await send_offer(
        message.from_user.id,
        code,
        target_id
    )

    await message.answer(
        ("✅ " if ok else "⚠️ ")
        + html.escape(result)
    )


@dp.business_connection()
async def business_connection_handler(
    connection: types.BusinessConnection
):
    owner_id = connection.user.id

    register_user(connection.user)

    enabled = bool(connection.is_enabled)

    can_reply = bool(connection.can_reply)

    execute(
        """
        INSERT INTO connections (
            connection_id,
            owner_id,
            enabled,
            can_reply,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(connection_id) DO UPDATE SET
            owner_id=excluded.owner_id,
            enabled=excluded.enabled,
            can_reply=excluded.can_reply,
            updated_at=excluded.updated_at
        """,
        (
            connection.id,
            owner_id,
            int(enabled),
            int(can_reply),
            int(time.time())
        )
    )

    if enabled and can_reply:
        await notify_owner(
            owner_id,
            "✅ <b>Telegram Business подключён.</b>\n\n"
            "Право отвечать активно."
        )

        log.info(
            "Business connection enabled: %s",
            owner_id
        )

    elif not enabled:
        await notify_owner(
            owner_id,
            "⚠️ <b>Telegram Business отключён.</b>"
        )

        log.warning(
            "Business connection disabled: %s",
            owner_id
        )

    else:
        await notify_owner(
            owner_id,
            "⚠️ <b>Business подключён, но право отвечать недоступно.</b>\n\n"
            "Проверь разрешения подключения."
        )

        log.warning(
            "Business connection has no can_reply permission: %s",
            owner_id
        )


@dp.business_message()
async def business_message_handler(
    message: types.Message
):
    if (
        not message.business_connection_id
        or not message.from_user
    ):
        return

    connection = fetchone(
        """
        SELECT owner_id, enabled
        FROM connections
        WHERE connection_id=?
        """,
        (message.business_connection_id,)
    )

    if not connection or not connection["enabled"]:
        return

    owner_id = connection["owner_id"]

    if message.from_user.id == owner_id:
        return

    execute(
        """
        INSERT INTO business_users (
            owner_id, connection_id, user_id,
            chat_id, username, first_name, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(connection_id, user_id) DO UPDATE SET
            chat_id=excluded.chat_id,
            username=excluded.username,
            first_name=excluded.first_name,
            last_seen=excluded.last_seen
        """,
        (
            owner_id,
            message.business_connection_id,
            message.from_user.id,
            message.chat.id,
            message.from_user.username or "",
            message.from_user.first_name or "",
            int(time.time())
        )
    )


@dp.callback_query(F.data == "sub:check")
async def check_subscription_callback(
    callback: types.CallbackQuery
):
    register_user(callback.from_user)

    if await is_subscribed(callback.from_user.id):
        await callback.answer(
            "Подписка подтверждена!"
        )

        if callback.message:
            try:
                await callback.message.edit_text(
                    "✅ Доступ открыт. Панель управления:",
                    reply_markup=menu_keyboard(
                        callback.from_user.id
                    )
                )
            except TelegramAPIError:
                pass

    else:
        await callback.answer(
            "Подписка не найдена.",
            show_alert=True
        )


@dp.callback_query(F.data.startswith("panel:"))
async def panel_callback(
    callback: types.CallbackQuery
):
    if not await check_callback(callback):
        return

    owner_id = callback.from_user.id

    action = callback.data.split(":", 1)[1]

    if action == "menu":
        text = "Панель управления:"
        markup = menu_keyboard(owner_id)

    elif action == "offers":
        items = fetchall(
            """
            SELECT code, amount, currency, status
            FROM offers
            WHERE owner_id=?
            ORDER BY created_at DESC
            LIMIT 15
            """,
            (owner_id,)
        )

        if not items:
            text = "📦 Офферов пока нет."

        else:
            lines = ["<b>📦 Твои офферы</b>\n"]

            for item in items:
                symbol = (
                    "⭐️"
                    if item["currency"] == "stars"
                    else "💎"
                )

                lines.append(
                    f"• <code>{item['code']}</code> | "
                    f"{html.escape(item['amount'])} {symbol} | "
                    f"<b>{html.escape(item['status'])}</b>"
                )

            text = "\n".join(lines)

        markup = back_to_menu_keyboard()

    elif action == "clients":
        items = fetchall(
            """
            SELECT user_id, username, first_name
            FROM business_users
            WHERE owner_id=?
            ORDER BY last_seen DESC
            LIMIT 15
            """,
            (owner_id,)
        )

        if not items:
            text = "👥 Клиентов пока нет."

        else:
            lines = ["<b>👥 Твои клиенты</b>\n"]

            for item in items:
                name = (
                    "@" + item["username"]
                    if item["username"]
                    else item["first_name"]
                )

                lines.append(
                    f"• {html.escape(name or 'Без имени')} "
                    f"(<code>{item['user_id']}</code>)"
                )

            text = "\n".join(lines)

        markup = back_to_menu_keyboard()

    elif action == "timers":
        items = fetchall(
            """
            SELECT code, target_user_id, retry_at
            FROM offers
            WHERE owner_id=? AND status='retry_wait'
            ORDER BY retry_at
            """,
            (owner_id,)
        )

        if not items:
            text = "⏱ Активных таймеров (PEER_FLOOD) нет."

        else:
            lines = ["<b>⏱ Активные таймеры</b>\n"]

            now = int(time.time())

            for item in items:
                left = max(
                    0,
                    item["retry_at"] - now
                )

                m, s = divmod(left, 60)

                time_str = (
                    f"{m} мин {s} сек"
                    if m > 0
                    else f"{s} сек"
                )

                lines.append(
                    f"• <code>{item['code']}</code> "
                    f"— ⏳ <b>{time_str}</b>"
                )

            text = "\n".join(lines)

        markup = back_to_menu_keyboard()

    elif action == "stats":
        total = fetchone(
            "SELECT COUNT(*) n FROM offers WHERE owner_id=?",
            (owner_id,)
        )["n"]

        sent = fetchone(
            """
            SELECT COUNT(*) n
            FROM offers
            WHERE owner_id=? AND status='sent'
            """,
            (owner_id,)
        )["n"]

        clients = fetchone(
            """
            SELECT COUNT(*) n
            FROM business_users
            WHERE owner_id=?
            """,
            (owner_id,)
        )["n"]

        waiting = fetchone(
            """
            SELECT COUNT(*) n
            FROM offers
            WHERE owner_id=? AND status='retry_wait'
            """,
            (owner_id,)
        )["n"]

        text = (
            "<b>📊 Твоя статистика</b>\n\n"
            f"• Создано офферов: <b>{total}</b>\n"
            f"• Отправлено: <b>{sent}</b>\n"
            f"• Клиентов: <b>{clients}</b>\n"
            f"• Ожидают повтора: <b>{waiting}</b>"
        )

        markup = back_to_menu_keyboard()

    else:
        text = (
            "<b>Помощь</b>\n\n"
            "<code>.buy ru URL SUM stars</code>\n"
            "<code>.buy eng URL SUM Gram</code>\n"
            "<code>/ref CODE USER_ID</code>\n"
            "<code>/sellername @name</code>"
        )

        markup = back_to_menu_keyboard()

    await callback.answer()

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=markup
            )
        except TelegramBadRequest:
            pass


@dp.callback_query(F.data.startswith("client:"))
async def client_action_callback(
    callback: types.CallbackQuery
):
    if not await check_callback(callback):
        return

    if not callback.message:
        await callback.answer()
        return

    try:
        _, action, code = callback.data.split(":", 2)

    except ValueError:
        await callback.answer(
            "Некорректная кнопка.",
            show_alert=True
        )
        return

    async with lock:
        offer = fetchone(
            "SELECT * FROM offers WHERE code=?",
            (code,)
        )

        if not offer:
            await callback.answer(
                "Предложение не найдено.",
                show_alert=True
            )
            return

        if (
            offer["message_id"] != callback.message.message_id
            or offer["chat_id"] != callback.message.chat.id
        ):
            await callback.answer(
                "Недействительное предложение.",
                show_alert=True
            )
            return

        if offer["target_user_id"] != callback.from_user.id:
            await callback.answer(
                "Это предложение отправлено не тебе.",
                show_alert=True
            )
            return

        if action == "how":
            if offer["status"] != "accepted":
                await callback.answer(
                    "Предложение ещё не принято.",
                    show_alert=True
                )
                return

            await callback.answer(
                "Нажмите «Передать NFT», проверьте получателя "
                "и подтвердите передачу в Telegram.",
                show_alert=True
            )
            return

        if offer["status"] != "sent":
            await callback.answer(
                "Предложение уже обработано.",
                show_alert=True
            )
            return

        sent_at = offer["sent_at"] or offer["created_at"] or 0

        if int(time.time()) - sent_at >= OFFER_LIFETIME:
            execute(
                """
                UPDATE offers
                SET status='expired'
                WHERE code=? AND status='sent'
                """,
                (code,)
            )

            try:
                await bot.edit_message_text(
                    chat_id=offer["chat_id"],
                    message_id=offer["message_id"],
                    business_connection_id=offer["connection_id"],
                    text="<b>⏱ Срок действия предложения истёк.</b>",
                    reply_markup=None,
                    link_preview_options=LinkPreviewOptions(
                        is_disabled=True
                    )
                )
            except TelegramAPIError as exc:
                log.warning(
                    "Expired offer edit failed: %s",
                    exc
                )

            await callback.answer(
                "Срок действия предложения истёк.",
                show_alert=True
            )

            await notify_owner(
                offer["owner_id"],
                f"⏱ Срок действия оффера "
                f"<code>{code}</code> истёк."
            )

            return

        if action == "accept":
            try:
                await bot.edit_message_text(
                    chat_id=offer["chat_id"],
                    message_id=offer["message_id"],
                    business_connection_id=offer["connection_id"],
                    text=accepted_text(offer),
                    reply_markup=accepted_keyboard(
                        code,
                        offer["gift_url"]
                    ),
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(
                        url=offer["gift_url"],
                        prefer_large_media=True,
                        show_above_text=True
                    )
                )

                execute(
                    """
                    UPDATE offers
                    SET status='accepted'
                    WHERE code=? AND status='sent'
                    """,
                    (code,)
                )

                await callback.answer(
                    "Предложение принято!"
                )

                log.info(
                    "Offer %s accepted",
                    code
                )

                await notify_owner(
                    offer["owner_id"],
                    "<b>✅ ПРЕДЛОЖЕНИЕ ПРИНЯТО</b>\n\n"
                    f"🎁 NFT: <code>{html.escape(gift_name(offer['gift_url']))}</code>\n"
                    f"🔑 Код: <code>{code}</code>\n"
                    f"👤 Покупатель: <code>{callback.from_user.id}</code>"
                )

            except TelegramBadRequest as exc:
                await callback.answer(
                    "Не удалось изменить сообщение.",
                    show_alert=True
                )

                log.error(
                    "Accept edit error: %s",
                    exc
                )

            except TelegramAPIError as exc:
                await callback.answer(
                    "Ошибка Telegram API.",
                    show_alert=True
                )

                log.error(
                    "Accept Telegram error: %s",
                    exc
                )

        elif action == "decline":
            try:
                await bot.edit_message_text(
                    chat_id=offer["chat_id"],
                    message_id=offer["message_id"],
                    business_connection_id=offer["connection_id"],
                    text="<b>Предложение отклонено.</b>",
                    parse_mode="HTML",
                    reply_markup=None,
                    link_preview_options=LinkPreviewOptions(
                        is_disabled=True
                    )
                )

                execute(
                    """
                    UPDATE offers
                    SET status='declined'
                    WHERE code=? AND status='sent'
                    """,
                    (code,)
                )

                await callback.answer(
                    "Предложение отклонено!"
                )

                log.info(
                    "Offer %s declined",
                    code
                )

                await notify_owner(
                    offer["owner_id"],
                    "<b>❌ ПРЕДЛОЖЕНИЕ ОТКЛОНЕНО</b>\n\n"
                    f"🎁 NFT: <code>{html.escape(gift_name(offer['gift_url']))}</code>\n"
                    f"🔑 Код: <code>{code}</code>\n"
                    f"👤 Покупатель: <code>{callback.from_user.id}</code>"
                )

            except TelegramBadRequest as exc:
                await callback.answer(
                    "Ошибка изменения сообщения.",
                    show_alert=True
                )

                log.error(
                    "Decline edit error: %s",
                    exc
                )

            except TelegramAPIError as exc:
                await callback.answer(
                    "Ошибка Telegram API.",
                    show_alert=True
                )

                log.error(
                    "Decline Telegram error: %s",
                    exc
                )

        else:
            await callback.answer(
                "Неизвестное действие.",
                show_alert=True
            )


@dp.callback_query(F.data.startswith("admin:"))
async def admin_callback(
    callback: types.CallbackQuery
):
    if not await check_callback(callback):
        return

    if callback.from_user.id != OWNER_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True
        )
        return

    action = callback.data.split(":", 1)[1]

    if action == "home":
        text = "🛠 <b>Админ-панель</b>"

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📊 Статистика",
                        callback_data="admin:stats"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="👥 Пользователи",
                        callback_data="admin:users"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="◀️ В меню",
                        callback_data="panel:menu"
                    )
                ]
            ]
        )

    elif action == "stats":
        users = fetchone(
            "SELECT COUNT(*) n FROM users"
        )["n"]

        offers = fetchone(
            "SELECT COUNT(*) n FROM offers"
        )["n"]

        connections = fetchone(
            "SELECT COUNT(*) n FROM connections WHERE enabled=1"
        )["n"]

        text = (
            "<b>📊 Общая статистика</b>\n\n"
            f"Пользователей: <b>{users}</b>\n"
            f"Офферов: <b>{offers}</b>\n"
            f"Активных подключений: <b>{connections}</b>"
        )

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="◀️ Назад",
                        callback_data="admin:home"
                    )
                ]
            ]
        )

    elif action == "users":
        items = fetchall(
            """
            SELECT user_id, username, first_name, banned
            FROM users
            ORDER BY created_at DESC
            LIMIT 20
            """
        )

        lines = ["<b>👥 Пользователи</b>\n"]

        for item in items:
            name = (
                "@" + item["username"]
                if item["username"]
                else item["first_name"]
            )

            status = "⛔" if item["banned"] else "✅"

            lines.append(
                f"{status} {html.escape(name or 'Без имени')} "
                f"— <code>{item['user_id']}</code>"
            )

        text = "\n".join(lines)

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="◀️ Назад",
                        callback_data="admin:home"
                    )
                ]
            ]
        )

    else:
        await callback.answer()
        return

    await callback.answer()

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=markup
            )
        except TelegramBadRequest:
            pass


async def retry_worker():
    while True:
        try:
            due = fetchall(
                """
                SELECT owner_id, code, target_user_id
                FROM offers
                WHERE status='retry_wait'
                  AND retry_at<=?
                """,
                (int(time.time()),)
            )

            for offer in due:
                if offer["target_user_id"]:
                    await send_offer(
                        offer["owner_id"],
                        offer["code"],
                        offer["target_user_id"]
                    )

        except Exception:
            log.exception(
                "Retry worker failed"
            )

        await asyncio.sleep(
            RETRY_SCAN_INTERVAL
        )


async def main():
    init_db()

    log.info("PortalMarket started")
    log.info("Owner: %s", OWNER_ID)
    log.info(
        "PEER_FLOOD delay: %s seconds",
        PEER_FLOOD_DELAY
    )

    retry_task = asyncio.create_task(
        retry_worker()
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )
    finally:
        retry_task.cancel()

        try:
            await retry_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        log.info("Bot stopped")

    except Exception:
        log.exception("Fatal error")

    finally:
        db.close()
