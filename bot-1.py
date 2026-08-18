# -*- coding: utf-8 -*-
"""
ربات تلگرامی اختصاصی گروه
====================================
یک ربات تک‌فایلی برای مدیریت یک گروه تلگرامی خاص با امکانات:
- همگام‌سازی نام ربات با نام گروه
- پنل ادمین در پیوی (فعال‌سازی با ارسال 1212)
- پیام همگانی، تنظیم لینک گروه، مدیریت بازی‌ها، روشن/خاموش کردن هوش مصنوعی
- سیستم کاربران داستان‌دار + درخواست «کاربر اصلی شدن»
- ۱۱ بازی گروهی: دوز، سنگ‌کاغذقیچی، حدس عدد، تاس شانس، شیر یا خط،
  حدس کلمه (دار)، کوییز اطلاعات عمومی، ریاضی سریع، این یا اون، حدس اموجی، بلک‌جک
- چت هوش مصنوعی با گراک (GPT-OSS-120B) + مدیریت خطای موقتی + ری‌ت لیمیت

نحوه اجرا:
    pip install -r requirements.txt
    python bot.py
"""

import os
import re
import time
import random
import string
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# ============================================================================
# تنظیمات (Config)
# ============================================================================

# ترجیحاً این دو مقدار رو به‌جای هاردکد، از متغیرهای محیطی (Environment Variables) بخون
# تا توکن‌ها داخل کد ذخیره نشن. اگه متغیر محیطی ست نشده باشه، از مقدار پیش‌فرض زیر استفاده می‌شه.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8750717991:AAHPqBuR-qrPSjE4RnfA21o12-6qRTk2LmI")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

# مدل و endpoint گراک (سازگار با فرمت OpenAI)
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_API_BASE = "https://api.groq.com/openai/v1/chat/completions"

# فاصله‌ی زمانی مجاز بین دو پیام هر کاربر به هوش مصنوعی (ثانیه)
AI_RATE_LIMIT_SECONDS = 15
# حداقل فاصله‌ی زمانی بین هر دو پاسخ هوش مصنوعی توی کل گروه (صرف‌نظر از اینکه کاربرش کیه).
# این جلوی اینه که چند نفر مختلف همزمان ریپلای بزنن و چندین کال گرون‌قیمت پشت‌سرهم شلیک بشه.
AI_GROUP_COOLDOWN_SECONDS = 5
# سقف طول پاسخ هوش مصنوعی؛ هرچی کمتر، هزینه‌ی هر کال کمتر (برای چت گروهی معمولی کافیه)
AI_MAX_OUTPUT_TOKENS = 1024

DB_PATH = os.environ.get("BOT_DB_PATH", "bot_data.db")
ADMIN_PASSCODE = "1212"  # کد فعال‌سازی پنل ادمین در پیوی

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("group_bot")

# بازی‌های قابل‌پشتیبانی. کلید = شناسه‌ی داخلی، مقادیر = متادیتا
GAMES = {
    "tictactoe": {"trigger": "دوز", "title": "دوز (سه در سه)", "min": 2, "max": 2},
    "rps": {"trigger": "سنگ کاغذ قیچی", "title": "سنگ کاغذ قیچی", "min": 2, "max": 2},
    "guess": {"trigger": "حدس عدد", "title": "حدس عدد (بدون محدودیت نفرات)", "min": 1, "max": None},
    "dice": {"trigger": "تاس شانس", "title": "تاس شانس (دو نفره)", "min": 2, "max": 2},
    "coin": {"trigger": "شیر یا خط", "title": "شیر یا خط (دو نفره)", "min": 2, "max": 2},
    "hangman": {"trigger": "حدس کلمه", "title": "حدس کلمه (دار)", "min": 1, "max": None},
    "trivia": {"trigger": "کوییز", "title": "کوییز اطلاعات عمومی", "min": 1, "max": None},
    "math": {"trigger": "ریاضی", "title": "ریاضی سریع", "min": 1, "max": None},
    "wyr": {"trigger": "این یا اون", "title": "این یا اون", "min": 1, "max": None},
    "emoji": {"trigger": "حدس اموجی", "title": "حدس اموجی", "min": 1, "max": None},
    "bj": {"trigger": "بلک جک", "title": "بلک جک (مقابل دیلر)", "min": 1, "max": None},
}
TRIGGER_TO_GAME = {meta["trigger"]: key for key, meta in GAMES.items()}

# ============================================================================
# دیتابیس
# ============================================================================

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")


def db_init():
    c = _conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute(
        "CREATE TABLE IF NOT EXISTS stories ("
        "user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, story TEXT)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS story_requests ("
        "user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, requested_at TEXT)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS game_settings ("
        "game_key TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1)"
    )
    c.execute("CREATE TABLE IF NOT EXISTS ai_prompts (id INTEGER PRIMARY KEY AUTOINCREMENT, prompt TEXT)")
    c.execute(
        "CREATE TABLE IF NOT EXISTS known_users ("
        "user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, first_seen TEXT)"
    )
    _conn.commit()
    for key in GAMES:
        c.execute("INSERT OR IGNORE INTO game_settings (game_key, enabled) VALUES (?, 1)", (key,))
    _conn.commit()


def get_setting(key: str, default=None):
    row = _conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str):
    _conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    _conn.commit()


def get_owner_id():
    v = get_setting("owner_id")
    return int(v) if v else None


def get_group_id():
    v = get_setting("group_id")
    return int(v) if v else None


def is_ai_enabled() -> bool:
    return get_setting("ai_enabled", "1") == "1"


def is_game_enabled(key: str) -> bool:
    row = _conn.execute("SELECT enabled FROM game_settings WHERE game_key = ?", (key,)).fetchone()
    return bool(row and row[0])


def toggle_game(key: str):
    cur = is_game_enabled(key)
    _conn.execute("UPDATE game_settings SET enabled = ? WHERE game_key = ?", (0 if cur else 1, key))
    _conn.commit()


def remember_user(user):
    _conn.execute(
        "INSERT OR IGNORE INTO known_users (user_id, full_name, username, first_seen) VALUES (?, ?, ?, ?)",
        (user.id, user.full_name, user.username or "", datetime.now(timezone.utc).isoformat()),
    )
    _conn.commit()


def get_story(user_id: int):
    row = _conn.execute("SELECT story FROM stories WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def list_stories():
    return _conn.execute("SELECT user_id, full_name, story FROM stories ORDER BY full_name").fetchall()


def save_story(user_id: int, full_name: str, username: str, story: str):
    _conn.execute(
        "INSERT INTO stories (user_id, full_name, username, story) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET story = excluded.story, full_name = excluded.full_name",
        (user_id, full_name, username or "", story),
    )
    _conn.execute("DELETE FROM story_requests WHERE user_id = ?", (user_id,))
    _conn.commit()


def delete_story(user_id: int):
    _conn.execute("DELETE FROM stories WHERE user_id = ?", (user_id,))
    _conn.commit()


def add_story_request(user_id: int, full_name: str, username: str):
    if get_story(user_id) is not None:
        return False
    _conn.execute(
        "INSERT OR IGNORE INTO story_requests (user_id, full_name, username, requested_at) VALUES (?, ?, ?, ?)",
        (user_id, full_name, username or "", datetime.now(timezone.utc).isoformat()),
    )
    _conn.commit()
    return True


def list_story_requests():
    return _conn.execute("SELECT user_id, full_name, username FROM story_requests ORDER BY requested_at").fetchall()


def reject_story_request(user_id: int):
    _conn.execute("DELETE FROM story_requests WHERE user_id = ?", (user_id,))
    _conn.commit()


def add_ai_prompt(text: str):
    _conn.execute("INSERT INTO ai_prompts (prompt) VALUES (?)", (text,))
    _conn.commit()


def get_ai_system_prompt() -> str:
    rows = _conn.execute("SELECT prompt FROM ai_prompts ORDER BY id").fetchall()
    base = (
        "تو یک دستیار هوش مصنوعی داخل یک گروه تلگرامی هستی. فارسی و دوستانه و طبیعی جواب بده، "
        "مگر اینکه دستور دیگه‌ای بهت داده شده باشه."
    )
    extra = "\n".join(r[0] for r in rows)
    return base + ("\n" + extra if extra else "")


db_init()

# ============================================================================
# وضعیت درون‌حافظه‌ای (In-memory state)
# ============================================================================

# چالش‌های درحال‌انتظار بازی‌های دو نفره: session_id -> dict
PENDING_CHALLENGES = {}
# بازی‌های درحال اجرا: session_id -> dict
ACTIVE_GAMES = {}
# بازی‌های حدس عدد (بدون محدودیت نفر) به ازای هر گروه: chat_id -> dict
GUESS_GAMES = {}
# بازی‌های حدس کلمه/دار به ازای هر گروه: chat_id -> dict
HANGMAN_GAMES = {}
# کوییز اطلاعات عمومی درحال اجرا: session_id -> dict
TRIVIA_GAMES = {}
# ریاضی سریع به ازای هر گروه: chat_id -> dict
MATH_GAMES = {}
# این یا اون درحال اجرا: session_id -> dict
WYR_GAMES = {}
# حدس اموجی به ازای هر گروه: chat_id -> dict
EMOJI_GAMES = {}
# بلک‌جک‌های درحال اجرا (هر کاربر جدا): session_id -> dict
BLACKJACK_GAMES = {}

# ---- بانک محتوای بازی‌ها ----

HANGMAN_WORDS = [
    ("گربه", "حیوانات"), ("فیل", "حیوانات"), ("زرافه", "حیوانات"), ("پروانه", "حیوانات"),
    ("گوسفند", "حیوانات"), ("کبوتر", "حیوانات"), ("روباه", "حیوانات"), ("پلنگ", "حیوانات"),
    ("سیب", "خوراکی"), ("هندوانه", "خوراکی"), ("پلو", "خوراکی"), ("کباب", "خوراکی"),
    ("آش", "خوراکی"), ("زعفران", "خوراکی"), ("گردو", "خوراکی"), ("انار", "خوراکی"),
    ("ایران", "کشورها"), ("ژاپن", "کشورها"), ("برزیل", "کشورها"), ("مصر", "کشورها"),
    ("کتاب", "اشیا"), ("چتر", "اشیا"), ("دوچرخه", "اشیا"), ("عینک", "اشیا"),
    ("گیتار", "اشیا"), ("ساعت", "اشیا"), ("آینه", "اشیا"), ("چراغ", "اشیا"),
    ("دریا", "طبیعت"), ("کوهستان", "طبیعت"), ("رنگین‌کمان", "طبیعت"), ("آبشار", "طبیعت"),
    ("فوتبال", "ورزش"), ("والیبال", "ورزش"), ("شطرنج", "ورزش"), ("کشتی", "ورزش"),
]

TRIVIA_QUESTIONS = [
    {"q": "پایتخت فرانسه کجاست؟", "options": ["برلین", "پاریس", "رم", "مادرید"], "answer": 1, "cat": "جغرافیا"},
    {"q": "بزرگ‌ترین اقیانوس جهان کدام است؟", "options": ["اطلس", "هند", "آرام", "منجمد شمالی"], "answer": 2, "cat": "جغرافیا"},
    {"q": "بلندترین قله ایران چه نام دارد؟", "options": ["سبلان", "دماوند", "علم‌کوه", "زردکوه"], "answer": 1, "cat": "جغرافیا"},
    {"q": "طولانی‌ترین رودخانه‌ی جهان کدام است؟", "options": ["آمازون", "نیل", "میسیسیپی", "یانگ‌تسه"], "answer": 1, "cat": "جغرافیا"},
    {"q": "نزدیک‌ترین سیاره به خورشید کدام است؟", "options": ["زهره", "زمین", "عطارد", "مریخ"], "answer": 2, "cat": "علمی"},
    {"q": "آب از چه دو عنصری تشکیل شده؟", "options": ["هیدروژن و اکسیژن", "کربن و اکسیژن", "نیتروژن و هیدروژن", "هلیوم و اکسیژن"], "answer": 0, "cat": "علمی"},
    {"q": "سریع‌ترین حیوان خشکی جهان کدام است؟", "options": ["شیر", "یوزپلنگ", "اسب", "گورخر"], "answer": 1, "cat": "علمی"},
    {"q": "قلب انسان چند حفره دارد؟", "options": ["۲", "۳", "۴", "۵"], "answer": 2, "cat": "علمی"},
    {"q": "جام جهانی فوتبال هر چند سال یک‌بار برگزار می‌شود؟", "options": ["۲", "۳", "۴", "۵"], "answer": 2, "cat": "ورزش"},
    {"q": "یک بازی فوتبال معمولاً چند دقیقه است؟", "options": ["۶۰", "۷۰", "۸۰", "۹۰"], "answer": 3, "cat": "ورزش"},
    {"q": "در شطرنج، کدام مهره فقط مورب حرکت می‌کند؟", "options": ["اسب", "رخ", "فیل", "وزیر"], "answer": 2, "cat": "ورزش"},
    {"q": "فردوسی نویسنده‌ی کدام اثر است؟", "options": ["گلستان", "شاهنامه", "مثنوی", "بوستان"], "answer": 1, "cat": "فرهنگ"},
    {"q": "نوروز مصادف با شروع کدام فصل است؟", "options": ["زمستان", "بهار", "تابستان", "پاییز"], "answer": 1, "cat": "فرهنگ"},
    {"q": "پول رسمی ژاپن چه نام دارد؟", "options": ["وون", "ین", "یوان", "روپیه"], "answer": 1, "cat": "عمومی"},
    {"q": "بزرگ‌ترین کشور جهان از نظر مساحت کدام است؟", "options": ["چین", "کانادا", "روسیه", "آمریکا"], "answer": 2, "cat": "عمومی"},
    {"q": "زبان رسمی برزیل چیست؟", "options": ["اسپانیایی", "پرتغالی", "انگلیسی", "فرانسوی"], "answer": 1, "cat": "عمومی"},
    {"q": "کدام سیاره به «سیاره‌ی سرخ» معروف است؟", "options": ["زهره", "مشتری", "مریخ", "زحل"], "answer": 2, "cat": "علمی"},
    {"q": "المپیک تابستانی هر چند سال برگزار می‌شود؟", "options": ["۲", "۳", "۴", "۵"], "answer": 2, "cat": "ورزش"},
    {"q": "پایتخت ایران کجاست؟", "options": ["اصفهان", "تبریز", "تهران", "شیراز"], "answer": 2, "cat": "جغرافیا"},
    {"q": "کدام گاز بیشترین حجم هوای کره‌ی زمین را تشکیل می‌دهد؟", "options": ["اکسیژن", "نیتروژن", "دی‌اکسید کربن", "هیدروژن"], "answer": 1, "cat": "علمی"},
]

WYR_QUESTIONS = [
    ("همیشه یک ساعت زودتر همه‌جا برسی", "همیشه یک ساعت دیرتر همه‌جا برسی"),
    ("بتونی پرواز کنی", "بتونی نامرئی بشی"),
    ("تمام عمر پیتزا بخوری", "تمام عمر کباب بخوری"),
    ("همیشه تابستون باشه", "همیشه زمستون باشه"),
    ("ذهن دیگران رو بخونی", "بتونی آینده رو ببینی"),
    ("پولدار ولی تنها باشی", "فقیر ولی دور و برت پر از دوست باشه"),
    ("هیچ‌وقت نتونی دروغ بگی", "هیچ‌وقت نتونی حقیقتو تشخیص بدی"),
    ("همیشه توی ترافیک گیر کنی", "همیشه پرواز رو از دست بدی"),
    ("بدون گوشی یک هفته زندگی کنی", "بدون اینترنت یک ماه زندگی کنی"),
    ("عاشق شغلت باشی ولی حقوق کم بگیری", "از شغلت متنفر باشی ولی حقوق عالی بگیری"),
    ("همیشه برنده بشی ولی تنها بازی کنی", "گاهی ببازی ولی با دوستات بازی کنی"),
    ("بتونی هر زبونی رو بلد باشی", "بتونی هر سازی رو بنوازی"),
    ("صد سال توی گذشته زندگی کنی", "صد سال توی آینده زندگی کنی"),
    ("هر روز صبح زود بیدار بشی", "هر شب خیلی دیر بخوابی"),
    ("بتونی زمان رو متوقف کنی", "بتونی زمان رو برگردونی"),
]

EMOJI_RIDDLES = [
    ("🦁👑", "شیرشاه"),
    ("🕷️👨", "مرد عنکبوتی"),
    ("❄️👸", "فروزن"),
    ("🏠🎈", "بالا"),
    ("🐠🔍", "در جستجوی نمو"),
    ("🚢💔🧊", "تایتانیک"),
    ("👽📞🏠", "ای‌تی"),
    ("🦖🏝️", "پارک ژوراسیک"),
    ("🐝🎬", "فیلم زنبور"),
    ("👦🪄⚡", "هری پاتر"),
    ("🍫🏭", "چارلی و کارخانه‌ی شکلات‌سازی"),
    ("🐭🧀", "موش و پنیر"),
    ("🌧️☂️😢", "روز بارونی"),
    ("🔥🐉", "اژدها"),
    ("🌙⭐️😴", "شب بخیر"),
]
# آخرین زمان چت هر کاربر با هوش مصنوعی: user_id -> timestamp
AI_LAST_USED = {}
# آخرین زمانی که هوش مصنوعی توی هر گروه جواب داده: chat_id -> timestamp
AI_GROUP_LAST_USED = {}
# حافظه‌ی کوتاه مکالمه‌ی هوش مصنوعی هر کاربر (فقط در حافظه، ری‌استارت پاکش می‌کنه)
AI_HISTORY = {}


def new_session_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


# ============================================================================
# ابزارهای کمکی
# ============================================================================


async def safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """
    مثل update.message.reply_text ولی اگه پیام اصلی پاک شده باشه (مثلاً یه ربات
    پاک‌کننده‌ی دیگه یا خود کاربر پیامش رو حذف کرده باشه) و خطای
    «Message to be replied not found» بگیریم، به‌جاش پیام رو معمولی
    (بدون ریپلای) توی همون چت می‌فرستیم، تا کارکرد ربات به‌خاطر یه پیام
    پاک‌شده متوقف نشه.
    """
    try:
        return await update.message.reply_text(text, **kwargs)
    except BadRequest as e:
        if "not found" in str(e).lower() or "message to be replied" in str(e).lower():
            logger.warning("safe_reply: پیام اصلی پیدا نشد، بدون ریپلای فرستاده شد.")
            return await context.bot.send_message(update.effective_chat.id, text, **kwargs)
        raise


async def is_owner(update: Update) -> bool:
    owner = get_owner_id()
    return owner is not None and update.effective_user and update.effective_user.id == owner


def in_linked_group(chat_id: int) -> bool:
    gid = get_group_id()
    return gid is not None and gid == chat_id


def admin_main_menu() -> InlineKeyboardMarkup:
    ai_state = "🟢 روشن" if is_ai_enabled() else "🔴 خاموش"
    rows = [
        [InlineKeyboardButton("📢 پیام همگانی به گروه", callback_data="adm:broadcast")],
        [InlineKeyboardButton("🔗 تنظیم لینک گروه", callback_data="adm:setlink")],
        [InlineKeyboardButton("🎮 مدیریت بازی‌ها", callback_data="adm:games")],
        [InlineKeyboardButton(f"🤖 هوش مصنوعی: {ai_state}", callback_data="adm:toggle_ai")],
        [InlineKeyboardButton("👥 کاربران داستان‌دار", callback_data="adm:stories")],
        [InlineKeyboardButton("✍️ افزودن پرامپت به هوش مصنوعی", callback_data="adm:addprompt")],
        [InlineKeyboardButton("📨 درخواست‌های کاربر اصلی شدن", callback_data="adm:requests")],
    ]
    return InlineKeyboardMarkup(rows)


def games_menu() -> InlineKeyboardMarkup:
    rows = []
    for key, meta in GAMES.items():
        state = "✅" if is_game_enabled(key) else "❌"
        rows.append([InlineKeyboardButton(f"{state} {meta['title']}", callback_data=f"adm:togglegame:{key}")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="adm:back")])
    return InlineKeyboardMarkup(rows)


def stories_admin_menu() -> InlineKeyboardMarkup:
    rows = []
    for uid, name, _ in list_stories():
        rows.append([InlineKeyboardButton(f"👤 {name}", callback_data=f"adm:storyview:{uid}")])
    rows.append([InlineKeyboardButton("➕ افزودن داستان برای کاربر جدید", callback_data="adm:storynew")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="adm:back")])
    return InlineKeyboardMarkup(rows)


# ============================================================================
# هوش مصنوعی (Groq — GPT-OSS-120B)
# ============================================================================


async def _call_groq_once(client: httpx.AsyncClient, payload: dict):
    """
    یک تلاش برای صدا زدن GPT-OSS-120B روی گراک.
    خروجی: (متن پاسخ یا None, کد وضعیت یا None برای خطای شبکه)
    """
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = await client.post(GROQ_API_BASE, json=payload, headers=headers)
    except Exception as e:
        logger.warning("Groq: خطای شبکه — %s", e)
        return None, None

    if resp.status_code != 200:
        logger.warning("Groq: خطای %s — %s", resp.status_code, resp.text[:200])
        return None, resp.status_code

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        logger.warning("Groq: پاسخ بدون choices")
        return None, resp.status_code

    message = choices[0].get("message", {}) or {}
    text = (message.get("content") or "").strip()
    # بعضی وقتا مدل‌های استدلالی زنجیره‌ی فکرشونو توی <think> برمی‌گردونن؛ حذفش می‌کنیم
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if not text:
        logger.warning("Groq: متن پاسخ خالی بود")
        return None, resp.status_code
    return text, resp.status_code


async def ask_groq(system_prompt: str, history: list, user_text: str):
    """
    صدا زدن GPT-OSS-120B روی گراک، با تحمل خطای موقتی:
    - وضعیت 503 یا خطای شبکه => یک بار با کمی تاخیر دوباره امتحان می‌شه (چون معمولاً موقتیه).
    - وضعیت 429 (rate limit) یا هر خطای دیگه => بلافاصله None برمی‌گرده.
    history: لیستی از dict به شکل {"role": "user"/"model", "text": "..."}
    خروجی: (پاسخ متنی یا None، نام مدلی که جواب داد یا None)
    """
    messages = [{"role": "system", "content": system_prompt}]
    for turn in history[-8:]:
        role = "assistant" if turn["role"] == "model" else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": AI_MAX_OUTPUT_TOKENS,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        text, status = await _call_groq_once(client, payload)
        if text:
            return text, GROQ_MODEL

        # وضعیت 503 یا خطای شبکه (status=None) معمولاً موقتیه؛ یک‌بار دیگه امتحان کن
        if status == 503 or status is None:
            await asyncio.sleep(2)
            text, status = await _call_groq_once(client, payload)
            if text:
                return text, GROQ_MODEL

    return None, None


# ============================================================================
# هندلرها: شروع، عضویت ربات در گروه، تغییر نام گروه
# ============================================================================


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    new_status = update.my_chat_member.new_chat_member.status
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if new_status in ("member", "administrator"):
        existing = get_group_id()
        if existing is None:
            set_setting("group_id", chat.id)
            set_setting("group_title", chat.title or "گروه")
            try:
                await context.bot.set_my_name(name=(chat.title or "ربات گروه")[:64])
            except Exception as e:
                logger.warning("set_my_name failed: %s", e)
            await context.bot.send_message(
                chat.id, "سلام! من فعال شدم و از این به بعد فقط توی همین گروه کار می‌کنم. 🎉\n/start رو بزنید تا شروع کنیم."
            )
        elif existing != chat.id:
            await context.bot.send_message(chat.id, "این ربات فقط برای یک گروه خاص تنظیم شده و نمی‌تونه اینجا فعال باشه. 🙏")
            try:
                await context.bot.leave_chat(chat.id)
            except Exception:
                pass


async def on_new_chat_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not in_linked_group(chat.id):
        return
    new_title = update.message.new_chat_title
    set_setting("group_title", new_title)
    try:
        await context.bot.set_my_name(name=new_title[:64])
    except Exception as e:
        logger.warning("set_my_name failed: %s", e)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    remember_user(user)

    if chat.type == ChatType.PRIVATE:
        gid = get_group_id()
        title = get_setting("group_title", "گروه")
        link = get_setting("group_link")
        if gid is None:
            await safe_reply(update, context, "سلام! هنوز به هیچ گروهی وصل نشدم. اول من رو به عنوان ادمین به گروهت اضافه کن. 🙌")
            return
        text = f"سلام {user.first_name} 👋\nاین ربات فقط توی گروه «{title}» فعالیته و اینجا توی پیوی کاری ازم برنمیاد."
        kb = None
        if link:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 ورود به گروه", url=link)]])
        await safe_reply(update, context, text, reply_markup=kb)
        return

    # داخل گروه
    if not in_linked_group(chat.id):
        return
    title = get_setting("group_title", chat.title or "این گروه")

    enabled_games = [meta for key, meta in GAMES.items() if is_game_enabled(key)]
    if enabled_games:
        games_lines = "\n".join(f"• «{g['trigger']}» — {g['title']}" for g in enabled_games)
        games_block = f"🎮 <b>بازی‌های فعال</b> (کافیه اسمشون رو دقیقاً همینجوری بفرستی):\n{games_lines}"
    else:
        games_block = "🎮 الان بازی فعالی نداریم."

    text = (
        f"سلام به همه! 👋 توی گروه «{title}» هستیم.\n\n"
        f"{games_block}\n\n"
        "🤖 <b>چت با هوش مصنوعی</b>: روی هر پیامی که خود من فرستادم ریپلای بزن و باهام حرف بزن.\n\n"
        "📖 <b>داستان کاربرا</b>: با دکمه‌ی زیر می‌تونی داستان کاربرای خاص گروه رو ببینی.\n"
        "⭐️ اگه دوست داری خودتم یه داستان اختصاصی داشته باشی، درخواست بده تا بررسی بشه."
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📖 لیست کاربران داستان‌دار", callback_data="grp:stories")],
            [InlineKeyboardButton("⭐️ درخواست کاربر اصلی شدن", callback_data="grp:reqmain")],
        ]
    )
    await safe_reply(update, context, text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ============================================================================
# روتر پیام‌های خصوصی (کد ادمین + مراحل چندمرحله‌ای)
# ============================================================================


async def private_passcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    owner = get_owner_id()
    if owner is None:
        set_setting("owner_id", user.id)
        await safe_reply(update, context, "شما به عنوان ادمین اصلی ربات ثبت شدید ✅")
        await safe_reply(update, context, "پنل ادمین 👇", reply_markup=admin_main_menu())
    elif owner == user.id:
        await safe_reply(update, context, "پنل ادمین 👇", reply_markup=admin_main_menu())
    # اگه شخص دیگه‌ای بفرسته، هیچ واکنشی نشون نمی‌دیم (برای امنیت)


async def private_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != get_owner_id():
        return  # فقط ادمین جریان‌های چندمرحله‌ای داره
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return
    text = update.message.text.strip()

    if awaiting == "broadcast":
        gid = get_group_id()
        if gid:
            await context.bot.send_message(gid, text)
            await safe_reply(update, context, "پیام همگانی ارسال شد ✅")
        else:
            await safe_reply(update, context, "هنوز گروهی وصل نشده.")
        context.user_data["awaiting"] = None

    elif awaiting == "setlink":
        set_setting("group_link", text)
        await safe_reply(update, context, "لینک گروه ذخیره شد ✅")
        context.user_data["awaiting"] = None

    elif awaiting == "addprompt":
        add_ai_prompt(text)
        await safe_reply(update, context, "این دستور برای همیشه به شخصیت هوش مصنوعی اضافه شد ✅")
        context.user_data["awaiting"] = None

    elif awaiting == "story_new_id":
        if not text.isdigit():
            await safe_reply(update, context, "آیدی عددی کاربر (User ID) رو بفرست.")
            return
        context.user_data["story_target_id"] = int(text)
        context.user_data["awaiting"] = "story_new_text"
        await safe_reply(update, context, "حالا متن داستان این کاربر رو بفرست:")

    elif awaiting == "story_new_text":
        uid = context.user_data.get("story_target_id")
        save_story(uid, f"کاربر {uid}", "", text)
        await safe_reply(update, context, "داستان ذخیره شد ✅", reply_markup=stories_admin_menu())
        context.user_data["awaiting"] = None
        context.user_data["story_target_id"] = None

    elif awaiting == "story_approve_text":
        uid = context.user_data.get("story_target_id")
        info = context.user_data.get("story_target_info", {})
        save_story(uid, info.get("full_name", f"کاربر {uid}"), info.get("username", ""), text)
        await safe_reply(update, context, "داستان ذخیره و کاربر به لیست اضافه شد ✅")
        context.user_data["awaiting"] = None
        context.user_data["story_target_id"] = None


# ============================================================================
# کال‌بک‌های پنل ادمین
# ============================================================================


async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if update.effective_user.id != get_owner_id():
        await query.answer("این بخش فقط برای ادمین ربات در دسترسه.", show_alert=True)
        return
    await query.answer()

    if data == "adm:back":
        await query.edit_message_text("پنل ادمین 👇", reply_markup=admin_main_menu())

    elif data == "adm:broadcast":
        context.user_data["awaiting"] = "broadcast"
        await query.edit_message_text("متن پیام همگانی رو بفرست تا توی گروه ارسال بشه:")

    elif data == "adm:setlink":
        context.user_data["awaiting"] = "setlink"
        await query.edit_message_text("لینک دعوت گروه رو بفرست:")

    elif data == "adm:games":
        await query.edit_message_text("مدیریت بازی‌ها 👇", reply_markup=games_menu())

    elif data.startswith("adm:togglegame:"):
        key = data.split(":")[2]
        toggle_game(key)
        await query.edit_message_text("مدیریت بازی‌ها 👇", reply_markup=games_menu())

    elif data == "adm:toggle_ai":
        set_setting("ai_enabled", "0" if is_ai_enabled() else "1")
        await query.edit_message_text("پنل ادمین 👇", reply_markup=admin_main_menu())

    elif data == "adm:stories":
        await query.edit_message_text("کاربران داستان‌دار 👇", reply_markup=stories_admin_menu())

    elif data.startswith("adm:storyview:"):
        uid = int(data.split(":")[2])
        story = get_story(uid) or "(داستانی ثبت نشده)"
        rows = [
            [InlineKeyboardButton("🗑 حذف این کاربر", callback_data=f"adm:storydel:{uid}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="adm:stories")],
        ]
        await query.edit_message_text(f"داستان کاربر {uid}:\n\n{story}", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("adm:storydel:"):
        uid = int(data.split(":")[2])
        delete_story(uid)
        await query.edit_message_text("کاربران داستان‌دار 👇", reply_markup=stories_admin_menu())

    elif data == "adm:storynew":
        context.user_data["awaiting"] = "story_new_id"
        await query.edit_message_text("آیدی عددی (User ID) کاربر مورد نظر رو بفرست:")

    elif data == "adm:addprompt":
        context.user_data["awaiting"] = "addprompt"
        await query.edit_message_text(
            "دستور یا شخصیتی که می‌خوای برای همیشه به هوش مصنوعی اضافه بشه رو بفرست.\n"
            "مثال: «تو اسمت جعفره»"
        )

    elif data == "adm:requests":
        reqs = list_story_requests()
        if not reqs:
            rows = [[InlineKeyboardButton("🔙 بازگشت", callback_data="adm:back")]]
            await query.edit_message_text("درخواستی در انتظار نیست.", reply_markup=InlineKeyboardMarkup(rows))
            return
        rows = []
        for uid, name, uname in reqs:
            rows.append([InlineKeyboardButton(f"👤 {name}", callback_data=f"adm:reqview:{uid}")])
        rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="adm:back")])
        await query.edit_message_text("درخواست‌های کاربر اصلی شدن 👇", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("adm:reqview:"):
        uid = int(data.split(":")[2])
        row = _conn.execute(
            "SELECT full_name, username FROM story_requests WHERE user_id = ?", (uid,)
        ).fetchone()
        name = row[0] if row else f"کاربر {uid}"
        uname = row[1] if row else ""
        rows = [
            [InlineKeyboardButton("✅ قبول و نوشتن داستان", callback_data=f"adm:reqapprove:{uid}")],
            [InlineKeyboardButton("❌ رد درخواست", callback_data=f"adm:reqreject:{uid}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="adm:requests")],
        ]
        await query.edit_message_text(
            f"درخواست از طرف: {name} (@{uname or '---'})\nآیدی: {uid}", reply_markup=InlineKeyboardMarkup(rows)
        )

    elif data.startswith("adm:reqapprove:"):
        uid = int(data.split(":")[2])
        row = _conn.execute(
            "SELECT full_name, username FROM story_requests WHERE user_id = ?", (uid,)
        ).fetchone()
        context.user_data["story_target_id"] = uid
        context.user_data["story_target_info"] = {
            "full_name": row[0] if row else f"کاربر {uid}",
            "username": row[1] if row else "",
        }
        context.user_data["awaiting"] = "story_approve_text"
        await query.edit_message_text("متن داستان این کاربر رو بنویس:")

    elif data.startswith("adm:reqreject:"):
        uid = int(data.split(":")[2])
        reject_story_request(uid)
        await query.edit_message_text("درخواست رد شد.", reply_markup=admin_main_menu())


# ============================================================================
# کال‌بک‌های داخل گروه (داستان‌ها / درخواست کاربر اصلی)
# ============================================================================


async def group_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat = update.effective_chat
    user = update.effective_user

    if not in_linked_group(chat.id):
        await query.answer()
        return

    if data == "grp:stories":
        stories = list_stories()
        if not stories:
            await query.answer("فعلاً کاربر داستان‌داری نداریم.", show_alert=True)
            return
        rows = [[InlineKeyboardButton(f"👤 {name}", callback_data=f"grp:storyview:{uid}")] for uid, name, _ in stories]
        rows.append([InlineKeyboardButton("🔙 بستن", callback_data="grp:close")])
        await query.answer()
        await context.bot.send_message(chat.id, "لیست کاربران داستان‌دار 👇", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("grp:storyview:"):
        uid = int(data.split(":")[2])
        story = get_story(uid) or "داستانی ثبت نشده."
        await query.answer()
        await context.bot.send_message(chat.id, story)

    elif data == "grp:close":
        await query.answer()

    elif data == "grp:reqmain":
        remember_user(user)
        if get_story(user.id) is not None:
            await query.answer("شما همین الان هم کاربر داستان‌دار هستید! ⭐️", show_alert=True)
            return
        added = add_story_request(user.id, user.full_name, user.username or "")
        if not added:
            await query.answer("درخواست شما قبلاً ثبت شده، صبر کن ادمین بررسی کنه.", show_alert=True)
            return
        await query.answer("درخواست شما ثبت شد ✅", show_alert=True)
        owner = get_owner_id()
        if owner:
            rows = [
                [InlineKeyboardButton("✅ قبول و نوشتن داستان", callback_data=f"adm:reqapprove:{user.id}")],
                [InlineKeyboardButton("❌ رد درخواست", callback_data=f"adm:reqreject:{user.id}")],
            ]
            try:
                await context.bot.send_message(
                    owner,
                    f"درخواست جدید برای کاربر اصلی شدن:\n👤 {user.full_name} (@{user.username or '---'})\nآیدی: {user.id}",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
            except Exception as e:
                logger.warning("could not notify owner: %s", e)


# ============================================================================
# بازی‌ها
# ============================================================================


async def game_trigger_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not in_linked_group(chat.id):
        return
    text = update.message.text.strip()
    key = TRIGGER_TO_GAME.get(text)
    if key is None:
        return
    if not is_game_enabled(key):
        await safe_reply(update, context, "این بازی الان توسط ادمین غیرفعال شده.")
        return

    user = update.effective_user
    remember_user(user)

    if key == "guess":
        await start_guess_game(update, context)
        return
    if key == "hangman":
        await start_hangman_game(update, context)
        return
    if key == "trivia":
        await start_trivia_game(update, context)
        return
    if key == "math":
        await start_math_game(update, context)
        return
    if key == "wyr":
        await start_wyr_game(update, context)
        return
    if key == "emoji":
        await start_emoji_game(update, context)
        return
    if key == "bj":
        await start_blackjack_game(update, context)
        return

    # بازی‌های دو نفره: ساخت چالش با دکمه‌ی «قبول چالش»
    session_id = new_session_id()
    PENDING_CHALLENGES[session_id] = {
        "game": key,
        "initiator": user.id,
        "initiator_name": user.full_name,
        "chat_id": chat.id,
    }
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚔️ قبول چالش", callback_data=f"join:{session_id}")]])
    await safe_reply(update, context, 
        f"🎮 {user.full_name} دنبال حریف برای «{GAMES[key]['title']}» می‌گرده!\nکی قبول می‌کنه؟",
        reply_markup=kb,
    )


async def game_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.split(":")[1]
    challenge = PENDING_CHALLENGES.get(session_id)
    if not challenge:
        await query.answer("این چالش دیگه معتبر نیست.", show_alert=True)
        return
    user = update.effective_user
    if user.id == challenge["initiator"]:
        await query.answer("نمی‌تونی با خودت بازی کنی! یکی دیگه باید قبول کنه.", show_alert=True)
        return

    del PENDING_CHALLENGES[session_id]
    remember_user(user)
    game_key = challenge["game"]

    if game_key == "tictactoe":
        ACTIVE_GAMES[session_id] = {
            "game": "tictactoe",
            "board": [""] * 9,
            "players": {challenge["initiator"]: "❌", user.id: "⭕️"},
            "names": {challenge["initiator"]: challenge["initiator_name"], user.id: user.full_name},
            "turn": challenge["initiator"],
        }
        await query.answer()
        await query.edit_message_text(
            f"دوز شروع شد! ❌ {challenge['initiator_name']} در برابر ⭕️ {user.full_name}\nنوبت: {challenge['initiator_name']} (❌)",
            reply_markup=render_ttt_board(session_id),
        )
    elif game_key == "rps":
        ACTIVE_GAMES[session_id] = {
            "game": "rps",
            "players": [challenge["initiator"], user.id],
            "names": {challenge["initiator"]: challenge["initiator_name"], user.id: user.full_name},
            "choices": {},
        }
        await query.answer()
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🪨 سنگ", callback_data=f"rps:{session_id}:سنگ"),
                    InlineKeyboardButton("📄 کاغذ", callback_data=f"rps:{session_id}:کاغذ"),
                    InlineKeyboardButton("✂️ قیچی", callback_data=f"rps:{session_id}:قیچی"),
                ]
            ]
        )
        await query.edit_message_text(
            f"سنگ‌کاغذقیچی شروع شد بین {challenge['initiator_name']} و {user.full_name}!\nهر دو نفر مخفیانه انتخاب کنید 👇",
            reply_markup=kb,
        )
    elif game_key == "dice":
        ACTIVE_GAMES[session_id] = {
            "game": "dice",
            "players": [challenge["initiator"], user.id],
            "names": {challenge["initiator"]: challenge["initiator_name"], user.id: user.full_name},
            "rolls": {},
        }
        await query.answer()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 رول کن", callback_data=f"dd:{session_id}:roll")]])
        await query.edit_message_text(
            f"تاس شانس شروع شد بین {challenge['initiator_name']} و {user.full_name}!\nهر دو نفر تاس بریزید، بیشترین عدد می‌بره 👇",
            reply_markup=kb,
        )
    elif game_key == "coin":
        p1, p2 = challenge["initiator"], user.id
        sides = {p1: "شیر", p2: "خط"}
        result = random.choice(["شیر", "خط"])
        winner_id = p1 if sides[p1] == result else p2
        winner_name = challenge["initiator_name"] if winner_id == p1 else user.full_name
        emoji = "🦁" if result == "شیر" else "🪙"
        await query.answer()
        await query.edit_message_text(
            f"شیر یا خط: {challenge['initiator_name']} = شیر 🦁 | {user.full_name} = خط 🪙\n\n"
            f"سکه چرخید... {emoji} {result} اومد!\n\n🏆 {winner_name} برنده شد!"
        )


# ---- دوز (Tic Tac Toe) ----


def check_ttt_winner(board):
    lines = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6),
    ]
    for a, b, c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


def render_ttt_board(session_id):
    game = ACTIVE_GAMES[session_id]
    board = game["board"]
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            label = board[i] if board[i] else "・"
            row.append(InlineKeyboardButton(label, callback_data=f"ttt:{session_id}:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def ttt_move_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, session_id, idx = query.data.split(":")
    idx = int(idx)
    game = ACTIVE_GAMES.get(session_id)
    if not game:
        await query.answer("این بازی تموم شده.", show_alert=True)
        return
    user = update.effective_user
    if user.id not in game["players"]:
        await query.answer("این بازی برای شما نیست.", show_alert=True)
        return
    if user.id != game["turn"]:
        await query.answer("نوبت شما نیست، صبر کن.", show_alert=True)
        return
    if game["board"][idx]:
        await query.answer("این خونه پره!", show_alert=True)
        return

    game["board"][idx] = game["players"][user.id]
    winner = check_ttt_winner(game["board"])
    await query.answer()

    if winner == "draw":
        await query.edit_message_text("🤝 بازی مساوی شد!", reply_markup=render_ttt_board(session_id))
        del ACTIVE_GAMES[session_id]
        return
    if winner:
        winner_name = game["names"][user.id]
        await query.edit_message_text(f"🏆 {winner_name} برنده شد!", reply_markup=render_ttt_board(session_id))
        del ACTIVE_GAMES[session_id]
        return

    other = [uid for uid in game["players"] if uid != user.id][0]
    game["turn"] = other
    turn_name = game["names"][other]
    turn_symbol = game["players"][other]
    await query.edit_message_text(f"نوبت: {turn_name} ({turn_symbol})", reply_markup=render_ttt_board(session_id))


# ---- سنگ کاغذ قیچی ----

RPS_BEATS = {"سنگ": "قیچی", "کاغذ": "سنگ", "قیچی": "کاغذ"}


async def rps_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, session_id, choice = query.data.split(":")
    game = ACTIVE_GAMES.get(session_id)
    if not game:
        await query.answer("این بازی تموم شده.", show_alert=True)
        return
    user = update.effective_user
    if user.id not in game["players"]:
        await query.answer("این بازی برای شما نیست.", show_alert=True)
        return
    if user.id in game["choices"]:
        await query.answer("انتخابت قبلاً ثبت شده، منتظر حریف باش.", show_alert=True)
        return

    game["choices"][user.id] = choice
    await query.answer("انتخابت ثبت شد ✅ منتظر حریف باش.")

    if len(game["choices"]) < 2:
        return

    p1, p2 = game["players"]
    c1, c2 = game["choices"][p1], game["choices"][p2]
    n1, n2 = game["names"][p1], game["names"][p2]
    if c1 == c2:
        result = "🤝 مساوی شد!"
    elif RPS_BEATS[c1] == c2:
        result = f"🏆 {n1} برنده شد!"
    else:
        result = f"🏆 {n2} برنده شد!"

    await query.edit_message_text(f"{n1} انتخاب کرد: {c1}\n{n2} انتخاب کرد: {c2}\n\n{result}")
    del ACTIVE_GAMES[session_id]


# ---- تاس شانس ----


async def dice_duel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, session_id, _ = query.data.split(":")
    game = ACTIVE_GAMES.get(session_id)
    if not game:
        await query.answer("این بازی تموم شده.", show_alert=True)
        return
    user = update.effective_user
    if user.id not in game["players"]:
        await query.answer("این بازی برای شما نیست.", show_alert=True)
        return
    if user.id in game["rolls"]:
        await query.answer("قبلاً رول کردی، منتظر حریف باش.", show_alert=True)
        return

    game["rolls"][user.id] = random.randint(1, 6)
    await query.answer(f"🎲 عدد تو: {game['rolls'][user.id]}")

    if len(game["rolls"]) < 2:
        return

    p1, p2 = game["players"]
    r1, r2 = game["rolls"][p1], game["rolls"][p2]
    n1, n2 = game["names"][p1], game["names"][p2]

    if r1 == r2:
        game["rolls"] = {}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 رول کن (تساوی، دوباره)", callback_data=f"dd:{session_id}:roll")]])
        await query.edit_message_text(f"{n1} 🎲 {r1}  |  {n2} 🎲 {r2}\n\n🤝 تساوی شد! دوباره رول کنید.", reply_markup=kb)
        return

    winner = n1 if r1 > r2 else n2
    await query.edit_message_text(f"{n1} 🎲 {r1}  |  {n2} 🎲 {r2}\n\n🏆 {winner} برنده شد!")
    del ACTIVE_GAMES[session_id]


# ---- حدس عدد (بدون محدودیت نفرات) ----


async def start_guess_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in GUESS_GAMES:
        await safe_reply(update, context, "یک بازی حدس عدد همین الان فعاله! یه عدد بین ۱ تا ۱۰۰ بفرست.")
        return
    GUESS_GAMES[chat_id] = {"number": random.randint(1, 100), "low": 1, "high": 100}
    await safe_reply(update, context, "🎲 بازی حدس عدد شروع شد! یه عدد بین ۱ تا ۱۰۰ حدس بزن (فقط با فرستادن عدد توی گروه).")


async def guess_number_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    یک هندلر واحد برای پیام‌های فقط‌رقمی توی گروه: هم حدس عدد رو مدیریت می‌کنه
    هم جواب‌های ریاضی سریع رو، چون هر دو روی همون فیلتر regex ^\\d+$ ثبت شدن.
    """
    chat = update.effective_chat
    if not in_linked_group(chat.id):
        return

    value = int(update.message.text.strip())
    user = update.effective_user

    math_game = MATH_GAMES.get(chat.id)
    if math_game is not None and is_game_enabled("math"):
        remember_user(user)
        if value == math_game["answer"]:
            await safe_reply(
                update, context,
                f"🎉 {user.full_name} درست جواب داد! {math_game['question']} = {math_game['answer']}",
            )
            del MATH_GAMES[chat.id]
        return

    game = GUESS_GAMES.get(chat.id)
    if not game:
        return
    if not is_game_enabled("guess"):
        return
    remember_user(user)

    if value == game["number"]:
        await safe_reply(update, context, f"🎉 {user.full_name} درست حدس زد! عدد {game['number']} بود.")
        del GUESS_GAMES[chat.id]
    elif value < game["number"]:
        await safe_reply(update, context, "⬆️ بزرگ‌تره!")
    else:
        await safe_reply(update, context, "⬇️ کوچیک‌تره!")


# ---- ریاضی سریع ----


async def start_math_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in MATH_GAMES:
        await safe_reply(update, context, f"یک سوال ریاضی همین الان فعاله: {MATH_GAMES[chat_id]['question']} = ?")
        return
    op = random.choice(["+", "-", "×"])
    if op == "+":
        a, b = random.randint(10, 90), random.randint(10, 90)
        answer = a + b
    elif op == "-":
        a, b = random.randint(20, 99), random.randint(1, 19)
        answer = a - b
    else:
        a, b = random.randint(2, 12), random.randint(2, 12)
        answer = a * b
    question = f"{a} {op} {b}"
    MATH_GAMES[chat_id] = {"question": question, "answer": answer}
    await safe_reply(update, context, f"🧮 ریاضی سریع! {question} = ?\n(فقط جواب رو به‌صورت عدد بفرست)")


# ---- حدس کلمه (دار) ----

HANGMAN_STAGES = ["🙂", "😐", "😟", "😨", "😰", "💀"]


def render_hangman_word(game) -> str:
    return " ".join(ch if ch in game["guessed"] else "▫️" for ch in game["word"])


async def start_hangman_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in HANGMAN_GAMES:
        game = HANGMAN_GAMES[chat_id]
        await safe_reply(
            update, context,
            f"یک بازی حدس کلمه همین الان فعاله ({game['category']}):\n{render_hangman_word(game)}\nحرف بعدی رو بفرست 👇",
        )
        return
    word, category = random.choice(HANGMAN_WORDS)
    HANGMAN_GAMES[chat_id] = {"word": word, "category": category, "guessed": set(), "wrong": 0, "wrong_letters": []}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏳️ لو دادن کلمه", callback_data="hg:reveal")]])
    await safe_reply(
        update, context,
        f"🔤 حدس کلمه شروع شد! (دسته: {category})\n{render_hangman_word(HANGMAN_GAMES[chat_id])}\n"
        "یک حرف فارسی بفرست تا حدس بزنی 👇",
        reply_markup=kb,
    )


async def hangman_letter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not in_linked_group(chat.id):
        return
    game = HANGMAN_GAMES.get(chat.id)
    if not game:
        return
    if not is_game_enabled("hangman"):
        return
    letter = update.message.text.strip()
    user = update.effective_user
    remember_user(user)

    if letter in game["guessed"] or letter in game["wrong_letters"]:
        await safe_reply(update, context, f"حرف «{letter}» رو قبلاً امتحان کردیم.")
        return

    if letter in game["word"]:
        game["guessed"].add(letter)
        if all(ch in game["guessed"] for ch in game["word"]):
            await safe_reply(update, context, f"🎉 {user.full_name} کلمه رو کامل کرد! کلمه «{game['word']}» بود.")
            del HANGMAN_GAMES[chat.id]
            return
        await safe_reply(update, context, f"✅ درست بود!\n{render_hangman_word(game)}")
    else:
        game["wrong"] += 1
        game["wrong_letters"].append(letter)
        if game["wrong"] >= len(HANGMAN_STAGES) - 1:
            await safe_reply(
                update, context,
                f"{HANGMAN_STAGES[-1]} باختید! کلمه «{game['word']}» بود.",
            )
            del HANGMAN_GAMES[chat.id]
            return
        stage = HANGMAN_STAGES[game["wrong"]]
        wrong_list = "، ".join(game["wrong_letters"])
        await safe_reply(
            update, context,
            f"{stage} غلط بود! (حروف غلط: {wrong_list})\n{render_hangman_word(game)}",
        )


async def hangman_reveal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    game = HANGMAN_GAMES.get(chat_id)
    if not game:
        await query.answer("بازی فعالی نیست.", show_alert=True)
        return
    await query.answer()
    await context.bot.send_message(chat_id, f"🏳️ کلمه لو رفت: «{game['word']}» (دسته: {game['category']})")
    del HANGMAN_GAMES[chat_id]


# ---- کوییز اطلاعات عمومی ----

TRIVIA_LETTERS = ["A", "B", "C", "D"]


def render_trivia_keyboard(session_id: str, options: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{TRIVIA_LETTERS[i]}. {opt}", callback_data=f"triv:{session_id}:{i}")]
        for i, opt in enumerate(options)
    ]
    return InlineKeyboardMarkup(rows)


async def start_trivia_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for sid, g in list(TRIVIA_GAMES.items()):
        if g["chat_id"] == chat_id:
            await safe_reply(update, context, "یک کوییز همین الان فعاله، اول اونو جواب بدید.")
            return

    q = random.choice(TRIVIA_QUESTIONS)
    session_id = new_session_id()
    TRIVIA_GAMES[session_id] = {"chat_id": chat_id, "question": q, "answered_by": None}
    await safe_reply(
        update, context,
        f"🧠 کوییز! (دسته: {q['cat']})\n{q['q']}",
        reply_markup=render_trivia_keyboard(session_id, q["options"]),
    )


async def trivia_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, session_id, idx = query.data.split(":")
    idx = int(idx)
    game = TRIVIA_GAMES.get(session_id)
    if not game:
        await query.answer("این کوییز تموم شده.", show_alert=True)
        return
    if game["answered_by"] is not None:
        await query.answer("یکی دیگه زودتر جواب داد!", show_alert=True)
        return

    user = update.effective_user
    remember_user(user)
    q = game["question"]

    if idx == q["answer"]:
        game["answered_by"] = user.id
        await query.answer("درست بود! 🎉")
        correct_option = q["options"][q["answer"]]
        await query.edit_message_text(
            f"🧠 {q['q']}\n\n✅ جواب درست: {correct_option}\n🏆 {user.full_name} اول جواب داد!"
        )
        del TRIVIA_GAMES[session_id]
    else:
        await query.answer("غلط بود، یکی دیگه امتحان کنه!", show_alert=True)


# ---- این یا اون ----


def render_wyr_keyboard(session_id: str, game: dict) -> InlineKeyboardMarkup:
    votes_a = sum(1 for v in game["votes"].values() if v == "a")
    votes_b = sum(1 for v in game["votes"].values() if v == "b")
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"1️⃣ {game['option_a']} ({votes_a})", callback_data=f"wyr:{session_id}:a")],
            [InlineKeyboardButton(f"2️⃣ {game['option_b']} ({votes_b})", callback_data=f"wyr:{session_id}:b")],
            [InlineKeyboardButton("🔒 پایان نظرسنجی", callback_data=f"wyr:{session_id}:close")],
        ]
    )


async def start_wyr_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    option_a, option_b = random.choice(WYR_QUESTIONS)
    session_id = new_session_id()
    WYR_GAMES[session_id] = {
        "option_a": option_a, "option_b": option_b,
        "votes": {}, "starter": update.effective_user.id,
    }
    await safe_reply(
        update, context,
        f"🤔 این یا اون؟\n\n1️⃣ {option_a}\n— یا —\n2️⃣ {option_b}\n\nرأی بده 👇",
        reply_markup=render_wyr_keyboard(session_id, WYR_GAMES[session_id]),
    )


async def wyr_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, session_id, choice = query.data.split(":")
    game = WYR_GAMES.get(session_id)
    if not game:
        await query.answer("این نظرسنجی بسته شده.", show_alert=True)
        return
    user = update.effective_user
    remember_user(user)

    if choice == "close":
        if user.id != game["starter"]:
            await query.answer("فقط کسی که این یا اون رو شروع کرده می‌تونه ببندش.", show_alert=True)
            return
        votes_a = sum(1 for v in game["votes"].values() if v == "a")
        votes_b = sum(1 for v in game["votes"].values() if v == "b")
        await query.answer()
        await query.edit_message_text(
            f"🤔 این یا اون؟\n\n1️⃣ {game['option_a']} — {votes_a} رأی\n2️⃣ {game['option_b']} — {votes_b} رأی\n\n"
            "✅ نظرسنجی بسته شد."
        )
        del WYR_GAMES[session_id]
        return

    game["votes"][user.id] = choice
    await query.answer("رأیت ثبت شد ✅")
    await query.edit_message_reply_markup(reply_markup=render_wyr_keyboard(session_id, game))


# ---- حدس اموجی ----


def _normalize_fa(text: str) -> str:
    return text.strip().replace("ي", "ی").replace("ك", "ک").replace("‌", " ").replace("  ", " ").lower()


async def start_emoji_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in EMOJI_GAMES:
        await safe_reply(update, context, f"یک حدس اموجی همین الان فعاله: {EMOJI_GAMES[chat_id]['emojis']}")
        return
    emojis, answer = random.choice(EMOJI_RIDDLES)
    EMOJI_GAMES[chat_id] = {"emojis": emojis, "answer": answer}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ رد شدن / لو دادن", callback_data="emj:skip")]])
    await safe_reply(update, context, f"🧩 حدس اموجی!\n\n{emojis}\n\nاسم فیلم/عبارت رو بنویس 👇", reply_markup=kb)


async def emoji_guess_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not in_linked_group(chat.id):
        return
    game = EMOJI_GAMES.get(chat.id)
    if not game:
        return
    if not is_game_enabled("emoji"):
        return
    guess = update.message.text
    if _normalize_fa(guess) != _normalize_fa(game["answer"]):
        return

    user = update.effective_user
    remember_user(user)
    await safe_reply(update, context, f"🎉 {user.full_name} درست حدس زد! جواب «{game['answer']}» بود.")
    del EMOJI_GAMES[chat.id]


async def emoji_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    game = EMOJI_GAMES.get(chat_id)
    if not game:
        await query.answer("بازی فعالی نیست.", show_alert=True)
        return
    await query.answer()
    await context.bot.send_message(chat_id, f"⏭️ رد شد! جواب «{game['answer']}» بود.")
    del EMOJI_GAMES[chat_id]


# ---- بلک‌جک ----

CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
CARD_SUITS = ["♠️", "♥️", "♦️", "♣️"]


def _new_deck():
    deck = [(r, s) for r in CARD_RANKS for s in CARD_SUITS]
    random.shuffle(deck)
    return deck


def _card_value(rank: str) -> int:
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def _hand_value(hand: list) -> int:
    total = sum(_card_value(r) for r, _ in hand)
    aces = sum(1 for r, _ in hand if r == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _fmt_hand(hand: list) -> str:
    return " ".join(f"{r}{s}" for r, s in hand)


def render_blackjack_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🃏 بگیر (Hit)", callback_data=f"bj:{session_id}:hit"),
            InlineKeyboardButton("✋ بمون (Stand)", callback_data=f"bj:{session_id}:stand"),
        ]]
    )


async def start_blackjack_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remember_user(user)
    deck = _new_deck()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]
    session_id = new_session_id()
    BLACKJACK_GAMES[session_id] = {
        "player_id": user.id, "player_name": user.full_name,
        "deck": deck, "player_hand": player_hand, "dealer_hand": dealer_hand,
    }

    if _hand_value(player_hand) == 21:
        await safe_reply(
            update, context,
            f"🃏 بلک‌جک برای {user.full_name}!\nدست تو: {_fmt_hand(player_hand)} (21) 🎉\nدست دیلر: {_fmt_hand(dealer_hand)}\n\n🏆 بردی!",
        )
        del BLACKJACK_GAMES[session_id]
        return

    await safe_reply(
        update, context,
        f"🃏 بلک‌جک شروع شد، {user.full_name}!\n"
        f"دست تو: {_fmt_hand(player_hand)} ({_hand_value(player_hand)})\n"
        f"دست دیلر: {dealer_hand[0][0]}{dealer_hand[0][1]} 🂠",
        reply_markup=render_blackjack_keyboard(session_id),
    )


async def blackjack_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, session_id, action = query.data.split(":")
    game = BLACKJACK_GAMES.get(session_id)
    if not game:
        await query.answer("این بازی تموم شده.", show_alert=True)
        return
    user = update.effective_user
    if user.id != game["player_id"]:
        await query.answer("این بازی برای شما نیست.", show_alert=True)
        return

    await query.answer()

    if action == "hit":
        game["player_hand"].append(game["deck"].pop())
        value = _hand_value(game["player_hand"])
        if value > 21:
            await query.edit_message_text(
                f"🃏 دست تو: {_fmt_hand(game['player_hand'])} ({value})\n\n💥 باختی! (بیشتر از ۲۱ شد)"
            )
            del BLACKJACK_GAMES[session_id]
            return
        if value == 21:
            action = "stand"  # ۲۱ شد، خودکار می‌ره سراغ دیلر
        else:
            await query.edit_message_text(
                f"🃏 دست تو: {_fmt_hand(game['player_hand'])} ({value})\n"
                f"دست دیلر: {game['dealer_hand'][0][0]}{game['dealer_hand'][0][1]} 🂠",
                reply_markup=render_blackjack_keyboard(session_id),
            )
            return

    # action == "stand": نوبت دیلر
    dealer_hand = game["dealer_hand"]
    while _hand_value(dealer_hand) < 17:
        dealer_hand.append(game["deck"].pop())

    player_value = _hand_value(game["player_hand"])
    dealer_value = _hand_value(dealer_hand)

    if dealer_value > 21 or player_value > dealer_value:
        outcome = f"🏆 {game['player_name']} برد!"
    elif player_value == dealer_value:
        outcome = "🤝 مساوی شد!"
    else:
        outcome = f"💀 دیلر برد."

    await query.edit_message_text(
        f"🃏 دست تو: {_fmt_hand(game['player_hand'])} ({player_value})\n"
        f"دست دیلر: {_fmt_hand(dealer_hand)} ({dealer_value})\n\n{outcome}"
    )
    del BLACKJACK_GAMES[session_id]


# ============================================================================
# چت هوش مصنوعی (پاسخ به ریپلای روی پیام‌های ربات)
# ============================================================================


async def ai_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not in_linked_group(chat.id):
        return
    if not is_ai_enabled():
        return
    replied = update.message.reply_to_message
    if not replied or not replied.from_user or not replied.from_user.is_bot:
        return
    if replied.from_user.id != context.bot.id:
        return

    user = update.effective_user
    remember_user(user)
    now = time.time()

    # کول‌داون کل گروه: جلوی شلیک همزمان چند نفر مختلف رو می‌گیره تا هزینه‌ی API منفجر نشه
    group_last = AI_GROUP_LAST_USED.get(chat.id, 0)
    if now - group_last < AI_GROUP_COOLDOWN_SECONDS:
        return  # سکوت می‌کنیم، پیام اضافه نمی‌فرستیم تا خودش هم اسپم نشه

    last = AI_LAST_USED.get(user.id, 0)
    if now - last < AI_RATE_LIMIT_SECONDS:
        wait = int(AI_RATE_LIMIT_SECONDS - (now - last))
        await safe_reply(update, context, f"⏳ لطفاً {wait} ثانیه صبر کن و دوباره امتحان کن.")
        return
    AI_LAST_USED[user.id] = now
    AI_GROUP_LAST_USED[chat.id] = now

    system_prompt = get_ai_system_prompt()
    story = get_story(user.id)
    if story:
        system_prompt += (
            f"\n\nاین کاربر که داری باهاش صحبت می‌کنی داستان زیر رو داره؛ اول اون رو در نظر بگیر و "
            f"طبق شخصیت و داستانش باهاش هم‌صحبت شو:\n{story}"
        )

    history = AI_HISTORY.setdefault(user.id, [])
    user_text = update.message.text

    await context.bot.send_chat_action(chat.id, "typing")
    answer, used_model = await ask_groq(system_prompt, history, user_text)

    if answer is None:
        await safe_reply(update, context, "الان نتونستم به هوش مصنوعی وصل بشم، یکم بعد دوباره امتحان کن. 🙏")
        return

    history.append({"role": "user", "text": user_text})
    history.append({"role": "model", "text": answer})
    AI_HISTORY[user.id] = history[-16:]

    await safe_reply(update, context, answer)


# ============================================================================
# راه‌اندازی
# ============================================================================


def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit("توکن ربات (BOT_TOKEN) درست تنظیم نشده.")

    app = Application.builder().token(BOT_TOKEN).build()

    # شروع
    app.add_handler(CommandHandler("start", cmd_start))

    # پیوی: کد ادمین 1212
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.Regex(rf"^{ADMIN_PASSCODE}$"), private_passcode)
    )
    # پیوی: مراحل چندمرحله‌ای پنل ادمین
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_text_router)
    )

    # عضویت ربات در گروه / تغییر نام گروه
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_TITLE, on_new_chat_title))

    # تریگر بازی‌ها (نام دقیق بازی به‌عنوان متن پیام)
    trigger_pattern = "^(" + "|".join(re.escape(t) for t in TRIGGER_TO_GAME) + ")$"
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.Regex(trigger_pattern), game_trigger_handler)
    )
    # حدس عدد + ریاضی سریع: پیام‌های فقط‌رقمی (هر دو روی یک فیلتر، تفکیک داخل هندلر)
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.Regex(r"^\d+$"), guess_number_handler)
    )
    # حدس کلمه (دار): پیام‌های تک‌حرفی فارسی
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.Regex(r"^[آ-یءئؤأآ]$"), hangman_letter_handler)
    )
    # حدس اموجی: هر پیام متنی معمولی (غیر از ریپلای/دستور) - اگه بازی فعال نباشه بی‌اثره
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND & ~filters.REPLY, emoji_guess_handler
        )
    )
    # هوش مصنوعی: ریپلای روی پیام ربات
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.REPLY & filters.TEXT & ~filters.COMMAND, ai_reply_handler)
    )

    # کال‌بک‌ها
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(group_callbacks, pattern=r"^grp:"))
    app.add_handler(CallbackQueryHandler(game_join_callback, pattern=r"^join:"))
    app.add_handler(CallbackQueryHandler(ttt_move_callback, pattern=r"^ttt:"))
    app.add_handler(CallbackQueryHandler(rps_choice_callback, pattern=r"^rps:"))
    app.add_handler(CallbackQueryHandler(dice_duel_callback, pattern=r"^dd:"))
    app.add_handler(CallbackQueryHandler(hangman_reveal_callback, pattern=r"^hg:"))
    app.add_handler(CallbackQueryHandler(trivia_answer_callback, pattern=r"^triv:"))
    app.add_handler(CallbackQueryHandler(wyr_vote_callback, pattern=r"^wyr:"))
    app.add_handler(CallbackQueryHandler(emoji_skip_callback, pattern=r"^emj:"))
    app.add_handler(CallbackQueryHandler(blackjack_action_callback, pattern=r"^bj:"))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
