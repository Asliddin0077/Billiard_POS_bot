import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "billiardpos-secret")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SITE_URL = os.getenv("SITE_URL", "https://www.billiardpos.uz")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("billiard-bot")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Toshkent vaqti (UTC+5, yozgi vaqt yo'q). zoneinfo ishlatmaymiz - Windows'da tzdata talab qiladi.
TZ = timezone(timedelta(hours=5))
DAY_START_HOUR = 8          # klub kuni 08:00 dan 08:00 gacha
REPORT_HOUR = 9             # kunlik hisobot soat 09:00 da
REMINDER_HOUR = 10          # obuna eslatmasi soat 10:00 da
MILESTONES = [25, 50, 100, 150, 200]


@dp.error()
async def global_error_handler(event: ErrorEvent):
    log.error("Kutilmagan xatolik: %s", event.exception, exc_info=True)
    try:
        update = event.update
        chat_id = None
        if update.message:
            chat_id = update.message.chat.id
        elif update.callback_query:
            chat_id = update.callback_query.message.chat.id
        if chat_id:
            await bot.send_message(chat_id, "😵 Voy, nimadir xato ketdi. Iltimos /start bosib qaytadan urinib ko'ring — men shu yerdaman!")
        if chat_id != ADMIN_CHAT_ID:
            await bot.send_message(ADMIN_CHAT_ID, f"🐞 Bot xatosi: {event.exception}")
    except Exception:
        pass
    return True


# ---- Biznes ma'lumotlari ----
RECEIVING_CARD = "9860 0803 9957 0243"
OWNER_NAME_HINTS = ["Amiriddinov", "Asliddin"]

TIER_NAME = {"basic": "Oddiy", "pro": "Pro"}
TIER_ICON = {"basic": "🟢", "pro": "⭐"}
TIER_FEATURES = {
    "basic": "stollar va vaqt hisobi, bar, smena, statistika",
    "pro": "Oddiy tarifdagi hamma narsa + Sklad, Qarz daftari, Xodimlar va oylik, Moliya, "
           "har kuni ertalab Telegram'ga hisobot va Posik tabriklari",
}


def tier_of(plan_or_user: dict | None) -> str:
    """Tarif darajasi. Eski yozuvlarda bo'sh bo'lsa - 'pro' (hech kimning imkoniyati kamaymasin)."""
    if not plan_or_user:
        return "pro"
    t = plan_or_user.get("tier") or plan_or_user.get("plan_tier")
    return t if t in ("basic", "pro") else "pro"


def plan_title(p: dict) -> str:
    t = tier_of(p)
    return f"{TIER_ICON[t]} {TIER_NAME[t]} · {p['label']}"


async def get_active_plans() -> list[dict]:
    res = supabase.table("subscription_plans").select("*").eq("active", True).order("days").execute()
    plans = res.data or []
    return sorted(plans, key=lambda p: (0 if tier_of(p) == "basic" else 1, p.get("days") or 0))


async def get_plan(plan_id: str) -> dict | None:
    if not plan_id or not re.fullmatch(r"[0-9a-fA-F-]{8,40}", plan_id):
        return None
    try:
        res = supabase.table("subscription_plans").select("*").eq("id", plan_id).limit(1).execute()
    except Exception:
        return None
    return res.data[0] if res.data else None


def linked_user(chat_id: int) -> dict | None:
    res = supabase.table("users").select("*").eq("telegram_chat_id", chat_id).order("created_at", desc=True).limit(1).execute()
    return res.data[0] if res.data else None


class Flow(StatesGroup):
    awaiting_login = State()
    awaiting_plan = State()
    awaiting_receipt = State()
    support = State()


# ---------------- Klaviaturalar ----------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Obunani to'lash / uzaytirish", callback_data="pay")],
        [InlineKeyboardButton(text="💬 Posikka yozish (savol, taklif)", callback_data="support")],
        [InlineKeyboardButton(text="📊 Kunlik hisobot (Pro)", callback_data="report_info")],
        [InlineKeyboardButton(text="🌐 Ilovani ochish", url=SITE_URL)],
    ])


def build_plan_keyboard(plans: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{plan_title(p)} — {fmt_money(p['price'])}", callback_data=f"plan:{p['id']}")] for p in plans]
    rows.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]])


def renew_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Obunani uzaytirish", callback_data="pay")],
        [InlineKeyboardButton(text="💬 Posikka yozish", callback_data="support")],
    ])


def fmt_money(n) -> str:
    return f"{int(round(n or 0)):,}".replace(",", " ") + " so'm"


def last4(card: str) -> str:
    digits = "".join(c for c in card if c.isdigit())
    return digits[-4:]


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def compute_new_until(current_until_str: str | None, days: int) -> datetime:
    """Qolgan kunlar ustiga qo'shiladi: 15 kun qolgan + 30 kun = 45 kun.
    Ilova bilan bir xil: obuna Toshkent vaqti bilan soat 08:00 da tugaydi."""
    now = datetime.now(timezone.utc)
    current = parse_dt(current_until_str)
    base = current if (current and current > now) else now
    target = (base + timedelta(days=days)).astimezone(TZ).replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    return target.astimezone(timezone.utc)


def fmt_date(d: datetime) -> str:
    return d.astimezone(TZ).strftime("%d.%m.%Y")


def sub_status(u: dict) -> str:
    if not u:
        return "—"
    if u.get("account_type") == "vip":
        return "👑 VIP (cheksiz)"
    until = parse_dt(u.get("subscription_until"))
    if until:
        return f"{'✅ faol' if until > datetime.now(timezone.utc) else '⏳ tugagan'}, {fmt_date(until)} gacha"
    return "✅ faol" if u.get("subscribed") else "❌ obunasiz"


# ---------------- /start ----------------
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    args = message.text.split(maxsplit=1)
    arg = args[1].strip() if len(args) > 1 else ""

    if arg.startswith("support"):
        uid = arg.split("_", 1)[1] if "_" in arg else None
        await enter_support(message, state, uid)
        return
    if arg.startswith("link_"):
        await link_account(message, arg[5:])
        return
    if arg in ("renew", "pay"):
        await begin_payment(message, state)
        return
    if arg:
        plan = await get_plan(arg)
        if plan and plan.get("active"):
            await begin_payment(message, state, plan_id=plan["id"])
            return

    u = linked_user(message.chat.id)
    hello = f", {html.escape(u['name'])}" if u else ""
    await message.answer(
        f"Assalomu alaykum{hello}! Men <b>Posik</b> 💜 — Billiard POS yordamchisiman.\n\n"
        "Men bilan obunani to'laysiz, savol va takliflaringizni adminga yetkazaman, "
        "Pro tarifda esa har kuni ertalab klubingiz hisobotini yuborib turaman.\n\n"
        "Nima qilamiz?",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "menu")
async def menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer("Nima qilamiz? 😊", reply_markup=main_menu())


# ---------------- Bekor qilish ----------------
@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qildim 👌 Kerak bo'lsam — /start", reply_markup=main_menu())


@dp.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Bekor qilindi")
    await callback.message.answer("Bekor qildim 👌 Kerak bo'lsam — shu yerdaman.", reply_markup=main_menu())


# ---------------- Telegram'ni hisobga xavfsiz ulash (ilovadagi tugma orqali) ----------------
async def link_account(message: Message, token: str):
    if not re.fullmatch(r"[A-Za-z0-9]{16,48}", token or ""):
        await message.answer("Havola noto'g'ri ko'rinadi 🤔 Ilovadagi tugmani qayta bosib ko'ring.")
        return
    token_hash = hashlib.sha256(token.encode()).hexdigest()   # bazada faqat xesh saqlanadi
    res = supabase.table("users").select("*").eq("tg_link_token", token_hash).limit(1).execute()
    u = res.data[0] if res.data else None
    exp = parse_dt(u.get("tg_link_expires")) if u else None
    if not u or (exp and exp < datetime.now(timezone.utc)):
        await message.answer("Bu havola eskirgan yoki ishlatilgan 🙈 Ilovadan yangisini oling.")
        return
    supabase.table("users").update({
        "telegram_chat_id": message.chat.id,
        "telegram_username": message.from_user.username,
        "telegram_verified": True,
        "tg_link_token": None,
        "tg_link_expires": None,
        "daily_report": True,
    }).eq("id", u["id"]).execute()
    tier = tier_of(u)
    extra = (f"Har kuni ertalab soat {REPORT_HOUR:02d}:00 da kechagi kun hisobotini yuboraman, "
             "yaxshi natijalarda esa tabriklab turaman! 🎉") if tier == "pro" else \
        ("Hozir tarifingiz <b>Oddiy</b>. Kunlik hisobot va tabriklar <b>Pro</b> tarifda ishlaydi — "
         "Pro'ga o'tsangiz, darhol yuborishni boshlayman 😉")
    await message.answer(
        f"Ulandik, {html.escape(u['name'])}! 🤝 Endi men sizning klubingiz yordamchisiman.\n\n{extra}",
        parse_mode="HTML", reply_markup=main_menu())


@dp.callback_query(F.data == "report_info")
async def report_info(callback: CallbackQuery):
    await callback.answer()
    u = linked_user(callback.message.chat.id)
    if u and u.get("telegram_verified") and tier_of(u) == "pro":
        on = u.get("daily_report", True) is not False
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🔕 Hisobotni o'chirish" if on else "🔔 Hisobotni yoqish", callback_data="report_toggle")]])
        await callback.message.answer(
            f"📊 Kunlik hisobot {'yoqilgan ✅' if on else 'o‘chirilgan'}.\n"
            f"Har kuni soat {REPORT_HOUR:02d}:00 da kechagi kun natijalarini yuboraman.",
            reply_markup=kb)
    elif u and u.get("telegram_verified"):
        await callback.message.answer(
            "📊 Kunlik hisobot <b>Pro</b> tarifda ishlaydi.\n"
            f"Pro'da: {TIER_FEATURES['pro']}.\n\nPro'ga o'tish uchun obunani tanlang 👇",
            parse_mode="HTML", reply_markup=renew_keyboard())
    else:
        await callback.message.answer(
            "Hisobot yuborishim uchun avval Telegram'ingizni klubingizga ulash kerak 🔗\n\n"
            "Ilovada: <b>Sozlamalar → Telegram'da kunlik hisobot</b> tugmasini bosing — "
            "u sizni shu yerga olib keladi va men sizni taniyman.",
            parse_mode="HTML")


@dp.callback_query(F.data == "report_toggle")
async def report_toggle(callback: CallbackQuery):
    u = linked_user(callback.message.chat.id)
    if not u:
        await callback.answer("Avval akkauntni ulang", show_alert=True)
        return
    on = u.get("daily_report", True) is not False
    supabase.table("users").update({"daily_report": not on}).eq("id", u["id"]).execute()
    await callback.answer("Saqlandi")
    await callback.message.answer("🔕 Hisobotni o'chirdim. Kerak bo'lsa qayta yoqasiz." if on else "🔔 Hisobotni yoqdim! Ertaga ertalab ko'rishamiz ☀️")


# ---------------- To'lov ----------------
@dp.callback_query(F.data == "pay")
async def pay_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await begin_payment(callback.message, state)


async def begin_payment(message: Message, state: FSMContext, plan_id: str | None = None):
    await state.clear()
    await state.update_data(plan_id=plan_id)
    u = linked_user(message.chat.id)
    if u:
        await state.update_data(login=u["login"], user_id=u["id"], user_name=u["name"])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Ha, {u['login']} uchun", callback_data="pay_me")],
            [InlineKeyboardButton(text="🔄 Boshqa login", callback_data="pay_other")],
        ])
        await message.answer(
            f"Siz <b>{html.escape(u['name'])}</b> (login: <code>{html.escape(u['login'])}</code>) hisobiga to'layapsizmi?\n"
            f"Hozirgi obuna: {sub_status(u)}",
            parse_mode="HTML", reply_markup=kb)
        return
    await state.set_state(Flow.awaiting_login)
    await message.answer(
        "Zo'r! 😊 Avval saytdagi <b>login</b>ingizni yuboring — qaysi klub ekanini bilib olay.\n\n"
        "(Istalgan vaqtda /cancel)",
        parse_mode="HTML", reply_markup=cancel_keyboard())


@dp.callback_query(F.data == "pay_me")
async def pay_me(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await after_user_known(callback.message, state)


@dp.callback_query(F.data == "pay_other")
async def pay_other(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    await state.clear()
    await state.update_data(plan_id=data.get("plan_id"))
    await state.set_state(Flow.awaiting_login)
    await callback.message.answer("Mayli, qaysi <b>login</b> uchun to'laymiz?", parse_mode="HTML", reply_markup=cancel_keyboard())


@dp.message(Flow.awaiting_login)
async def got_login(message: Message, state: FSMContext):
    login = (message.text or "").strip()
    if not login or len(login) > 64:
        await message.answer("Loginni matn ko'rinishida yuboring 🙂")
        return
    res = supabase.table("users").select("*").ilike("login", login).limit(1).execute()
    if not res.data:
        await message.answer("Hmm, bunday login topilmadi 🤔 Qaytadan yozib ko'ring yoki avval saytda ro'yxatdan o'ting.")
        return
    user = res.data[0]
    await state.update_data(login=user["login"], user_id=user["id"], user_name=user["name"])
    # Chat'ni eslab qolamiz (to'lov xabarlari va obuna eslatmalari uchun).
    # Hisobot faqat ilovadagi xavfsiz havola orqali ulanganlarga boradi (telegram_verified).
    # Ilova orqali tasdiqlangan (verified) ulanishni login bilan almashtirib bo'lmaydi.
    if not (user.get("telegram_verified") and user.get("telegram_chat_id") != message.chat.id):
        supabase.table("users").update(
            {"telegram_chat_id": message.chat.id, "telegram_username": message.from_user.username}
        ).eq("id", user["id"]).execute()
    await after_user_known(message, state)


async def after_user_known(message: Message, state: FSMContext):
    data = await state.get_data()
    plan = await get_plan(data.get("plan_id")) if data.get("plan_id") else None
    if plan:
        await ask_receipt(message, state, plan)
        return
    plans = await get_active_plans()
    if not plans:
        await message.answer("Hozircha tariflar sozlanmagan 🙈 Birozdan so'ng qayta urinib ko'ring.")
        return
    await state.set_state(Flow.awaiting_plan)
    tiers = sorted({tier_of(p) for p in plans}, key=lambda t: 0 if t == "basic" else 1)
    lines = [f"{TIER_ICON[t]} <b>{TIER_NAME[t]}</b>: {TIER_FEATURES[t]}" for t in tiers]
    await message.answer(
        "Qaysi tarifni tanlaysiz? 👇\n\n" + "\n\n".join(lines) +
        "\n\nℹ️ Yangi muddat qolgan kunlaringiz ustiga qo'shiladi.",
        parse_mode="HTML", reply_markup=build_plan_keyboard(plans))


async def ask_receipt(message: Message, state: FSMContext, plan: dict):
    await state.update_data(plan_id=plan["id"], plan_price=plan["price"], plan_label=plan_title(plan),
                            plan_days=plan["days"], plan_tier=tier_of(plan))
    await state.set_state(Flow.awaiting_receipt)
    await message.answer(
        f"Ajoyib tanlov! 😊\nTarif: <b>{html.escape(plan_title(plan))}</b> — {fmt_money(plan['price'])}\n\n"
        f"💳 Shu kartaga to'lang:\n<code>{RECEIVING_CARD}</code>\n\n"
        "Keyin chekni (screenshot) shu yerga tashlang — men tekshirib, adminga yetkazaman.",
        parse_mode="HTML", reply_markup=cancel_keyboard())


@dp.callback_query(F.data.startswith("plan:"))
async def chose_plan(callback: CallbackQuery, state: FSMContext):
    plan = await get_plan(callback.data.split(":", 1)[1])
    if not plan:
        await callback.answer("Tarif topilmadi", show_alert=True)
        return
    data = await state.get_data()
    await callback.answer()
    if not data.get("user_id"):
        await begin_payment(callback.message, state, plan_id=plan["id"])
        return
    await ask_receipt(callback.message, state, plan)


@dp.message(Flow.awaiting_plan)
async def plan_via_text(message: Message):
    await message.answer("Iltimos, tugmalardan birini bosing 👆")


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
    await message.answer("Chekni oldim! 💜 Bir daqiqa, tekshiryapman...")

    data = await state.get_data()
    plan_id = data["plan_id"]
    plan_label = data["plan_label"]
    plan_tier = data.get("plan_tier") or "pro"
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
        f"Ism: {html.escape(user_name)}\n"
        f"Sayt login: {html.escape(login)}\n"
        f"{contact_line}\n"
        f"Tarif: {html.escape(plan_label)} ({fmt_money(expected_amount)})\n\n"
        f"AI tekshiruvi:\n"
        f"- Summa mos: {'✅' if verdict.get('amount_matches') else '❌'} (aniqlangan: {verdict.get('amount', '?')})\n"
        f"- Karta/Ism mos: {'✅' if (card_ok or name_ok) else '❌'}\n"
        f"- Chek ko'rinishida: {'✅' if verdict.get('looks_like_valid_receipt') else '❌'}"
    )

    if auto_approve:
        new_until = activate_subscription(user_id, plan_days, plan_tier, expected_amount, plan_label)
        await message.answer(
            f"Tayyor! 🎉 To'lovingiz tasdiqlandi.\n"
            f"Tarif: <b>{html.escape(plan_label)}</b>\nObuna: <b>{fmt_date(new_until)}</b> gacha faol.\n\n"
            "Ilovaga qaytib, ishlayvering — men doim yoningizdaman 💜",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌐 Ilovani ochish", url=SITE_URL)]]))
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🚫 Bekor qilish (soxta chek)", callback_data=f"revoke:{user_id}")]]
        )
        await bot.send_photo(ADMIN_CHAT_ID, photo.file_id, caption=caption + "\n\n✅ AVTOMATIK TASDIQLANDI (nazorat uchun)", reply_markup=kb, parse_mode="HTML")
    else:
        supabase.table("users").update({"pending_plan": plan_id}).eq("id", user_id).execute()
        await message.answer("Chekni adminga yubordim 📨 U tekshirib tasdiqlashi bilan sizga darhol xabar beraman!")
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


def activate_subscription(user_id: str, days: int, tier: str, amount: int, label: str | None) -> datetime:
    cur = supabase.table("users").select("subscription_until").eq("id", user_id).single().execute()
    new_until = compute_new_until((cur.data or {}).get("subscription_until"), days)
    supabase.table("users").update(
        {"subscribed": True, "subscription_until": new_until.isoformat(), "pending_plan": None, "plan_tier": tier}
    ).eq("id", user_id).execute()
    supabase.table("payments").insert({"user_id": user_id, "amount": amount, "plan_label": label}).execute()
    return new_until


@dp.message(Flow.awaiting_receipt)
async def wrong_content(message: Message):
    await message.answer("Iltimos, chekning rasmini (screenshot) yuboring 📸")


# ---------------- Admin: Tasdiqlash / Rad etish / Bekor qilish ----------------
async def notify_user(user_id: str, text: str, kb: InlineKeyboardMarkup | None = None):
    u = supabase.table("users").select("telegram_chat_id").eq("id", user_id).limit(1).execute()
    if u.data and u.data[0].get("telegram_chat_id"):
        await bot.send_message(u.data[0]["telegram_chat_id"], text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("approve:"))
async def admin_approve(callback: CallbackQuery):
    if callback.message.chat.id != ADMIN_CHAT_ID:
        return
    user_id = callback.data.split(":")[1]
    u = supabase.table("users").select("pending_plan").eq("id", user_id).single().execute()
    plan = await get_plan((u.data or {}).get("pending_plan"))
    days = plan["days"] if plan else 30
    new_until = activate_subscription(user_id, days, tier_of(plan), plan["price"] if plan else 0, plan_title(plan) if plan else None)
    await callback.answer("Tasdiqlandi ✅")
    await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n✅ QO'LDA TASDIQLANDI")
    await notify_user(user_id, f"Tayyor! 🎉 To'lovingiz tasdiqlandi. Obunangiz <b>{fmt_date(new_until)}</b> gacha faol.\nYaxshi ishlang! 💜")


@dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(callback: CallbackQuery):
    if callback.message.chat.id != ADMIN_CHAT_ID:
        return
    user_id = callback.data.split(":")[1]
    supabase.table("users").update({"pending_plan": None}).eq("id", user_id).execute()
    await callback.answer("Rad etildi")
    await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n❌ RAD ETILDI")
    await notify_user(
        user_id,
        "Hmm, chekda muammo bor ekan 😕 To'lov tasdiqlanmadi.\nIltimos, to'g'ri chekni qaytadan yuboring yoki menga yozing — birga hal qilamiz.",
        renew_keyboard(),
    )


@dp.callback_query(F.data.startswith("revoke:"))
async def admin_revoke(callback: CallbackQuery):
    if callback.message.chat.id != ADMIN_CHAT_ID:
        return
    user_id = callback.data.split(":")[1]
    supabase.table("users").update({"subscribed": False, "subscription_until": None}).eq("id", user_id).execute()
    last_payment = supabase.table("payments").select("id").eq("user_id", user_id).order("created_at", desc=True).limit(1).execute()
    if last_payment.data:
        supabase.table("payments").delete().eq("id", last_payment.data[0]["id"]).execute()
    await callback.answer("Obuna bekor qilindi")
    await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n🚫 BEKOR QILINDI (soxta chek)")


# ---------------- Support: Posik orqali adminga yozish ----------------
@dp.callback_query(F.data == "support")
async def support_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await enter_support(callback.message, state, None)


async def enter_support(message: Message, state: FSMContext, app_user_id: str | None):
    await state.clear()
    await state.set_state(Flow.support)
    if app_user_id and re.fullmatch(r"[0-9a-fA-F-]{8,40}", app_user_id):
        await state.update_data(app_user_id=app_user_id)
    await message.answer(
        "Men tinglayapman! 💜\n\n"
        "Savolingiz, taklifingiz yoki muammoingizni yozing — rasm, video yoki ovozli xabar ham bo'ladi. "
        "Men darhol adminga yetkazaman, javob ham shu yerga keladi.\n\n"
        "Tugatgach — /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Tugatish", callback_data="cancel")]]))


def support_header(message: Message, data: dict) -> str:
    u = None
    if data.get("app_user_id"):
        r = supabase.table("users").select("*").eq("id", data["app_user_id"]).limit(1).execute()
        u = r.data[0] if r.data else None
    if not u:
        u = linked_user(message.chat.id)
    tg = message.from_user
    who = html.escape(u["name"]) + f" (<code>{html.escape(u['login'])}</code>)" if u else html.escape(tg.full_name or "Noma'lum")
    lines = [
        "💬 <b>Posik orqali murojaat</b>",
        f"👤 {who}",
    ]
    if u:
        lines.append(f"🏷 {TIER_ICON[tier_of(u)]} {TIER_NAME[tier_of(u)]} · {sub_status(u)}")
    lines.append(f"📱 @{tg.username}" if tg.username else f'📱 <a href="tg://user?id={tg.id}">profil</a>')
    lines.append(f"🆔 {message.chat.id}")
    return "\n".join(lines)


async def forward_to_admin(message: Message, state: FSMContext):
    data = await state.get_data()
    head = support_header(message, data)
    tail = "\n\n↩️ <i>Javob berish uchun shu xabarga Reply qiling</i>"
    if message.text:
        await bot.send_message(ADMIN_CHAT_ID, f"{head}\n━━━━━━━━━━\n{html.escape(message.text)}{tail}", parse_mode="HTML")
    elif message.content_type in ("photo", "video", "document", "voice", "audio", "animation"):
        cap = head + ("\n━━━━━━━━━━\n" + html.escape(message.caption) if message.caption else "") + tail
        await bot.copy_message(ADMIN_CHAT_ID, message.chat.id, message.message_id, caption=cap[:1000], parse_mode="HTML")
    else:
        await bot.send_message(ADMIN_CHAT_ID, head + tail, parse_mode="HTML")
        await bot.copy_message(ADMIN_CHAT_ID, message.chat.id, message.message_id)
    await message.answer("Yetkazdim! 📨 Admin javob berishi bilan shu yerga yuboraman. Yana nimadir bo'lsa — yozavering.")


@dp.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply(message: Message):
    src = message.reply_to_message
    m = re.search(r"🆔\s*(-?\d+)", (src.text or src.caption or ""))
    if not m:
        await message.answer("Bu xabarda mijoz ID'si yo'q. Murojaat xabarining o'ziga (🆔 bor xabarga) Reply qiling.")
        return
    user_chat = int(m.group(1))
    try:
        if message.text:
            await bot.send_message(user_chat, f"💜 <b>Posik:</b> admin javob berdi\n\n{html.escape(message.text)}", parse_mode="HTML")
        else:
            await bot.send_message(user_chat, "💜 <b>Posik:</b> admin javob berdi 👇", parse_mode="HTML")
            await bot.copy_message(user_chat, message.chat.id, message.message_id)
        await message.answer("✅ Yuborildi")
    except Exception as e:
        await message.answer(f"❌ Yuborib bo'lmadi: {e}")


@dp.message(Flow.support)
async def support_message(message: Message, state: FSMContext):
    await forward_to_admin(message, state)


# ---------------- Admin: foydalanuvchilar ----------------
@dp.message(Command("users"))
async def list_users(message: Message):
    if message.chat.id != ADMIN_CHAT_ID:
        return
    res = supabase.table("users").select("name, login, subscribed, account_type, subscription_until, plan_tier, telegram_verified").order("created_at", desc=True).execute()
    rows = res.data or []
    if not rows:
        await message.answer("Hali foydalanuvchi yo'q.")
        return
    lines = []
    for u in rows:
        t = tier_of(u)
        tg = " 📲" if u.get("telegram_verified") else ""
        lines.append(f"{u['name']} (@{u['login']}) — {TIER_ICON[t]}{TIER_NAME[t]} · {sub_status(u)}{tg}")
    text = f"👥 Jami foydalanuvchilar: {len(rows)}\n(📲 = Telegram ulangan)\n\n" + "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i: i + 3500])


@dp.message(Command("tariflar"))
async def list_plans(message: Message):
    if message.chat.id != ADMIN_CHAT_ID:
        return
    plans = await get_active_plans()
    if not plans:
        await message.answer("Faol tarif yo'q. Admin panelda qo'shing.")
        return
    await message.answer("💳 Faol tariflar (narxlar admin paneldan o'zgartiriladi):\n\n" +
                         "\n".join(f"{plan_title(p)} — {fmt_money(p['price'])}" for p in plans))


# ---------------- Admin: umumiy hisobot ----------------
@dp.message(Command("hisobot"))
async def report(message: Message):
    if message.chat.id != ADMIN_CHAT_ID:
        return

    now = datetime.now(timezone.utc)
    today_start = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    month_start = now.astimezone(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    pay_today = supabase.table("payments").select("amount").gte("created_at", today_start.isoformat()).execute()
    pay_month = supabase.table("payments").select("amount").gte("created_at", month_start.isoformat()).execute()
    pay_all = supabase.table("payments").select("amount").execute()
    revenue_today = sum(p["amount"] or 0 for p in (pay_today.data or []))
    revenue_month = sum(p["amount"] or 0 for p in (pay_month.data or []))
    revenue_all = sum(p["amount"] or 0 for p in (pay_all.data or []))

    users = supabase.table("users").select("id, name, subscribed, account_type, created_at, plan_tier").execute().data or []
    total_users = len(users)
    new_today = len([u for u in users if (parse_dt(u["created_at"]) or now) >= today_start])
    active_subs = len([u for u in users if u.get("subscribed") or u.get("account_type") == "vip"])
    vip_count = len([u for u in users if u.get("account_type") == "vip"])
    basic_count = len([u for u in users if u.get("plan_tier") == "basic"])
    inactive = total_users - active_subs

    ht = supabase.table("session_history").select("owner_id, total").gte("end_time", today_start.isoformat()).execute().data or []
    active_owners_today = len(set(r["owner_id"] for r in ht))
    tables_closed_today = len(ht)
    avg_check_today = (sum(r["total"] or 0 for r in ht) / len(ht)) if ht else 0

    hm = supabase.table("session_history").select("owner_id").gte("end_time", month_start.isoformat()).execute().data or []
    counts = {}
    for r in hm:
        counts[r["owner_id"]] = counts.get(r["owner_id"], 0) + 1
    top_owner_name, top_count = None, 0
    if counts:
        top_owner_id = max(counts, key=counts.get)
        top_count = counts[top_owner_id]
        u = supabase.table("users").select("name").eq("id", top_owner_id).limit(1).execute()
        if u.data:
            top_owner_name = u.data[0]["name"]

    text = (
        f"📊 <b>Hisobot</b> — {now.astimezone(TZ).strftime('%d.%m.%Y')}\n\n"
        f"👥 <b>Foydalanuvchilar</b>\n"
        f"Jami: {total_users} · Bugun qo'shilgan: {new_today}\n"
        f"Faol obuna: {active_subs} (VIP: {vip_count}) · Obunasiz: {inactive}\n"
        f"Tarif: 🟢 Oddiy {basic_count} · ⭐ Pro {total_users - basic_count}\n\n"
        f"💰 <b>Daromad</b>\n"
        f"Bugun: {fmt_money(revenue_today)}\n"
        f"Shu oy: {fmt_money(revenue_month)}\n"
        f"Jami (umr bo'yi): {fmt_money(revenue_all)}\n\n"
        f"🎯 <b>Faollik (bugun)</b>\n"
        f"Faol klublar: {active_owners_today}\n"
        f"Yopilgan stollar: {tables_closed_today}\n"
        f"O'rtacha chek: {fmt_money(avg_check_today)}\n"
    )
    if top_owner_name:
        text += f"\n🏆 <b>Oyning eng faol mijozi:</b> {html.escape(top_owner_name)} ({top_count} marta stol yopgan)"
    await message.answer(text, parse_mode="HTML")


# ---------------- Boshqa har qanday xabar -> support ----------------
@dp.message()
async def fallback(message: Message, state: FSMContext):
    if message.chat.id == ADMIN_CHAT_ID:
        return
    if message.text and message.text.startswith("/"):
        await message.answer("Bu buyruqni bilmayman 🙈 Mana menyu:", reply_markup=main_menu())
        return
    await state.set_state(Flow.support)
    await forward_to_admin(message, state)


# =====================================================================
#                    Posik: avtomatik xabarlar
# =====================================================================
def business_day_bounds(now_local: datetime, days_back: int = 0) -> tuple[datetime, datetime]:
    """Klub kuni 08:00 dan keyingi kun 08:00 gacha. days_back=1 -> kechagi kun."""
    start = now_local.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if now_local < start:
        start -= timedelta(days=1)
    start -= timedelta(days=days_back)
    return start, start + timedelta(days=1)


def claim_notification(owner_id: str, kind: str, day: str) -> bool:
    """Bir xil xabar ikki marta ketmasligi uchun (bot qayta ishga tushsa ham)."""
    try:
        supabase.table("bot_notifications").insert({"owner_id": str(owner_id), "kind": kind, "day": day}).execute()
        return True
    except Exception:
        return False


def owner_is_active(u: dict) -> bool:
    if u.get("account_type") == "vip":
        return True
    until = parse_dt(u.get("subscription_until"))
    if until:
        return until > datetime.now(timezone.utc)
    return bool(u.get("subscribed"))


def pro_report_owners() -> list[dict]:
    res = supabase.table("users").select("*").eq("telegram_verified", True).not_.is_("telegram_chat_id", "null").execute()
    return [u for u in (res.data or [])
            if (u.get("role") or "owner") == "owner" and tier_of(u) == "pro"
            and u.get("daily_report", True) is not False and owner_is_active(u)]


def sessions_between(owner_ids: list[str], a: datetime, b: datetime) -> list[dict]:
    if not owner_ids:
        return []
    res = supabase.table("session_history").select("owner_id, table_name, hall_name, duration_seconds, table_cost, extras_cost, total, payment_method, end_time") \
        .in_("owner_id", owner_ids).gte("end_time", a.astimezone(timezone.utc).isoformat()).lt("end_time", b.astimezone(timezone.utc).isoformat()).execute()
    return res.data or []


def build_daily_report(u: dict, rows: list[dict], prev_count: int, day_start: datetime) -> str:
    n = len(rows)
    name = html.escape(u["name"])
    if n == 0:
        return (f"☀️ Xayrli tong, {name}! Men Posik 💜\n\n"
                f"Kecha ({day_start.strftime('%d.%m')}) birorta ham stol yopilmabdi.\n"
                "Bugun omad sizga yor bo'lsin! 🍀")
    total = sum(r.get("total") or 0 for r in rows)
    tcost = sum(r.get("table_cost") or 0 for r in rows)
    bar = sum(r.get("extras_cost") or 0 for r in rows)
    pay = {}
    for r in rows:
        k = (r.get("payment_method") or "naqd").lower()
        pay[k] = pay.get(k, 0) + (r.get("total") or 0)
    tables = {}
    for r in rows:
        k = r.get("table_name") or "?"
        tables[k] = tables.get(k, 0) + 1
    top_t = max(tables, key=tables.get)
    avg_min = sum(r.get("duration_seconds") or 0 for r in rows) / n / 60
    icons = {"naqd": "💵 Naqd", "karta": "💳 Karta", "qarz": "📒 Qarz"}
    pay_line = " · ".join(f"{icons.get(k, k.capitalize())}: {fmt_money(v)}" for k, v in sorted(pay.items(), key=lambda x: -x[1]))
    diff = n - prev_count
    if prev_count and diff > 0:
        mood = f"Oldingi kundan {diff} ta ko'p — zo'r natija! 🚀"
    elif prev_count and diff < 0:
        mood = "Oldingi kundan biroz kamroq. Bugun to'ldiramiz! 💪"
    else:
        mood = "Yaxshi ishladingiz! 👏"
    return (
        f"☀️ Xayrli tong, {name}! Men Posik 💜\n"
        f"Kechagi kun hisoboti ({day_start.strftime('%d.%m')}, 08:00 – 08:00):\n\n"
        f"🎱 Yopilgan stollar: <b>{n}</b>\n"
        f"💰 Jami tushum: <b>{fmt_money(total)}</b>\n"
        f"   ⏱ Stol vaqti: {fmt_money(tcost)}\n"
        f"   🥤 Bar: {fmt_money(bar)}\n"
        f"   {pay_line}\n"
        f"🏆 Eng faol stol: <b>{html.escape(str(top_t))}</b> ({tables[top_t]} marta)\n"
        f"⏳ O'rtacha o'yin: {avg_min:.0f} daqiqa\n\n"
        f"{mood}"
    )


async def send_daily_reports():
    now_local = datetime.now(TZ)
    today = now_local.strftime("%Y-%m-%d")
    owners = pro_report_owners()
    if not owners:
        return
    y_start, y_end = business_day_bounds(now_local, 1)
    p_start, p_end = business_day_bounds(now_local, 2)
    ids = [u["id"] for u in owners]
    rows = sessions_between(ids, y_start, y_end)
    prev = sessions_between(ids, p_start, p_end)
    for u in owners:
        if not claim_notification(u["id"], "daily_report", today):
            continue
        mine = [r for r in rows if r["owner_id"] == u["id"]]
        prev_n = len([r for r in prev if r["owner_id"] == u["id"]])
        try:
            await bot.send_message(u["telegram_chat_id"], build_daily_report(u, mine, prev_n, y_start), parse_mode="HTML")
        except Exception as e:
            log.warning("Hisobot yuborilmadi %s: %s", u.get("login"), e)
        await asyncio.sleep(0.1)


CONGRATS = {
    25: "🎉 Voy-bo'! Bugun allaqachon <b>25 ta</b> stol yopildi! Ish qizib ketdi 🔥",
    50: "🎊 Tabriklayman! Bugun <b>50 ta</b> stol yopildi! Zo'r kun bo'lyapti! 💪",
    100: "🏆 VOY! Bugun <b>100 ta</b> stol!!! Siz haqiqiy chempionsiz! 🎉🎉🎉",
    150: "🚀 <b>150 ta</b> stol! Bunaqasini hali ko'rmagandim! Rekord kun! 🤩",
    200: "👑 <b>200 ta</b> stol! Afsonaviy kun! Men sizdan faxrlanaman! 💜",
}


async def check_milestones():
    now_local = datetime.now(TZ)
    owners = pro_report_owners()
    if not owners:
        return
    d_start, d_end = business_day_bounds(now_local, 0)
    day = d_start.strftime("%Y-%m-%d")
    rows = sessions_between([u["id"] for u in owners], d_start, d_end)
    counts = {}
    for r in rows:
        counts[r["owner_id"]] = counts.get(r["owner_id"], 0) + 1
    for u in owners:
        n = counts.get(u["id"], 0)
        reached = [m for m in MILESTONES if n >= m]
        if not reached:
            continue
        m = reached[-1]
        # oraliq bosqichlarni "egallab" qo'yamiz, faqat eng kattasi uchun xabar ketadi
        for mm in reached[:-1]:
            claim_notification(u["id"], f"milestone_{mm}", day)
        if claim_notification(u["id"], f"milestone_{m}", day):
            try:
                await bot.send_message(u["telegram_chat_id"], f"{CONGRATS[m]}\n\n— Posik 💜", parse_mode="HTML")
            except Exception as e:
                log.warning("Tabrik yuborilmadi: %s", e)


async def send_expiry_reminders():
    now = datetime.now(timezone.utc)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    res = supabase.table("users").select("*").not_.is_("telegram_chat_id", "null").not_.is_("subscription_until", "null").execute()
    for u in res.data or []:
        if u.get("account_type") == "vip" or (u.get("role") or "owner") != "owner":
            continue
        until = parse_dt(u.get("subscription_until"))
        if not until:
            continue
        left = (until - now).total_seconds() / 86400
        if 2 < left <= 3:
            kind, text = "expiry_3d", (f"Salom, {html.escape(u['name'])}! Men Posik 💜\n"
                                       f"Obunangiz <b>3 kundan</b> keyin ({fmt_date(until)}) tugaydi. "
                                       "Ish to'xtab qolmasligi uchun hozir uzaytirib qo'yamizmi? Qolgan kunlar kuyib ketmaydi — ustiga qo'shiladi 😉")
        elif 0 < left <= 1:
            kind, text = "expiry_1d", (f"⏰ {html.escape(u['name'])}, obunangiz <b>ertaga</b> ({fmt_date(until)}) tugaydi!\n"
                                       "Uzaytirib qo'ysangiz, men xotirjam bo'laman 🙏")
        elif -1 < left <= 0:
            kind, text = "expired", (f"😢 {html.escape(u['name'])}, obunangiz tugadi.\n"
                                     "Ilovaga kirishda davom etish uchun obunani yangilang — 2 daqiqalik ish!")
        else:
            continue
        if claim_notification(u["id"], kind, today):
            try:
                await bot.send_message(u["telegram_chat_id"], text, parse_mode="HTML", reply_markup=renew_keyboard())
            except Exception as e:
                log.warning("Eslatma yuborilmadi: %s", e)
            await asyncio.sleep(0.1)


async def scheduler():
    """Har daqiqada vaqtni tekshiradi. Xabarlar bot_notifications orqali faqat bir marta ketadi."""
    last_milestone = 0.0
    while True:
        try:
            now_local = datetime.now(TZ)
            if now_local.hour == REPORT_HOUR:
                await send_daily_reports()
            if now_local.hour == REMINDER_HOUR:
                await send_expiry_reminders()
            if asyncio.get_event_loop().time() - last_milestone > 300:
                last_milestone = asyncio.get_event_loop().time()
                await check_milestones()
        except Exception as e:
            log.error("Scheduler xatosi: %s", e, exc_info=True)
        await asyncio.sleep(60)


# ---------------- Render uchun kichik "tirikligim bor" server ----------------
async def health(request):
    from aiohttp import web
    return web.Response(text="Billiard POS bot (Posik) ishlayapti ✅")


async def new_user_webhook(request):
    from aiohttp import web
    if request.rel_url.query.get("secret") != WEBHOOK_SECRET:
        return web.Response(status=403, text="forbidden")
    try:
        payload = await request.json()
        record = payload.get("record", {})
        name = html.escape(str(record.get("name", "?")))
        phone = html.escape(str(record.get("phone", "?")))
        login = html.escape(str(record.get("login", "?")))
        created = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
        text = (
            f"🆕 <b>Yangi ro'yxatdan o'tish!</b>\n\n"
            f"Ism: {name}\nTelefon: {phone}\nLogin: @{login}\nVaqt: {created}"
        )
        await bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML")
    except Exception as e:
        log.error("new_user_webhook xatosi: %s", e)
    return web.Response(text="ok")


async def start_health_server():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/webhook/new-user", new_user_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health-check server %s portida ishga tushdi", port)


# ---------------- Ishga tushirish ----------------
async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Menyu"),
        BotCommand(command="cancel", description="Bekor qilish"),
    ])
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Menyu"),
            BotCommand(command="users", description="Foydalanuvchilar ro'yxati"),
            BotCommand(command="tariflar", description="Faol tariflar"),
            BotCommand(command="hisobot", description="Umumiy hisobot"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID),
    )
    await start_health_server()
    asyncio.create_task(scheduler())
    log.info("Posik ishga tushdi (polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
