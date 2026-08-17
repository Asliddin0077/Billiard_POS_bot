import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.types import BotCommand
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("billiard-bot")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ---- Biznes ma'lumotlari ----
RECEIVING_CARD = "9860 0803 9957 0243"
OWNER_NAME_HINTS = ["Amiriddinov", "Asliddin"]


async def get_active_plans() -> list[dict]:
    res = supabase.table("subscription_plans").select("*").eq("active", True).order("days").execute()
    return res.data or []


async def get_plan(plan_id: str) -> dict | None:
    if not plan_id:
        return None
    res = supabase.table("subscription_plans").select("*").eq("id", plan_id).limit(1).execute()
    return res.data[0] if res.data else None


class Flow(StatesGroup):
    awaiting_login = State()
    awaiting_plan = State()
    awaiting_receipt = State()


def build_plan_keyboard(plans: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{p['label']} — {fmt_money(p['price'])}", callback_data=f"plan:{p['id']}")] for p in plans]
    rows.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]])


def fmt_money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " so'm"


def last4(card: str) -> str:
    digits = "".join(c for c in card if c.isdigit())
    return digits[-4:]


def compute_new_until(current_until_str: str | None, days: int) -> datetime:
    now = datetime.now(timezone.utc)
    current = None
    if current_until_str:
        try:
            current = datetime.fromisoformat(current_until_str)
        except Exception:
            current = None
    base = current if (current and current > now) else now
    return base + timedelta(days=days)


# ---------------- /start ----------------
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    args = message.text.split(maxsplit=1)
    plan_id = None
    if len(args) > 1:
        candidate = args[1].strip()
        plan = await get_plan(candidate)
        if plan and plan.get("active"):
            plan_id = candidate
    await state.update_data(plan_id=plan_id)
    await state.set_state(Flow.awaiting_login)
    await message.answer(
        "Assalomu alaykum! Billiard POS obunasi uchun avval saytdagi <b>login</b>ingizni yuboring.\n\n"
        "(Istalgan vaqtda bekor qilish uchun /cancel yozing)",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


# ---------------- Bekor qilish (istalgan bosqichda ishlaydi) ----------------
@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi. Qayta boshlash uchun /start yozing.")


@dp.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Bekor qilindi")
    await callback.message.answer("Bekor qilindi. Qayta boshlash uchun /start yozing.")


# ---------------- Login qabul qilish ----------------
@dp.message(Flow.awaiting_login)
async def got_login(message: Message, state: FSMContext):
    login = (message.text or "").strip()
    res = supabase.table("users").select("*").ilike("login", login).limit(1).execute()
    if not res.data:
        await message.answer("Bunday login topilmadi. Qaytadan urinib ko'ring yoki avval saytda ro'yxatdan o'ting.")
        return
    user = res.data[0]

    data = await state.get_data()
    await state.update_data(login=login, user_id=user["id"], user_name=user["name"])

    # Foydalanuvchini o'zining Telegram chat_id'si orqali bog'laymiz (login orqali emas - aniqroq va adashmaydi)
    tg_username = message.from_user.username
    supabase.table("users").update(
        {"telegram_chat_id": message.chat.id, "telegram_username": tg_username}
    ).eq("id", user["id"]).execute()

    plan_id = data.get("plan_id")
    plan = await get_plan(plan_id) if plan_id else None
    if plan:
        await state.update_data(plan_id=plan["id"], plan_price=plan["price"], plan_label=plan["label"], plan_days=plan["days"])
        await state.set_state(Flow.awaiting_receipt)
        await message.answer(
            f"Tarif: <b>{plan['label']}</b> — {fmt_money(plan['price'])}.\n\n"
            f"Kartaga ({RECEIVING_CARD}) to'lovni amalga oshiring va chekni (screenshot) shu yerga yuboring.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
    else:
        await state.set_state(Flow.awaiting_plan)
        plans = await get_active_plans()
        if not plans:
            await message.answer("Hozircha tariflar sozlanmagan. Birozdan so'ng qayta urinib ko'ring.")
            return
        await message.answer("Qaysi tarifni tanlaysiz?", reply_markup=build_plan_keyboard(plans))


# ---------------- Tarif tanlash ----------------
@dp.callback_query(F.data.startswith("plan:"))
async def chose_plan(callback: CallbackQuery, state: FSMContext):
    plan_id = callback.data.split(":", 1)[1]
    plan = await get_plan(plan_id)
    if not plan:
        await callback.answer("Tarif topilmadi", show_alert=True)
        return
    await state.update_data(plan_id=plan["id"], plan_price=plan["price"], plan_label=plan["label"], plan_days=plan["days"])
    await state.set_state(Flow.awaiting_receipt)
    await callback.answer()
    await callback.message.answer(
        f"Tarif: <b>{plan['label']}</b> — {fmt_money(plan['price'])}.\n\n"
        f"Kartaga ({RECEIVING_CARD}) to'lovni amalga oshiring va chekni (screenshot) shu yerga yuboring.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@dp.message(Flow.awaiting_plan)
async def plan_via_text(message: Message):
    await message.answer("Iltimos tugmalardan birini bosing.")


# ---------------- AI orqali chekni tekshirish ----------------
async def verify_receipt(image_bytes: bytes, mime_type: str, expected_amount: int) -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    prompt = f"""Bu O'zbekiston bank/to'lov ilovasidan olingan to'lov cheki (screenshot). Quyidagilarni tekshir va FAQAT JSON qaytar, boshqa hech narsa yozma:
{{
  "amount": <chekdagi summa, faqat raqam, so'mda>,
  "amount_matches": <true agar summa {expected_amount} so'mga teng yoki juda yaqin bo'lsa>,
  "recipient_card_last4": "<qabul qiluvchi kartaning oxirgi 4 raqami, agar ko'rinsa>",
  "recipient_name_visible": "<chekda ko'ringan qabul qiluvchi ism-familiyasi, agar bor bo'lsa>",
  "looks_like_valid_receipt": <true/false - bu chindan ham to'lov cheki ko'rinishidami>
}}
Qabul qiluvchi karta: {RECEIVING_CARD}. Qabul qiluvchi ism: Amiriddinov Asliddin (yoki qisqartirilgan/bosh harflar bilan bo'lishi mumkin)."""

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 500,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            },
        )
        data = res.json()

    text = "".join(b.get("text", "") for b in data.get("content", []))
    try:
        clean = text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception:
        log.warning("AI javobini o'qib bo'lmadi: %s", text)
        return {"amount_matches": False, "looks_like_valid_receipt": False, "raw": text}


# ---------------- Chek qabul qilish ----------------
@dp.message(Flow.awaiting_receipt, F.photo)
async def got_receipt(message: Message, state: FSMContext):
    await message.answer("Chek qabul qilindi, tekshirilmoqda...")

    data = await state.get_data()
    plan_id = data["plan_id"]
    plan_label = data["plan_label"]
    user_id = data["user_id"]
    user_name = data["user_name"]
    login = data["login"]
    expected_amount = data["plan_price"]
    plan_days = data["plan_days"]

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes_io = await bot.download_file(file.file_path)
    image_bytes = file_bytes_io.read()

    verdict = await verify_receipt(image_bytes, "image/jpeg", expected_amount)

    card_ok = bool(verdict.get("recipient_card_last4")) and str(verdict["recipient_card_last4"])[-4:] == last4(RECEIVING_CARD)
    name_visible = str(verdict.get("recipient_name_visible") or "").lower()
    name_ok = any(h.lower() in name_visible for h in OWNER_NAME_HINTS)
    auto_approve = bool(verdict.get("looks_like_valid_receipt")) and bool(verdict.get("amount_matches")) and (card_ok or name_ok)

    tg_username = message.from_user.username
    contact_line = f"Telegram: @{tg_username}" if tg_username else f'Telegram: <a href="tg://user?id={message.from_user.id}">shu yerga bosing</a>'

    caption = (
        f"💳 Yangi to'lov\n"
        f"Ism: {user_name}\n"
        f"Sayt login: {login}\n"
        f"{contact_line}\n"
        f"Tarif: {plan_label} ({fmt_money(expected_amount)})\n\n"
        f"AI tekshiruvi:\n"
        f"- Summa mos: {'✅' if verdict.get('amount_matches') else '❌'} (aniqlangan: {verdict.get('amount', '?')})\n"
        f"- Karta/Ism mos: {'✅' if (card_ok or name_ok) else '❌'}\n"
        f"- Chek ko'rinishida: {'✅' if verdict.get('looks_like_valid_receipt') else '❌'}"
    )

    if auto_approve:
        cur = supabase.table("users").select("subscription_until").eq("id", user_id).single().execute()
        new_until = compute_new_until((cur.data or {}).get("subscription_until"), plan_days)
        supabase.table("users").update(
            {"subscribed": True, "subscription_until": new_until.isoformat(), "pending_plan": None}
        ).eq("id", user_id).execute()
        await message.answer(f"✅ To'lovingiz avtomatik tasdiqlandi! Obunangiz {new_until.strftime('%d.%m.%Y')} gacha faol. Saytga qaytib kirishingiz mumkin.")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🚫 Bekor qilish (soxta chek)", callback_data=f"revoke:{user_id}")]]
        )
        await bot.send_photo(ADMIN_CHAT_ID, photo.file_id, caption=caption + "\n\n✅ AVTOMATIK TASDIQLANDI (nazorat uchun)", reply_markup=kb, parse_mode="HTML")
    else:
        supabase.table("users").update({"pending_plan": plan_id}).eq("id", user_id).execute()
        await message.answer("To'lovingiz tekshirilmoqda, tez orada admin tomonidan tasdiqlanadi.")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve:{user_id}"),
                    InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject:{user_id}"),
                ]
            ]
        )
        await bot.send_photo(ADMIN_CHAT_ID, photo.file_id, caption=caption + "\n\n⚠️ QO'LDA TEKSHIRISH KERAK", reply_markup=kb, parse_mode="HTML")

    await state.clear()


@dp.message(Flow.awaiting_receipt)
async def wrong_content(message: Message):
    await message.answer("Iltimos, chekning rasmini (screenshot) yuboring.")


# ---------------- Admin: Tasdiqlash / Rad etish / Bekor qilish ----------------
async def notify_user(user_id: str, text: str):
    u = supabase.table("users").select("telegram_chat_id").eq("id", user_id).limit(1).execute()
    if u.data and u.data[0].get("telegram_chat_id"):
        await bot.send_message(u.data[0]["telegram_chat_id"], text, parse_mode="HTML")


@dp.callback_query(F.data.startswith("approve:"))
async def admin_approve(callback: CallbackQuery):
    user_id = callback.data.split(":")[1]
    u = supabase.table("users").select("subscription_until, pending_plan").eq("id", user_id).single().execute()
    pending_plan_id = (u.data or {}).get("pending_plan")
    plan = await get_plan(pending_plan_id)
    days = plan["days"] if plan else 30
    new_until = compute_new_until((u.data or {}).get("subscription_until"), days)
    supabase.table("users").update(
        {"subscribed": True, "subscription_until": new_until.isoformat(), "pending_plan": None}
    ).eq("id", user_id).execute()
    await callback.answer("Tasdiqlandi ✅")
    await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n✅ QO'LDA TASDIQLANDI")
    await notify_user(user_id, f"✅ To'lovingiz tasdiqlandi! Obunangiz {new_until.strftime('%d.%m.%Y')} gacha faol.")


@dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(callback: CallbackQuery):
    user_id = callback.data.split(":")[1]
    supabase.table("users").update({"pending_plan": None}).eq("id", user_id).execute()
    await callback.answer("Rad etildi")
    await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n❌ RAD ETILDI")
    await notify_user(user_id, "❌ To'lov cheki tasdiqlanmadi. Iltimos qaytadan to'g'ri chek yuboring yoki <a href=\"https://t.me/Asliddinamriddinov\">shu yerga yozing</a>.")


@dp.callback_query(F.data.startswith("revoke:"))
async def admin_revoke(callback: CallbackQuery):
    user_id = callback.data.split(":")[1]
    supabase.table("users").update({"subscribed": False}).eq("id", user_id).execute()
    await callback.answer("Obuna bekor qilindi")
    await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n🚫 BEKOR QILINDI (soxta chek)")


# ---------------- Admin: foydalanuvchilarni ko'rish (faqat ko'rish, boshqarish saytdan) ----------------
@dp.message(Command("users"))
async def list_users(message: Message):
    if message.chat.id != ADMIN_CHAT_ID:
        return
    res = supabase.table("users").select("name, login, subscribed, account_type, subscription_until").order("created_at", desc=True).execute()
    rows = res.data or []
    if not rows:
        await message.answer("Hali foydalanuvchi yo'q.")
        return

    now = datetime.now(timezone.utc)
    lines = []
    for u in rows:
        if u.get("account_type") == "vip":
            status = "👑 VIP (cheksiz)"
        elif u.get("subscription_until"):
            try:
                until = datetime.fromisoformat(u["subscription_until"])
            except Exception:
                until = None
            if until and until > now:
                status = f"✅ Faol — {until.strftime('%d.%m.%Y')} gacha"
            elif until:
                status = f"⏳ Muddati tugagan ({until.strftime('%d.%m.%Y')})"
            else:
                status = "✅ Faol" if u.get("subscribed") else "❌ Obunasiz"
        else:
            status = "✅ Faol" if u.get("subscribed") else "❌ Obunasiz"
        lines.append(f"{u['name']} (@{u['login']}) — {status}")

    text = f"👥 Jami foydalanuvchilar: {len(rows)}\n\n" + "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i : i + 3500])


# ---------------- Render uchun kichik "tirikligim bor" server ----------------
async def health(request):
    from aiohttp import web
    return web.Response(text="Billiard POS bot ishlayapti ✅")


async def start_health_server():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health-check server %s portida ishga tushdi", port)


# ---------------- Ishga tushirish ----------------
async def main():
    await bot.set_my_commands([BotCommand(command="start", description="Boshlash / Obuna sotib olish")])
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Boshlash"),
            BotCommand(command="users", description="Foydalanuvchilar ro'yxati"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID),
    )
    await start_health_server()
    log.info("Bot ishga tushdi (polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
