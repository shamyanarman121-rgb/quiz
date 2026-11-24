import asyncio
import json
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import gspread
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
POST_TIME = os.getenv("POST_TIME", "12:00")
ANSWERED_FILE = "answered.json"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN-ը բացակայում է .env ֆայլում")

TZ = ZoneInfo("Asia/Yerevan")

# ---------- Հարցեր ----------
QUESTIONS = [
    {"id": 1, "date": "2026-11-23", "points": 10,
     "q": "Ո՞րն է Հայաստանի մայրաքաղաքը։",
     "options": ["Գյումրի", "Երևան", "Վանաձոր"], "answer": 1},
    {"id": 2, "date": "2026-11-24", "points": 20,
     "q": "Քանի՞ տառ ունի հայկական այբուբենը։",
     "options": ["36", "38", "39"], "answer": 1},
    {"id": 3, "date": "2026-11-25/", "points": 30,
     "q": "Ո՞ր լիճն է Հայաստանի ամենամեծը։",
     "options": ["Արփի", "Սևան", "Փարվանա"], "answer": 1},
]
QUESTIONS_BY_ID = {q["id"]: q for q in QUESTIONS}
LEVEL = {10: "🟢 Հեշտ", 20: "🟡 Միջին", 30: "🔴 Դժվար"}

# ---------- Google Sheets (միայն Answers) ----------
HEADERS = ["telegram_id", "gaming_id", "points", "reached_at"]

gc = gspread.service_account(filename=CREDENTIALS_FILE)
spreadsheet = gc.open_by_key(SHEET_ID)

try:
    answers_ws = spreadsheet.worksheet("Answers")
except gspread.WorksheetNotFound:
    answers_ws = spreadsheet.add_worksheet(title="Answers", rows=1000, cols=5)

if answers_ws.row_values(1) != HEADERS:
    answers_ws.update(values=[HEADERS], range_name="A1")

# ---------- Հիշողության քեշ ----------
USERS = {}        # telegram_id -> {"gaming_id": str, "points": int}
ROWS = {}         # telegram_id -> տողի համարը Answers թերթում
GID_OWNER = {}    # gaming_id (փոքրատառ) -> telegram_id
ANSWERED = set()  # (telegram_id, question_id)
SHEET_LOCK = asyncio.Lock()


def norm(gid: str) -> str:
    return gid.strip().lower()


def today_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def load_cache():
    for i, r in enumerate(answers_ws.get_all_values()[1:], start=2):
        if not r or not r[0]:
            continue
        uid = int(r[0])
        if uid in ROWS:
            print(f"⚠️ Answers թերթում կրկնվող telegram_id {uid} (տող {i}), ջնջեք այն")
            continue
        gid = r[1] if len(r) > 1 else ""
        pts = int(r[2]) if len(r) > 2 and r[2] else 0
        reached = r[3] if len(r) > 3 else ""
        USERS[uid] = {"gaming_id": gid, "points": pts, "reached_at": reached}
        ROWS[uid] = i
        GID_OWNER.setdefault(norm(gid), uid)

    if os.path.exists(ANSWERED_FILE):
        try:
            with open(ANSWERED_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                for uid, qid in json.loads(content):
                    ANSWERED.add((int(uid), int(qid)))
        except Exception as e:
            print(f"answered.json կարդալու սխալ՝ {e}")


def save_answered():
    tmp = ANSWERED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(ANSWERED), f)
    os.replace(tmp, ANSWERED_FILE)


def sheet_register(tg_id, gaming_id):
    if tg_id in ROWS:
        answers_ws.update_cell(ROWS[tg_id], 2, gaming_id)
        return
    answers_ws.append_row([tg_id, gaming_id, 0, ""],
                          value_input_option="USER_ENTERED", table_range="A1")
    cell = answers_ws.find(str(tg_id), in_column=1)
    ROWS[tg_id] = cell.row


def sheet_update_points(tg_id, gaming_id, points, reached_at):
    if tg_id not in ROWS:
        sheet_register(tg_id, gaming_id)
    row = ROWS[tg_id]
    answers_ws.update(values=[[points, reached_at]], range_name=f"C{row}:D{row}")


# ---------- Հարցերի տրամաբանություն ----------
def is_open(q) -> bool:
    """Հարցը հասանելի է միայն իր օրը։ Եթե ուզում եք՝ հասանելի մնա նաև հետո,
    «==»-ը փոխեք «<=»-ով։"""
    return q["date"] == today_str()


def today_question():
    return next((q for q in QUESTIONS if q["date"] == today_str()), None)


async def send_question(bot, user_id, qid):
    q = QUESTIONS_BY_ID.get(qid)
    if not q:
        await bot.send_message(user_id, "Այդ հարցը չի գտնվել։")
        return
    if not is_open(q):
        await bot.send_message(user_id, "⏰ Այս հարցը այլևս (կամ դեռ) հասանելի չէ։")
        return
    if (user_id, qid) in ANSWERED:
        await bot.send_message(user_id, "⚠️ Դուք արդեն պատասխանել եք այս հարցին։")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(opt, callback_data=f"a:{qid}:{i}")]
        for i, opt in enumerate(q["options"])
    ])
    await bot.send_message(
        user_id,
        f"❓ Հարց #{qid}\n{LEVEL.get(q['points'], '')} · {q['points']} միավոր\n\n{q['q']}",
        reply_markup=kb,
    )


# ---------- Օգտատիրոջ հրամաններ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    qid = None
    if context.args:
        p = context.args[0]
        if p.startswith("q") and p[1:].isdigit():
            qid = int(p[1:])

    if user.id not in USERS:
        context.user_data["pending_q"] = qid
        context.user_data["awaiting_gid"] = True
        await update.message.reply_text("👋 Բարի գալուստ։\nՆախ գրեք ձեր gaming ID-ն՝")
        return

    if qid is None:
        q = today_question()
        qid = q["id"] if q else None
    if qid is None:
        await update.message.reply_text("Այսօր հարց չկա։ Հետևեք ալիքին։ 📢")
        return
    await send_question(context.bot, user.id, qid)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id in USERS:
        context.user_data["awaiting_gid"] = False
        return
    if not context.user_data.get("awaiting_gid"):
        await update.message.reply_text("Սեղմեք /start՝ սկսելու համար։")
        return

    gid = (update.message.text or "").strip()
    if not (2 <= len(gid) <= 50):
        await update.message.reply_text("Gaming ID-ն պետք է լինի 2–50 նիշ։ Փորձեք նորից՝")
        return

    owner = GID_OWNER.get(norm(gid))
    if owner is not None and owner != user.id:
        await update.message.reply_text(
            "⚠️ Այս gaming ID-ն արդեն գրանցված է մեկ այլ հաշվով։\n"
            "Եթե սա ձեր ID-ն է, գրեք ճիշտ ID-ն, կամ դիմեք ադմինին։"
        )
        return

    USERS[user.id] = {"gaming_id": gid, "points": 0, "reached_at": ""}
    GID_OWNER[norm(gid)] = user.id
    context.user_data["awaiting_gid"] = False
    async with SHEET_LOCK:
        await asyncio.to_thread(sheet_register, user.id, gid)

    await update.message.reply_text(f"✅ Gaming ID-ն պահպանվեց՝ {gid}")

    qid = context.user_data.pop("pending_q", None)
    if qid is None:
        q = today_question()
        qid = q["id"] if q else None
    if qid is not None:
        await send_question(context.bot, user.id, qid)


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = USERS.get(update.effective_user.id)
    if not u:
        await update.message.reply_text("Սեղմեք /start՝ գրանցվելու համար։")
        return
    await update.message.reply_text(f"🎮 {u['gaming_id']}\nՁեր միավորները՝ {u['points']} ⭐")


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    _, qid, opt = query.data.split(":")
    qid, opt = int(qid), int(opt)
    q = QUESTIONS_BY_ID.get(qid)
    if not q or user.id not in USERS:
        return

    if (user.id, qid) in ANSWERED:
        await query.edit_message_text(f"{q['q']}\n\n⚠️ Դուք արդեն պատասխանել եք այս հարցին։")
        return
    if not is_open(q):
        await query.edit_message_text(f"{q['q']}\n\n⏰ Այս հարցի ժամանակը լրացել է։")
        return

    # նշում ենք անմիջապես, որ կրկնակի սեղմումը չանցնի
    ANSWERED.add((user.id, qid))
    save_answered()

    correct = opt == q["answer"]
    gained = q["points"] if correct else 0
    USERS[user.id]["points"] += gained
    total = USERS[user.id]["points"]

    if gained:
        USERS[user.id]["reached_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        try:
            async with SHEET_LOCK:
                await asyncio.to_thread(
                    sheet_update_points, user.id, USERS[user.id]["gaming_id"],
                    total, USERS[user.id]["reached_at"],
                )
        except Exception as e:
            print(f"Sheets գրանցման սխալ ({user.id}, q{qid}): {e}")

    if correct:
        result = f"✅ Ճիշտ է։ +{gained} միավոր\nԸնդամենը՝ {total} ⭐"
    else:
        result = f"❌ Սխալ է։ Ճիշտ պատասխանը՝ {q['options'][q['answer']]}\nԸնդամենը՝ {total} ⭐"
    await query.edit_message_text(f"{q['q']}\n\n{result}")


# ---------- Ալիքում հրապարակում ----------
async def post_question_to_channel(bot, q):
    url = f"https://t.me/{bot.username}?start=q{q['id']}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎯 Պատասխանել հարցին", url=url)]])
    await bot.send_message(
        CHANNEL_ID,
        f"🔥 Նոր հարցը պատրաստ է։ Արի՛։\n\n"
        f"{LEVEL.get(q['points'], '')} · {q['points']} միավոր",
        reply_markup=kb,
    )


async def daily_post(context: ContextTypes.DEFAULT_TYPE):
    q = today_question()
    if q:
        await post_question_to_channel(context.bot, q)


async def publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/publish 2 → ձեռքով հրապարակում է ալիքում հարց #2-ը։"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Օգտագործում՝ /publish <հարցի_id>")
        return
    q = QUESTIONS_BY_ID.get(int(context.args[0]))
    if not q:
        await update.message.reply_text("Հարցը չի գտնվել։")
        return
    await post_question_to_channel(context.bot, q)
    await update.message.reply_text("✅ Հրապարակվեց ալիքում։")


# ---------- Արդյունքներ ----------
def build_leaderboard():
    """Նախ՝ միավոր։ Հավասարության դեպքում՝ ով ավելի շուտ է հասել այդ միավորին։"""
    ranked = sorted(
        USERS.items(),
        key=lambda kv: (-kv[1]["points"],
                        kv[1].get("reached_at") or "9999",
                        ROWS.get(kv[0], 10**9)),
    )
    return [u for _, u in ranked if u["points"] > 0]


def format_results(ranked, me_gid=None) -> str:
    """me_gid՝ տվյալ օգտատիրոջ gaming ID-ն (փոքրատառ), նրա տողը կերևա «Ես»։"""
    if not ranked:
        return "Դեռ արդյունքներ չկան։"

    def name(u):
        if me_gid and norm(u["gaming_id"]) == me_gid:
            return "👉 Ես"
        return u["gaming_id"]

    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 Վիկտորինայի արդյունքները\n\n🔥 Տոպ 3\n"
    for i, u in enumerate(ranked[:3]):
        text += f"{medals[i]} {name(u)} — {u['points']} ⭐\n"
    rest = ranked[3:10]
    if rest:
        text += "\n⭐ Տոպ 4–10\n"
        for i, u in enumerate(rest, start=4):
            text += f"{i}. {name(u)} — {u['points']} ⭐\n"

    # եթե օգտատերը տոպ 10-ից դուրս է, ավելացնում ենք իր տողը
    if me_gid:
        place = next((i for i, u in enumerate(ranked, start=1)
                      if norm(u["gaming_id"]) == me_gid), None)
        if place and place > 10:
            text += f"\n...\n{place}. 👉 Ես — {ranked[place - 1]['points']} ⭐\n"
    return text


async def results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/results → ընթացիկ տոպը, հասանելի է բոլորին։ Օգտատերը իրեն տեսնում է «Ես»։"""
    ranked = build_leaderboard()
    me = USERS.get(update.effective_user.id)
    me_gid = norm(me["gaming_id"]) if me else None

    text = format_results(ranked, me_gid)

    if me and me["points"] == 0:
        text += "\nԴուք դեռ միավոր չունեք։ 🎯"
    await update.message.reply_text(text)


async def publish_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ադմին՝ /publish_results → Տոպ 3 և Տոպ 4–10՝ ալիքում։"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    ranked = build_leaderboard()
    await context.bot.send_message(CHANNEL_ID, format_results(ranked))
    await update.message.reply_text("✅ Արդյունքները հրապարակվեցին ալիքում։")


def main():
    load_cache()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("publish", publish))
    app.add_handler(CommandHandler("results", results))
    app.add_handler(CommandHandler("publish_results", publish_results))
    app.add_handler(CallbackQueryHandler(answer, pattern=r"^a:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    h, m = map(int, POST_TIME.split(":"))
    app.job_queue.run_daily(daily_post, time=dtime(h, m, tzinfo=TZ))
    print("Օրական գրառումը կատարվելու է՝", POST_TIME, "(Երևանի ժամանակով)")

    app.run_polling()


if __name__ == "__main__":
    main()