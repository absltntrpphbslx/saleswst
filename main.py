import asyncio
import hashlib
import hmac
import html
import json
import os
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import uvicorn

import database as db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")
ADMIN_TG_ID_RAW = os.getenv("ADMIN_TG_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Добавь его в переменные окружения (.env).")
if not WEBAPP_URL:
    raise RuntimeError("Не задан WEBAPP_URL. Добавь его в переменные окружения (.env).")

try:
    ADMIN_TG_ID = int(ADMIN_TG_ID_RAW) if ADMIN_TG_ID_RAW else None
except ValueError:
    raise RuntimeError("ADMIN_TG_ID должен быть числом — Telegram ID владельца.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db.init_db()


# ---------------- Общие проверки ----------------


def is_admin(tg_id: int) -> bool:
    return ADMIN_TG_ID is not None and int(tg_id) == ADMIN_TG_ID


def require_admin(tg_id: int):
    if not is_admin(tg_id):
        raise HTTPException(status_code=403, detail="Доступ только для владельца")


def validate_init_data(init_data: str) -> dict:
    """Проверяет, что данные действительно пришли из Telegram (а не подделаны)."""
    if not init_data:
        raise HTTPException(status_code=401, detail="Нет данных авторизации")
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            raise ValueError("bad hash")
        user = json.loads(parsed["user"])
        return user
    except Exception:
        raise HTTPException(status_code=401, detail="Не удалось подтвердить пользователя")


def upsert_from_tg_user(user: dict):
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return db.upsert_user(user["id"], user.get("username"), full_name)


# ---------------- Telegram-бот ----------------


@dp.message(Command("start"))
async def cmd_start(message: Message):
    upsert_from_tg_user({
        "id": message.from_user.id,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "last_name": message.from_user.last_name,
    })
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📊 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )
    admin_note = "\n\n🔐 Ты владелец бота: в приложении доступен раздел «Настройки»." if is_admin(message.from_user.id) else ""
    await message.answer(
        "Привет! Здесь можно вносить свои продажи, смотреть статистику, клиентов и таблицу лидеров.\n\n"
        "Команды:\n"
        "/leaderboard — таблица лидеров текстом\n"
        "/setgroup — включить уведомления о продажах в этом групповом чате\n"
        "/setcommission [процент] — задать долю воркера от суммы\n"
        "/setmilestone [сумма] — порог крупной сделки\n"
        "/buyer [юзернейм] — посмотреть историю покупателя"
        + admin_note,
        reply_markup=kb,
    )


@dp.message(Command("setmilestone"))
async def cmd_setmilestone(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Эта настройка доступна только владельцу бота.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].replace(".", "", 1).isdigit():
        await message.answer(
            "Использование: /setmilestone 50000\n"
            "Порог будет применяться к суммарным покупкам одного клиента."
        )
        return
    amount = float(parts[1])
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return
    db.set_config("buyer_milestone_amount", str(amount))
    await message.answer(f"✅ Порог крупной сделки установлен: {amount:.0f}₽.")


@dp.message(Command("buyer"))
async def cmd_buyer(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /buyer username  (без @)")
        return
    username = parts[1].lstrip("@").strip()
    stats = db.get_buyer_stats(username)
    if not stats["items"]:
        await message.answer(f"У покупателя @{html.escape(username)} пока нет зафиксированных покупок.")
        return
    text = (
        f"👤 Покупатель: @{html.escape(username)}\n"
        f"💰 Всего потрачено: {stats['total']:.0f}₽\n"
        f"🧾 Покупок: {stats['count']}\n\n"
        f"Последние покупки:\n"
    )
    for item in stats["items"][:10]:
        cat = f" [{item['category']}]" if item["category"] else ""
        text += f"• {item['product']}{cat} — {item['amount']:.0f}₽\n"
    await message.answer(text)


@dp.message(Command("setcommission"))
async def cmd_setcommission(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Эта настройка доступна только владельцу бота.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].replace(".", "", 1).isdigit():
        await message.answer("Использование: /setcommission 70  (воркер получает 70% с продажи)")
        return
    percent = float(parts[1])
    if not (0 < percent <= 100):
        await message.answer("Процент должен быть от 1 до 100.")
        return
    db.set_config("commission_percent", str(percent))
    await message.answer(f"✅ Доля воркера установлена: {percent:.0f}% от суммы продажи.")


@dp.message(Command("setgroup"))
async def cmd_setgroup(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Эта настройка доступна только владельцу бота.")
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эту команду нужно написать в групповом чате, куда слать уведомления о продажах.")
        return
    db.set_config("group_chat_id", str(message.chat.id))
    await message.answer("✅ Этот чат теперь будет получать уведомления о новых продажах.")


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(Command("leaderboard"))
async def cmd_leaderboard(message: Message):
    rows = db.get_leaderboard("all")
    if not rows:
        await message.answer("Пока нет ни одной транзакции.")
        return
    text = "🏆 Таблица лидеров (всё время):\n\n"
    for i, r in enumerate(rows[:15], 1):
        name = r["full_name"] or r["username"] or "Без имени"
        text += f"{i}. {html.escape(name)} — {r['total']:.0f}₽ ({r['count']} прод.)\n"
    await message.answer(text)


# ---------------- Backend мини-приложения ----------------

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.post("/api/transactions")
async def create_transaction(request: Request):
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))

    product = str(body.get("product", "")).strip()[:200]
    category = str(body.get("category", "")).strip()[:100] or None
    buyer_username = str(body.get("buyer_username", "")).strip().lstrip("@")[:100] or None
    try:
        amount = float(body.get("amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректная сумма")
    if not product or amount <= 0:
        raise HTTPException(status_code=400, detail="Заполни продукт и сумму")

    upsert_from_tg_user(user)

    old_top3 = db.get_top_tg_ids("all", limit=3)
    buyer_total_before = db.get_buyer_total(buyer_username) if buyer_username else 0

    db.add_transaction(user["id"], product, amount, category, buyer_username)

    group_chat_id = db.get_config("group_chat_id")
    if group_chat_id:
        display_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get("username") or "Кто-то"
        commission_percent = db.get_config("commission_percent")
        share = amount * (float(commission_percent) / 100) if commission_percent else amount

        lines = [
            "🏆 <b>Новый профит!</b>",
            "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️",
            f"👤 Продавец: {html.escape(display_name)}",
        ]
        if category:
            lines.append(f"📦 Категория: {html.escape(category)}")
        lines.append(f"📝 Товар: {html.escape(product)}")
        lines.append(f"💰 Сумма: {amount:.0f}₽")
        lines.append(f"💵 Доля: {share:.0f}₽")
        lines.append("▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️")
        try:
            await bot.send_message(int(group_chat_id), "\n".join(lines))
        except Exception:
            pass

        if buyer_username:
            milestone = float(db.get_config("buyer_milestone_amount") or 50000)
            buyer_total_after = buyer_total_before + amount
            old_tier = int(buyer_total_before // milestone)
            new_tier = int(buyer_total_after // milestone)
            if new_tier > old_tier:
                milestone_text = (
                    "💎 <b>КРУПНАЯ СДЕЛКА!</b>\n\n"
                    f"Один из клиентов суммарно закупился уже на {new_tier * milestone:.0f}₽+ 🔥🔥🔥"
                )
                try:
                    await bot.send_message(int(group_chat_id), milestone_text)
                except Exception:
                    pass

    new_top3 = db.get_top_tg_ids("all", limit=3)
    dropped_out = [tg_id for tg_id in old_top3 if tg_id not in new_top3 and tg_id != user["id"]]
    for tg_id in dropped_out:
        try:
            await bot.send_message(
                tg_id,
                "😤 Тебя обогнали! Ты вылетел(а) из топ-3 общего зачёта.\nОткрой приложение и вернись на пьедестал 🏆",
            )
        except Exception:
            pass

    return {"ok": True}


@app.post("/api/leaderboard")
async def leaderboard(request: Request):
    body = await request.json()
    validate_init_data(body.get("initData", ""))
    period = str(body.get("period", "all"))
    return db.get_leaderboard(period)


@app.post("/api/me")
async def me(request: Request):
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    upsert_from_tg_user(user)
    return db.get_user_stats(user["id"])


@app.post("/api/goal")
async def set_goal(request: Request):
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    try:
        amount = float(body.get("amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректная сумма")
    if amount < 0:
        raise HTTPException(status_code=400, detail="Сумма не может быть отрицательной")
    db.set_monthly_goal(user["id"], amount)
    return {"ok": True}


@app.post("/api/buyers")
async def buyers(request: Request):
    body = await request.json()
    validate_init_data(body.get("initData", ""))
    username = str(body.get("username", "")).strip().lstrip("@").strip()[:100]
    if not username:
        raise HTTPException(status_code=400, detail="Укажи юзернейм покупателя")
    return db.get_buyer_stats(username)


@app.post("/api/settings")
async def get_settings(request: Request):
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    require_admin(user["id"])
    return {
        "commission_percent": float(db.get_config("commission_percent") or 100),
        "buyer_milestone_amount": float(db.get_config("buyer_milestone_amount") or 50000),
        "admin_configured": ADMIN_TG_ID is not None,
    }


@app.post("/api/settings/save")
async def save_settings(request: Request):
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    require_admin(user["id"])

    try:
        commission = float(body.get("commission_percent"))
        milestone = float(body.get("buyer_milestone_amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Проверь значения")

    if not (0 < commission <= 100):
        raise HTTPException(status_code=400, detail="Комиссия должна быть от 1 до 100%")
    if milestone <= 0:
        raise HTTPException(status_code=400, detail="Порог должен быть больше нуля")

    db.set_config("commission_percent", str(commission))
    db.set_config("buyer_milestone_amount", str(milestone))
    return {"ok": True}


app.mount("/webapp", StaticFiles(directory="webapp", html=True), name="webapp")


# ---------------- Запуск бота и сервера вместе ----------------


async def run_bot():
    await dp.start_polling(bot)


async def run_all():
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)), log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(run_bot(), server.serve())


if __name__ == "__main__":
    asyncio.run(run_all())
