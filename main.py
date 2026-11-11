import asyncio
import html
import os
import secrets
import sqlite3
import time

from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LinkPreviewOptions
)
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError, TelegramBadRequest
from telethon import TelegramClient, events

# CONFIG

API_ID = 39869955
API_HASH = "00fe8f6a22b5b3ee33b8c97c353ee955"
PHONE = "+77716829130"
BOT_TOKEN = "9270560858:AAGIkDFXbTnfeHHHPVgt_cku1UAhhVAHGaE"

SELLER_NAME = "@crazyfuckin"

GLOBAL_INTERVAL = 60
CHAT_INTERVAL = 600


# CLIENTS

client = TelegramClient("userbot_session", API_ID, API_HASH)

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

OWNER_ID = None


# DATABASE

db = sqlite3.connect("offers.db")
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS offers (
    code TEXT PRIMARY KEY,
    creator_id INTEGER NOT NULL,
    creator_chat_id INTEGER,
    gift_url TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    language TEXT NOT NULL,
    used INTEGER DEFAULT 0,
    chat_id INTEGER,
    status TEXT DEFAULT 'created',
    connection_id TEXT,
    message_id INTEGER,
    created_at INTEGER DEFAULT 0
)
""")

columns = {
    row["name"]
    for row in db.execute("PRAGMA table_info(offers)").fetchall()
}

migrations = {
    "creator_chat_id": "INTEGER",
    "used": "INTEGER DEFAULT 0",
    "chat_id": "INTEGER",
    "status": "TEXT DEFAULT 'created'",
    "connection_id": "TEXT",
    "message_id": "INTEGER",
    "created_at": "INTEGER DEFAULT 0"
}

for name, definition in migrations.items():
    if name not in columns:
        db.execute(
            f"ALTER TABLE offers ADD COLUMN {name} {definition}"
        )

db.execute("""
UPDATE offers
SET status = CASE
    WHEN COALESCE(used, 0) = 1 THEN 'sent'
    ELSE 'created'
END
WHERE status IS NULL
""")

db.execute("""
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    can_reply INTEGER NOT NULL
)
""")

db.commit()


# STATE

lock = asyncio.Lock()

last_sent = {}
global_last_sent = 0
blocked_until = 0


# HELPERS

def gift_valid(url):
    try:
        p = urlparse(url)

        return (
            p.scheme == "https"
            and p.netloc.lower() in ("t.me", "telegram.me")
            and p.path.startswith("/nft/")
            and len(p.path.split("/")) >= 3
        )
    except Exception:
        return False


def gift_name(url):
    return url.rstrip("/").split("/")[-1].replace("-", " #")


def new_code():
    while True:
        code = secrets.token_hex(4).upper()

        if not db.execute(
            "SELECT 1 FROM offers WHERE code=?",
            (code,)
        ).fetchone():
            return code


def keyboard(code, url):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Принять",
                    callback_data=f"accept:{code}"
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"decline:{code}"
                )
            ]
        ]
    )


def get_connection(owner_id):
    if owner_id is None:
        return None

    return db.execute("""
        SELECT *
        FROM connections
        WHERE owner_id=?
          AND enabled=1
          AND can_reply=1
        ORDER BY rowid DESC
        LIMIT 1
    """, (owner_id,)).fetchone()


async def panel(text, chat_id=None):
    target = chat_id if chat_id is not None else OWNER_ID

    if target is None:
        return

    try:
        await bot.send_message(
            chat_id=target,
            text=text,
            link_preview_options=LinkPreviewOptions(
                is_disabled=True
            )
        )
    except TelegramAPIError as e:
        print(f"[PANEL ERROR] {e}")


def wait_time(chat_id):
    now = time.monotonic()

    return max(
        0,
        int(blocked_until - now),
        int(GLOBAL_INTERVAL - (now - global_last_sent)),
        int(CHAT_INTERVAL - (now - last_sent.get(chat_id, 0)))
    )


# BOT PANEL COMMANDS

@dp.message(F.text)
async def panel_commands(message: types.Message):

    if message.chat.type != "private":
        return

    if OWNER_ID is None or message.from_user.id != OWNER_ID:
        return

    raw = (message.text or "").strip()
    parts = raw.split()

    if not parts:
        return

    command = parts[0].lower()

    # HELP

    if command == "/help":

        await message.answer(
            "<b>ПАНЕЛЬ УПРАВЛЕНИЯ</b>\n\n"
            "<b>Создание предложения:</b>\n"
            "<code>.buy ru URL SUM stars</code>\n"
            "<code>.buy eng URL SUM Gram</code>\n\n"
            "<b>Отправка мамонту:</b>\n"
            "<code>/ref CODE</code>\n\n"
            "<b>Помощь:</b>\n"
            "<code>/help</code>"
        )
        return

    # BUY

    if command != ".buy":
        return

    if len(parts) != 5:
        await message.answer(
            "❌ <b>Неверный формат.</b>\n\n"
            "<code>.buy ru URL SUM stars</code>\n"
            "<code>.buy eng URL SUM Gram</code>"
        )
        return

    _, lang, url, amount, currency = parts

    lang = lang.lower()
    currency = currency.lower()

    if lang not in ("ru", "eng"):
        await message.answer("❌ Язык должен быть ru или eng.")
        return

    if currency not in ("stars", "gram"):
        await message.answer("❌ Валюта должна быть stars или Gram.")
        return

    if not gift_valid(url):
        await message.answer(
            "❌ <b>Неверная NFT-ссылка.</b>\n"
            "Пример: <code>https://t.me/nft/...</code>"
        )
        return

    try:
        value = Decimal(amount)

        if not value.is_finite() or value <= 0:
            raise InvalidOperation

    except InvalidOperation:
        await message.answer("❌ Неверная сумма.")
        return

    code = new_code()

    currency_name = (
        "stars"
        if currency == "stars"
        else "💎 Gram"
    )

    try:
        db.execute("""
            INSERT INTO offers (
                code,
                creator_id,
                creator_chat_id,
                chat_id,
                gift_url,
                amount,
                currency,
                language,
                created_at,
                status,
                used
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'created', 0)
        """, (
            code,
            message.from_user.id,
            message.chat.id,
            url,
            amount,
            currency_name,
            lang,
            int(time.time())
        ))

        db.commit()

        await message.answer(
            "<b>✅ ПРЕДЛОЖЕНИЕ СОЗДАНО</b>\n\n"
            f"🎁 NFT: <code>{html.escape(gift_name(url))}</code>\n"
            f"💰 Сумма: <b>{html.escape(amount)} {currency_name}</b>\n"
            f"🌐 Язык: <b>{lang.upper()}</b>\n\n"
            f"🔑 Код: <code>{code}</code>\n\n"
            "Отправить мамонту:\n"
            f"<code>/ref {code}</code>"
        )

    except sqlite3.Error as e:
        db.rollback()
        print(f"[DATABASE ERROR] {e}")
        await message.answer("❌ Ошибка сохранения предложения.")


# BUSINESS CONNECTION

@dp.business_connection()
async def connection_handler(
    connection: types.BusinessConnection
):

    db.execute("""
        INSERT OR REPLACE INTO connections (
            id,
            owner_id,
            enabled,
            can_reply
        )
        VALUES (?, ?, ?, ?)
    """, (
        connection.id,
        connection.user.id,
        int(connection.is_enabled),
        int(getattr(connection.rights, "can_reply", False))
    ))

    db.commit()

    print(
        "[BUSINESS] "
        f"enabled={connection.is_enabled}, "
        f"can_reply={getattr(connection.rights, 'can_reply', False)}"
    )


# TELETHON REF COMMAND

@client.on(events.NewMessage(outgoing=True))
async def command_handler(event):

    global global_last_sent, blocked_until

    if not event.is_private:
        return

    if OWNER_ID is None or event.sender_id != OWNER_ID:
        return

    raw = (event.raw_text or "").strip()
    parts = raw.split()

    if not parts:
        return

    command = parts[0].lower()

    if command != "/ref":
        return

    if len(parts) != 2:
        await panel(
            "❌ Формат: <code>/ref CODE</code>"
        )
        return

    code = parts[1].upper()

    async with lock:

        row = db.execute(
            "SELECT * FROM offers WHERE code=?",
            (code,)
        ).fetchone()

        if not row:
            await panel("❌ Код не найден.")
            return

        if row["status"] != "created":
            await panel(
                "❌ Предложение уже использовано или обработано."
            )
            return

        connection = get_connection(OWNER_ID)

        if not connection:
            await panel(
                "❌ <b>Business-подключение не найдено.</b>\n\n"
                "Проверь подключение бота и право can_reply."
            )
            return

        wait = wait_time(event.chat_id)

        if wait > 0:
            await panel(
                f"⏳ Отправка приостановлена.\n"
                f"Повтори через <b>{wait} секунд.</b>"
            )
            return

        currency = "⭐️" if row["currency"].lower() == "stars" else "💎"
        gift_url = html.escape(row["gift_url"], quote=True)
        nft_name = html.escape(gift_name(row["gift_url"]))

        if row["language"] == "ru":
            text = (
                f"Пользователь <b>{html.escape(SELLER_NAME)}</b> "
                f"предлагает Вам "
                f"<b>{html.escape(row['amount'])} {currency}</b> "
                f"за подарок <a href=\"{gift_url}\"><b>{nft_name}</b></a>.\n\n"
                f"<b>Действует еще 6 часов.</b>"
            )

        else:
            text = (
                f"User <b>{html.escape(SELLER_NAME)}</b> "
                f"offers you "
                f"<b>{html.escape(row['amount'])} {currency}</b> "
                f"for the gift <a href=\"{gift_url}\"><b>{nft_name}</b></a>.\n\n"
                f"<b>Valid for another 6 hours.</b>"
            )

        try:

            sent = await bot.send_message(
                chat_id=event.chat_id,
                business_connection_id=connection["id"],
                text=text,
                reply_markup=keyboard(
                    code,
                    row["gift_url"]
                ),
                link_preview_options=LinkPreviewOptions(
                    url=row["gift_url"],
                    prefer_large_media=True,
                    show_above_text=True
                )
            )

            db.execute("""
                UPDATE offers
                SET status='sent',
                    used=1,
                    chat_id=?,
                    connection_id=?,
                    message_id=?
                WHERE code=?
                  AND status='created'
            """, (
                event.chat_id,
                connection["id"],
                sent.message_id,
                code
            ))

            db.commit()

            global_last_sent = time.monotonic()
            last_sent[event.chat_id] = time.monotonic()

            await panel(
                "<b>✅ ПРЕДЛОЖЕНИЕ ОТПРАВЛЕНО</b>\n\n"
                f"🎁 NFT: <code>{html.escape(gift_name(row['gift_url']))}</code>\n"
                f"💰 Сумма: <b>{html.escape(row['amount'])} "
                f"{html.escape(row['currency'])}</b>\n"
                f"🔑 Код: <code>{code}</code>"
            )

        except TelegramRetryAfter as e:

            blocked_until = time.monotonic() + e.retry_after

            await panel(
                f"⏳ <b>Telegram ограничил отправку.</b>\n"
                f"Повтори через {e.retry_after} секунд."
            )

        except TelegramAPIError as e:

            print(f"[TELEGRAM ERROR] {e}")

            await panel(
                "❌ <b>Ошибка Telegram API</b>\n\n"
                f"<code>{html.escape(str(e)[:1000])}</code>"
            )

        except Exception as e:

            print(f"[ERROR] {type(e).__name__}: {e}")

            await panel(
                "❌ <b>Ошибка отправки</b>\n\n"
                f"<code>{html.escape(str(e)[:1000])}</code>"
            )


# BUTTONS


@dp.callback_query()
async def callback_handler(callback: types.CallbackQuery):

    if not callback.data or ":" not in callback.data:
        await callback.answer()
        return

    action, code = callback.data.split(":", 1)

    if action not in ("accept", "decline", "how"):
        await callback.answer()
        return

    message = callback.message

    if not isinstance(message, types.Message):
        await callback.answer()
        return

    async with lock:

        offer = db.execute(
            "SELECT * FROM offers WHERE code=?",
            (code,)
        ).fetchone()

        if not offer:
            await callback.answer(
                "Предложение не найдено.",
                show_alert=True
            )
            return

        if (
            offer["message_id"] != message.message_id
            or offer["chat_id"] != message.chat.id
        ):
            await callback.answer(
                "Недействительное предложение.",
                show_alert=True
            )
            return

        connection_id = offer["connection_id"]
        gift_url = offer["gift_url"]
        nft_title = gift_name(gift_url)
        safe_url = html.escape(gift_url, quote=True)

        # КАК ПЕРЕДАТЬ

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

        # ПРОВЕРКА СТАТУСА

        if offer["status"] != "sent":
            await callback.answer(
                "Предложение уже обработано.",
                show_alert=True
            )
            return

        if callback.from_user.id == offer["creator_id"]:
            await callback.answer(
                "Нельзя обработать собственное предложение.",
                show_alert=True
            )
            return

        # ПРИНЯТИЕ

        if action == "accept":

            new_keyboard = InlineKeyboardMarkup(
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
                            callback_data=f"how:{code}"
                        )
                    ]
                ]
            )

            new_text = (
                f'<a href="{safe_url}">&#8203;</a>'
                f"🎁 <b>Подарок:</b> "
                f'<a href="{safe_url}"><b>{html.escape(nft_title)}</b></a> '
                f"<b>успешно продан!</b>\n\n"
                f"<b>Передайте подарок покупателю для зачисления звёзд.</b>"
            )

            try:

                await bot.edit_message_text(
                    chat_id=offer["chat_id"],
                    message_id=offer["message_id"],
                    business_connection_id=connection_id,
                    text=new_text,
                    reply_markup=new_keyboard,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(
                        is_disabled=False,
                        url=gift_url,
                        prefer_large_media=True,
                        show_above_text=True
                    )
                )

                db.execute("""
                    UPDATE offers
                    SET status='accepted'
                    WHERE code=? AND status='sent'
                """, (code,))

                db.commit()

                await callback.answer("Предложение принято!")
                print("[ACCEPTED]", code)

                await panel(
                    "<b>✅ ПРЕДЛОЖЕНИЕ ПРИНЯТО</b>\n\n"
                    f"🎁 NFT: <code>{html.escape(nft_title)}</code>\n"
                    f"🔑 Код: <code>{code}</code>"
                )

            except TelegramBadRequest as e:

                await callback.answer(
                    "Не удалось изменить сообщение.",
                    show_alert=True
                )
                print("[EDIT ERROR]", e)

            except TelegramAPIError as e:

                await callback.answer(
                    "Ошибка Telegram API.",
                    show_alert=True
                )
                print("[TELEGRAM ERROR]", e)

        # ОТКЛОНЕНИЕ

        elif action == "decline":

            try:

                await bot.edit_message_text(
                    chat_id=offer["chat_id"],
                    message_id=offer["message_id"],
                    business_connection_id=connection_id,
                    text="<b>Предложение отклонено.</b>",
                    parse_mode="HTML",
                    reply_markup=None,
                    link_preview_options=LinkPreviewOptions(
                        is_disabled=True
                    )
                )

                db.execute("""
                    UPDATE offers
                    SET status='declined'
                    WHERE code=? AND status='sent'
                """, (code,))

                db.commit()

                await callback.answer("Предложение отклонено!")
                print("[DECLINED]", code)

                await panel(
                    "<b>❌ ПРЕДЛОЖЕНИЕ ОТКЛОНЕНО</b>\n\n"
                    f"🎁 NFT: <code>{html.escape(nft_title)}</code>\n"
                    f"🔑 Код: <code>{code}</code>"
                )

            except TelegramBadRequest as e:

                await callback.answer(
                    "Ошибка изменения сообщения.",
                    show_alert=True
                )
                print("[EDIT ERROR]", e)

            except TelegramAPIError as e:

                await callback.answer(
                    "Ошибка Telegram API.",
                    show_alert=True
                )
                print("[TELEGRAM ERROR]", e)

# MAIN

async def main():

    global OWNER_ID

    if not API_ID or not API_HASH or not PHONE or not BOT_TOKEN:
        raise RuntimeError(
            "Заполни API_ID, API_HASH, PHONE и BOT_TOKEN."
        )

    print("[1/4] Подключение Telethon...")

    try:
        await asyncio.wait_for(
            client.start(phone=PHONE),
            timeout=30
        )
    except asyncio.TimeoutError:
        print("[ERROR] Telethon не подключился за 30 секунд.")
        return

    me = await client.get_me()
    OWNER_ID = me.id

    print(f"[2/4] Аккаунт подключён: {me.id}")

    print("[3/4] Проверка Business-бота...")

    try:
        bot_me = await asyncio.wait_for(
            bot.get_me(),
            timeout=10
        )
    except asyncio.TimeoutError:
        print("[ERROR] Bot API не ответил за 10 секунд.")
        await client.disconnect()
        return

    print(f"[BOT] @{bot_me.username}")

    await bot.delete_webhook(drop_pending_updates=True)

    print("[4/4] Запуск polling...")
    print("[READY] Оба клиента запущены.")
    print("[PANEL] .buy ru/eng и /help")
    print("[REF] /ref CODE в личке с получателем")

    await asyncio.gather(
        client.run_until_disconnected(),
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )
    )


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("[STOP] Остановка.")

    except Exception as e:
        print(f"[FATAL] {type(e).__name__}: {e}")

    finally:
        db.close()