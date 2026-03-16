import os
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
    ConversationHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import re
import base64
import httpx

TOKEN        = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ALERT_THRESHOLD = 0.8  # 80% budget alert
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

CATS = [
    "Housing/Rent", "Groceries", "Dining Out", "Subscriptions",
    "Transportation", "Utilities", "Entertainment", "Savings", "Personal/Shopping"
]
CAT_ICONS = {
    "Housing/Rent":"🏠","Groceries":"🛒","Dining Out":"🍽",
    "Subscriptions":"📱","Transportation":"🚗","Utilities":"⚡",
    "Entertainment":"🎬","Savings":"💰","Personal/Shopping":"🛍","Income":"💵"
}
CAT_KEYWORDS = {
    "Groceries": ["grocery","groceries","supermarket","market","fairprice","ntuc","cold storage","giant","trader","walmart","costco","food","lunch","dinner","breakfast","meal","supper"],
    "Dining Out": ["dining","restaurant","cafe","coffee","starbucks","mcdonald","burger","pizza","sushi","hawker","kopitiam","grab food","deliveroo","foodpanda","nobu","brunch"],
    "Transportation": ["grab","taxi","uber","mrt","bus","transport","petrol","gas","parking","ez-link","train","gojek","car"],
    "Subscriptions": ["netflix","spotify","apple","google","subscription","sub","amazon prime","youtube","disney","hulu"],
    "Entertainment": ["movie","cinema","concert","game","shopping","mall","entertainment"],
    "Utilities": ["electric","water","internet","phone","bill","utility","telco","singtel","starhub","m1"],
    "Housing/Rent": ["rent","mortgage","condo","hdb","housing","property"],
    "Savings": ["savings","save","investment","invest","cpf"],
    "Personal/Shopping": ["shopping","clothes","shoes","haircut","gym","beauty","personal","lazada","shopee","amazon"],
}

# ── DATABASE ──────────────────────────────────────────────────────────────────
def get_conn():
    if DATABASE_URL:
        import psycopg2
        url = DATABASE_URL.replace("postgres://","postgresql://",1)
        return psycopg2.connect(url), "pg"
    return sqlite3.connect("budget.db"), "sqlite"

def ph(db_type):
    return "%s" if db_type == "pg" else "?"

def init_db():
    conn, db_type = get_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id BIGINT PRIMARY KEY, chat_id TEXT, type TEXT, amount REAL,
        description TEXT, category TEXT, date TEXT, added_by TEXT, month TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS budgets (
        chat_id TEXT, category TEXT, amount REAL, PRIMARY KEY (chat_id, category))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS recurring (
        id BIGINT PRIMARY KEY, chat_id TEXT, amount REAL,
        description TEXT, category TEXT, day_of_month INTEGER)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS settings (
        chat_id TEXT PRIMARY KEY, weekly_digest BOOLEAN DEFAULT TRUE,
        alert_threshold REAL DEFAULT 0.8)""")
    conn.commit(); conn.close()
    print("Database initialized OK.")

def get_txs(chat_id, month=None):
    month = month or get_month()
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(f"SELECT id,chat_id,type,amount,description,category,date,added_by,month FROM transactions WHERE chat_id={p} AND month={p} ORDER BY date DESC, id DESC", (chat_id, month))
    rows = cur.fetchall(); conn.close(); return rows

def add_tx(chat_id, type_, amount, desc, cat, date, added_by):
    month = date[:7]
    tx_id = int(datetime.now().timestamp() * 1000)
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(f"INSERT INTO transactions (id,chat_id,type,amount,description,category,date,added_by,month) VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})", (tx_id,chat_id,type_,amount,desc,cat,date,added_by,month))
    conn.commit(); conn.close()
    return tx_id

def del_tx(tx_id, chat_id):
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(f"DELETE FROM transactions WHERE id={p} AND chat_id={p}", (tx_id, chat_id))
    conn.commit(); conn.close()

def get_budgets(chat_id):
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(f"SELECT category, amount FROM budgets WHERE chat_id={p}", (chat_id,))
    rows = cur.fetchall(); conn.close(); return {r[0]: r[1] for r in rows}

def set_budget(chat_id, cat, amount):
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    if db_type == "pg":
        cur.execute(f"INSERT INTO budgets (chat_id,category,amount) VALUES ({p},{p},{p}) ON CONFLICT (chat_id,category) DO UPDATE SET amount={p}", (chat_id,cat,amount,amount))
    else:
        cur.execute(f"INSERT OR REPLACE INTO budgets (chat_id,category,amount) VALUES ({p},{p},{p})", (chat_id,cat,amount))
    conn.commit(); conn.close()

def get_recurring(chat_id):
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(f"SELECT id,chat_id,amount,description,category,day_of_month FROM recurring WHERE chat_id={p}", (chat_id,))
    rows = cur.fetchall(); conn.close(); return rows

def add_recurring(chat_id, amount, desc, cat, day):
    r_id = int(datetime.now().timestamp() * 1000)
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(f"INSERT INTO recurring (id,chat_id,amount,description,category,day_of_month) VALUES ({p},{p},{p},{p},{p},{p})", (r_id,chat_id,amount,desc,cat,day))
    conn.commit(); conn.close()

def del_recurring(r_id, chat_id):
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(f"DELETE FROM recurring WHERE id={p} AND chat_id={p}", (r_id, chat_id))
    conn.commit(); conn.close()

def get_all_chats():
    conn, db_type = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT chat_id FROM transactions")
    rows = cur.fetchall(); conn.close()
    return [r[0] for r in rows]

# ── HELPERS ───────────────────────────────────────────────────────────────────
def fmt(n): return f"${n:,.2f}"
def fmt_short(n): return f"${n:,.0f}" if n == int(n) else f"${n:,.2f}"
def get_month(): return datetime.now().strftime("%Y-%m")
def month_label(m=None):
    m = m or get_month()
    return datetime.strptime(m, "%Y-%m").strftime("%B %Y")

def calc_summary(chat_id, month=None):
    txs = get_txs(chat_id, month)
    income = sum(t[3] for t in txs if t[2]=="income")
    spent  = sum(t[3] for t in txs if t[2]=="expense")
    by_cat = {}
    for t in txs:
        if t[2]=="expense": by_cat[t[5]] = by_cat.get(t[5],0) + t[3]
    return income, spent, by_cat

def progress_bar(pct, width=10):
    filled = max(0, min(round(pct/100*width), width))
    return "█"*filled + "░"*(width-filled)

def budget_status(pct):
    if pct >= 100: return "🔴"
    if pct >= 80:  return "🟡"
    return "🟢"

def guess_category(text):
    """Guess category from description keywords."""
    text_lower = text.lower()
    for cat, keywords in CAT_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return cat
    return None

def get_grade(pct_used):
    if pct_used <= 0.7:  return "A", "🌟 Excellent!"
    if pct_used <= 0.85: return "B", "👍 Good job!"
    if pct_used <= 0.95: return "C", "😐 Watch your spending"
    if pct_used <= 1.0:  return "D", "⚠️ Almost over budget!"
    return "F", "🔴 Over budget!"

# ── REPLY KEYBOARD ───────────────────────────────────────────────────────────
def main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Summary"), KeyboardButton("📋 Spent")],
        [KeyboardButton("🏆 Report"), KeyboardButton("🧾 Recent")],
        [KeyboardButton("💰 Budgets"), KeyboardButton("🗑 Delete")],
        [KeyboardButton("🔁 Recurring"), KeyboardButton("❓ Help")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, persistent=True)

# ── RECEIPT SCANNER ──────────────────────────────────────────────────────────
async def scan_receipt_with_claude(image_bytes: bytes) -> dict:
    """Send receipt image to Claude API and extract transaction details."""
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not configured"}

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    # Detect media type from magic bytes
    if image_bytes[:4] == b'\x89PNG':
        media_type = "image/png"
    elif image_bytes[:2] == b'\xff\xd8':
        media_type = "image/jpeg"
    elif image_bytes[:4] == b'RIFF':
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"  # default
    print(f"Detected media type: {media_type}, size: {len(image_bytes)}")

    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": """Analyze this receipt image and extract the following information.
Reply ONLY with a JSON object, no other text:
{
  "merchant": "store/restaurant name",
  "total": 12.34,
  "date": "YYYY-MM-DD or null if not visible",
  "category": "one of: Groceries, Dining Out, Transportation, Entertainment, Subscriptions, Utilities, Housing/Rent, Savings, Personal/Shopping",
  "items": ["item1 $x.xx", "item2 $x.xx"],
  "confidence": "high/medium/low"
}

If you cannot read the receipt clearly, return:
{"error": "Cannot read receipt clearly"}"""
                    }
                ],
            }
        ],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        )
        if response.status_code != 200:
            print(f"Anthropic API error {response.status_code}: {response.text}")
            return {"error": f"API error {response.status_code}: {response.text[:200]}"}
        data = response.json()
        text = data["content"][0]["text"].strip()
        # Clean up any markdown code blocks
        text = text.replace("```json", "").replace("```", "").strip()
        import json
        return json.loads(text)

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages — scan as receipt."""
    chat_id = str(update.effective_chat.id)
    added_by = update.effective_user.first_name or "Someone"

    if not ANTHROPIC_API_KEY:
        await update.message.reply_text(
            "⚠️ Receipt scanning not configured yet.\nAsk Jonathan to add the ANTHROPIC_API_KEY!",
            parse_mode="Markdown"
        )
        return

    # Send scanning message
    scanning_msg = await update.message.reply_text("📸 Scanning receipt... give me a sec! ⏳")

    try:
        # Get the largest photo
        photo = update.message.photo[-1]
        photo_file = await ctx.bot.get_file(photo.file_id)

       # Download using Telegram's built-in downloader
        image_bytes = await photo_file.download_as_bytearray()
        image_bytes = bytes(image_bytes)

        print(f"Downloaded image: {len(image_bytes)} bytes")

        if len(image_bytes) < 1000:
            raise Exception(f"Downloaded image too small ({len(image_bytes)} bytes)")
        # Send to Claude
        result = await scan_receipt_with_claude(image_bytes)

        if "error" in result:
            await scanning_msg.edit_text(
                f"😕 *Couldn't read that receipt*\n\n_{result['error']}_\n\nTry a clearer photo or use:\n`amount description`",
                parse_mode="Markdown"
            )
            return

        merchant  = result.get("merchant", "Unknown")
        total     = float(result.get("total", 0))
        category  = result.get("category", "Personal/Shopping")
        items     = result.get("items", [])
        date      = result.get("date") or datetime.now().strftime("%Y-%m-%d")
        confidence = result.get("confidence", "medium")

        if total <= 0:
            await scanning_msg.edit_text(
                "😕 *Couldn't find a total on this receipt.*\n\nTry a clearer photo!",
                parse_mode="Markdown"
            )
            return

        # Log the transaction
        add_tx(chat_id, "expense", total, merchant, category, date, added_by)

        icon = CAT_ICONS.get(category, "•")
        conf_emoji = "🟢" if confidence == "high" else "🟡" if confidence == "medium" else "🔴"

        # Build items text
        items_text = ""
        if items:
            items_text = "\n" + "\n".join(f"  • {item}" for item in items[:5])
            if len(items) > 5:
                items_text += f"\n  _...and {len(items)-5} more_"

        # Category change keyboard
        keyboard = []
        row = []
        for i, cat in enumerate(CATS):
            icon2 = CAT_ICONS.get(cat, "•")
            row.append(InlineKeyboardButton(
                f"{icon2} {cat.split('/')[0]}",
                callback_data=f"quickcat_{total}_{merchant[:20]}_{cat}"
            ))
            if len(row) == 2:
                keyboard.append(row); row = []
        if row: keyboard.append(row)

        await scanning_msg.edit_text(
            f"🧾 *Receipt Scanned!* {conf_emoji}\n"
            f"{'─'*24}\n"
            f"{icon} *{merchant}*{items_text}\n\n"
            f"💸 Total: *{fmt(total)}*\n"
            f"📁 Category: *{category}*\n"
            f"👤 Added by {added_by}\n\n"
            f"_Tap to change category if wrong:_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        # Check budget alert
        await check_budget_alert(update, ctx, chat_id, category)

    except Exception as e:
        print(f"Receipt scan error: {e}")
        await scanning_msg.edit_text(
            "😕 *Something went wrong scanning that receipt.*\n\nTry again or log manually:\n`amount description`",
            parse_mode="Markdown"
        )

# ── SMART QUICK-ADD (just type amount + description) ─────────────────────────
async def smart_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles messages like '45.50 dinner at nobu' without /add prefix."""
    text = update.message.text.strip()
    # Match: number followed by text
    match = re.match(r'^(\d+\.?\d*)\s+(.+)$', text)
    if not match:
        return
    try:
        amount = float(match.group(1))
        desc = match.group(2).strip()
    except:
        return

    chat_id = str(update.effective_chat.id)
    guessed_cat = guess_category(desc)

    if guessed_cat:
        # Auto-log with guessed category
        date = datetime.now().strftime("%Y-%m-%d")
        added_by = update.effective_user.first_name or "Someone"
        add_tx(chat_id, "expense", amount, desc, guessed_cat, date, added_by)
        icon = CAT_ICONS.get(guessed_cat, "•")
        # Build category picker in case they want to change
        keyboard = []
        row = []
        for i, cat in enumerate(CATS):
            icon2 = CAT_ICONS.get(cat, "•")
            row.append(InlineKeyboardButton(f"{icon2} {cat.split('/')[0]}", callback_data=f"quickcat_{amount}_{desc[:20]}_{cat}"))
            if len(row) == 2:
                keyboard.append(row); row = []
        if row: keyboard.append(row)

        await update.message.reply_text(
            f"✅ *Logged!* _{desc}_\n{icon} {guessed_cat} · {fmt(amount)}\n_Tap below to change category_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        await check_budget_alert(update, ctx, chat_id, guessed_cat)
    else:
        # Show category picker
        keyboard = []
        row = []
        for i, cat in enumerate(CATS):
            icon = CAT_ICONS.get(cat, "•")
            row.append(InlineKeyboardButton(f"{icon} {cat.split('/')[0]}", callback_data=f"quickcat_{amount}_{desc[:20]}_{cat}"))
            if len(row) == 2:
                keyboard.append(row); row = []
        if row: keyboard.append(row)
        await update.message.reply_text(
            f"💸 *{fmt(amount)}* — _{desc}_\nWhich category?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def quickcat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    chat_id = str(update.effective_chat.id)
    parts = query.data.replace("quickcat_","").split("_")
    amount = float(parts[0])
    desc = parts[1]
    cat = "_".join(parts[2:])
    # Delete the most recent transaction with same amount to avoid duplicates
    txs = get_txs(chat_id)
    for t in txs:
        if t[3] == amount and t[4] == desc:
            del_tx(t[0], chat_id)
            break
    date = datetime.now().strftime("%Y-%m-%d")
    added_by = update.effective_user.first_name or "Someone"
    add_tx(chat_id, "expense", amount, desc, cat, date, added_by)
    icon = CAT_ICONS.get(cat,"•")
    await query.edit_message_text(f"✅ *Updated!*\n{icon} *{cat}*\n💸 {fmt(amount)} — {desc}\n👤 {added_by}", parse_mode="Markdown")
    await check_budget_alert_query(query, ctx, chat_id, cat)

async def check_budget_alert(update, ctx, chat_id, cat):
    budgets = get_budgets(chat_id)
    bud = budgets.get(cat, 0)
    if not bud: return
    _, _, by_cat = calc_summary(chat_id)
    spent = by_cat.get(cat, 0)
    pct = spent / bud
    if 0.8 <= pct < 0.82:  # Only alert once around 80%
        icon = CAT_ICONS.get(cat,"•")
        await update.message.reply_text(
            f"⚠️ *Budget Alert!*\n\n{icon} {cat} is at *{pct*100:.0f}%* of your {fmt(bud)} budget.\n_{fmt(bud-spent)} remaining_",
            parse_mode="Markdown"
        )

async def check_budget_alert_query(query, ctx, chat_id, cat):
    budgets = get_budgets(chat_id)
    bud = budgets.get(cat, 0)
    if not bud: return
    _, _, by_cat = calc_summary(chat_id)
    spent = by_cat.get(cat, 0)
    pct = spent / bud
    if 0.8 <= pct < 0.82:
        icon = CAT_ICONS.get(cat,"•")
        await query.message.reply_text(
            f"⚠️ *Budget Alert!*\n\n{icon} {cat} is at *{pct*100:.0f}%* of your {fmt(bud)} budget.\n_{fmt(bud-spent)} remaining_",
            parse_mode="Markdown"
        )

# ── COMMANDS ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to Yong's Budget Bot!*\n\n"
        "Track your family spending together 💑\n\n"
        "*✨ Quick tip:* Just type an amount + description and I'll auto-categorise it!\n"
        "e.g. `45.50 dinner at nobu` or `120 grab`\n\n"
        "*Commands:*\n"
        "➕ `/add 45.50 dining dinner` — log expense\n"
        "➕ `/income 5000 paycheck` — log income\n"
        "📊 `/summary` — monthly overview\n"
        "📋 `/spent` — category breakdown\n"
        "🏆 `/report` — monthly report card\n"
        "🧾 `/recent` — last 10 transactions\n"
        "🔁 `/recurring` — manage recurring expenses\n"
        "💰 `/budget groceries 400` — set budget\n"
        "📅 `/month 2026-02` — view past month\n"
        "🗑 `/delete` — delete a transaction\n"
        "❓ `/help` — all commands",
        parse_mode="Markdown",
        reply_markup=main_keyboard())

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *All Commands*\n\n"
        "*✨ Smart add (no command needed!)*\n"
        "Just type: `45.50 dinner at nobu`\n"
        "I'll guess the category automatically!\n\n"
        "*Logging:*\n"
        "`/add <amount> <category> <desc>`\n"
        "`/income <amount> <desc>`\n\n"
        "*Viewing:*\n"
        "`/summary` — monthly overview\n"
        "`/spent` — category breakdown\n"
        "`/report` — report card with grade\n"
        "`/recent` — last 10 transactions\n"
        "`/month YYYY-MM` — past month\n\n"
        "*Budgets:*\n"
        "`/budget <category> <amount>`\n"
        "`/budgets` — see all limits\n\n"
        "*Recurring:*\n"
        "`/recurring` — manage recurring expenses\n\n"
        "*Managing:*\n"
        "`/delete` — delete a transaction\n\n"
        "*Category shortcuts:*\n"
        "`housing` — Housing/Rent\n"
        "`grocery` — Groceries\n"
        "`dining` — Dining Out\n"
        "`sub` — Subscriptions\n"
        "`transport` — Transportation\n"
        "`util` — Utilities\n"
        "`entertain` — Entertainment\n"
        "`saving` — Savings\n"
        "`personal` — Personal/Shopping",
        parse_mode="Markdown")

async def add_expense(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("❌ Usage: `/add <amount> <category> <description>`\nOr just type: `45.50 dinner at nobu`", parse_mode="Markdown"); return
    try:
        amount = float(args[0].replace("$","").replace(",",""))
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", parse_mode="Markdown"); return

    cat_input = args[1].lower()
    matched_cat = next((c for c in CATS if cat_input in c.lower() or c.lower().startswith(cat_input)), "Personal/Shopping")
    desc = " ".join(args[2:]) if len(args) > 2 else matched_cat
    date = datetime.now().strftime("%Y-%m-%d")
    added_by = update.effective_user.first_name or "Someone"
    add_tx(chat_id, "expense", amount, desc, matched_cat, date, added_by)

    icon = CAT_ICONS.get(matched_cat, "•")
    budgets = get_budgets(chat_id)
    _, spent, by_cat = calc_summary(chat_id)
    cat_spent = by_cat.get(matched_cat, 0)
    cat_budget = budgets.get(matched_cat, 0)
    budget_line = ""
    if cat_budget:
        pct = min((cat_spent/cat_budget)*100, 100)
        status = budget_status(pct)
        budget_line = f"\n{status} `{progress_bar(pct,8)}` {pct:.0f}% of {fmt(cat_budget)}"

    await update.message.reply_text(
        f"✅ *Expense logged!*\n\n{icon} *{matched_cat}*\n💸 {fmt(amount)} — {desc}\n👤 {added_by}{budget_line}",
        parse_mode="Markdown")
    await check_budget_alert(update, ctx, chat_id, matched_cat)

async def add_income(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = ctx.args
    if not args:
        await update.message.reply_text("❌ Usage: `/income <amount> <description>`", parse_mode="Markdown"); return
    try:
        amount = float(args[0].replace("$","").replace(",",""))
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", parse_mode="Markdown"); return
    desc = " ".join(args[1:]) if len(args) > 1 else "Income"
    date = datetime.now().strftime("%Y-%m-%d")
    added_by = update.effective_user.first_name or "Someone"
    add_tx(chat_id, "income", amount, desc, "Income", date, added_by)
    await update.message.reply_text(
        f"✅ *Income logged!*\n\n💵 *{fmt(amount)}* — {desc}\n👤 {added_by}",
        parse_mode="Markdown")

async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE, month=None):
    chat_id = str(update.effective_chat.id)
    month = month or get_month()
    income, spent, by_cat = calc_summary(chat_id, month)
    remaining = income - spent
    budgets = get_budgets(chat_id)
    total_budget = sum(budgets.values())
    pct = (spent/total_budget*100) if total_budget else 0
    rem_icon = "✅" if remaining >= 0 else "🔴"
    savings_rate = (remaining/income*100) if income > 0 else 0

    text = (f"📊 *{month_label(month)} Summary*\n"
            f"{'─'*28}\n"
            f"💵 Income:      *{fmt(income)}*\n"
            f"💸 Spent:       *{fmt(spent)}*\n"
            f"{rem_icon} Remaining:  *{fmt(abs(remaining))}*{'  left' if remaining>=0 else '  over!'}\n")
    if income > 0:
        text += f"📈 Savings rate: *{savings_rate:.0f}%*\n"
    if total_budget:
        status = budget_status(pct)
        text += f"\n{status} `{progress_bar(pct,12)}` {pct:.0f}% of {fmt(total_budget)} budget\n"
    if by_cat:
        text += f"\n*Top categories:*\n{'─'*28}\n"
        for cat, amt in sorted(by_cat.items(), key=lambda x:-x[1])[:5]:
            bud = budgets.get(cat,0)
            status = budget_status((amt/bud*100) if bud else 0) if bud else "  "
            bud_str = f" / {fmt(bud)}" if bud else ""
            text += f"{CAT_ICONS.get(cat,'•')} {cat}: *{fmt(amt)}*{bud_str} {status}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def report_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Monthly report card with grade."""
    chat_id = str(update.effective_chat.id)
    month = get_month()
    income, spent, by_cat = calc_summary(chat_id, month)
    budgets = get_budgets(chat_id)
    total_budget = sum(budgets.values())

    # Compare to last month
    last_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    _, last_spent, last_by_cat = calc_summary(chat_id, last_month)
    diff = spent - last_spent
    diff_str = f"{'📈 +' if diff > 0 else '📉 '}{fmt(abs(diff))} vs last month"

    pct_used = (spent/total_budget) if total_budget else 0
    grade, grade_msg = get_grade(pct_used)
    savings_rate = ((income-spent)/income*100) if income > 0 else 0

    text = (f"🏆 *{month_label()} Report Card*\n"
            f"{'─'*28}\n"
            f"Grade: *{grade}* — {grade_msg}\n"
            f"{'─'*28}\n"
            f"💵 Income:   *{fmt(income)}*\n"
            f"💸 Spent:    *{fmt(spent)}*\n"
            f"💰 Saved:    *{fmt(income-spent)}* ({savings_rate:.0f}%)\n"
            f"{diff_str}\n")

    if total_budget:
        text += f"\n*Category breakdown:*\n{'─'*28}\n"
        for cat in CATS:
            s = by_cat.get(cat, 0)
            b = budgets.get(cat, 0)
            if not s and not b: continue
            icon = CAT_ICONS.get(cat,"•")
            if b:
                pct = min((s/b)*100,100)
                status = budget_status(pct)
                bar = progress_bar(pct, 6)
                text += f"{icon} {cat.split('/')[0]}\n`{bar}` {fmt(s)}/{fmt(b)} {status}\n"
            else:
                text += f"{icon} {cat.split('/')[0]}: {fmt(s)}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def spent_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    income, total_spent, by_cat = calc_summary(chat_id)
    budgets = get_budgets(chat_id)
    if not by_cat:
        await update.message.reply_text(f"No expenses yet for {month_label()}.\n\nJust type an amount to log one!\ne.g. `45.50 dinner`", parse_mode="Markdown"); return

    text = f"📋 *Spending — {month_label()}*\n{'─'*28}\n\n"
    for cat in CATS:
        spent = by_cat.get(cat,0); bud = budgets.get(cat,0)
        if not spent and not bud: continue
        icon = CAT_ICONS.get(cat,"•")
        if bud:
            pct = min((spent/bud)*100,100)
            status = budget_status(pct)
            text += f"{icon} *{cat}*\n`{progress_bar(pct,8)}` {fmt(spent)} / {fmt(bud)} {status}\n\n"
        else:
            text += f"{icon} *{cat}*: {fmt(spent)}\n\n"
    text += f"{'─'*28}\n💸 *Total: {fmt(total_spent)}*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    txs = get_txs(chat_id)[:10]
    if not txs:
        await update.message.reply_text("No transactions this month yet!\n\nJust type an amount to log one!\ne.g. `45.50 dinner`", parse_mode="Markdown"); return
    text = f"🧾 *Recent Transactions*\n{'─'*28}\n\n"
    for t in txs:
        icon = "💵" if t[2]=="income" else CAT_ICONS.get(t[5],"•")
        sign = "+" if t[2]=="income" else "-"
        date_str = datetime.strptime(t[6],"%Y-%m-%d").strftime("%b %d")
        text += f"{icon} *{sign}{fmt(t[3])}*  {t[4]}\n_{date_str} · {t[7]}_\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    txs = get_txs(chat_id)[:8]
    if not txs:
        await update.message.reply_text("No transactions to delete!", parse_mode="Markdown"); return
    keyboard = []
    for t in txs:
        icon = "💵" if t[2]=="income" else CAT_ICONS.get(t[5],"•")
        sign = "+" if t[2]=="income" else "-"
        date_str = datetime.strptime(t[6],"%Y-%m-%d").strftime("%b %d")
        keyboard.append([InlineKeyboardButton(f"{icon} {sign}${t[3]:.0f} {t[4][:18]} · {date_str}", callback_data=f"del_{t[0]}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="del_cancel")])
    await update.message.reply_text("🗑 *Which transaction to delete?*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    chat_id = str(update.effective_chat.id)
    if query.data == "del_cancel":
        await query.edit_message_text("Cancelled."); return
    del_tx(int(query.data.replace("del_","")), chat_id)
    await query.edit_message_text("✅ Transaction deleted.")

async def budget_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id); args = ctx.args
    if len(args) < 2:
        # Show inline keyboard for category selection
        keyboard = [[InlineKeyboardButton(f"{CAT_ICONS.get(c,'•')} {c.split('/')[0]}", callback_data=f"budcat_{c}")] for c in CATS]
        await update.message.reply_text("💰 *Which category to set budget for?*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)); return
    try:
        amount = float(args[-1].replace("$","").replace(",",""))
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", parse_mode="Markdown"); return
    cat_input = " ".join(args[:-1]).lower()
    matched_cat = next((c for c in CATS if cat_input in c.lower() or c.lower().startswith(cat_input)), None)
    if not matched_cat:
        await update.message.reply_text("❌ Category not found.", parse_mode="Markdown"); return
    set_budget(chat_id, matched_cat, amount)
    await update.message.reply_text(f"✅ *Budget set!*\n\n{CAT_ICONS.get(matched_cat,'•')} *{matched_cat}*\n💰 {fmt(amount)}/month", parse_mode="Markdown")

async def budcat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    cat = query.data.replace("budcat_","")
    await query.edit_message_text(
        f"💰 Setting budget for *{cat}*\n\nReply with the amount, e.g.:\n`/budget {cat.lower().split('/')[0]} 500`",
        parse_mode="Markdown")

async def budgets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    bud = get_budgets(chat_id)
    _, spent, by_cat = calc_summary(chat_id)
    if not bud:
        await update.message.reply_text("No budgets set yet!\nUse `/budget <category> <amount>`", parse_mode="Markdown"); return
    text = f"💰 *Monthly Budgets*\n{'─'*28}\n\n"
    for cat in CATS:
        if cat in bud:
            s = by_cat.get(cat, 0)
            pct = min((s/bud[cat])*100,100) if bud[cat] else 0
            status = budget_status(pct)
            text += f"{CAT_ICONS.get(cat,'•')} *{cat}*: {fmt(bud[cat])} {status}\n"
    text += f"\n{'─'*28}\n📊 Total: *{fmt(sum(bud.values()))}/month*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def recurring_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = ctx.args
    recurrings = get_recurring(chat_id)

    if not args:
        # Show current recurring + options
        text = f"🔁 *Recurring Expenses*\n{'─'*28}\n\n"
        if recurrings:
            for r in recurrings:
                icon = CAT_ICONS.get(r[4],"•")
                text += f"{icon} *{fmt(r[2])}* — {r[3]}\n_{r[4]} · day {r[5]} of each month_\n\n"
        else:
            text += "_No recurring expenses set yet._\n\n"
        text += "To add: `/recurring add <amount> <category> <description> <day>`\nExample: `/recurring add 3200 housing rent 1`\n\nTo remove: `/recurring remove`"
        await update.message.reply_text(text, parse_mode="Markdown"); return

    if args[0] == "add":
        if len(args) < 5:
            await update.message.reply_text("Usage: `/recurring add <amount> <category> <description> <day>`\nExample: `/recurring add 3200 housing rent 1`", parse_mode="Markdown"); return
        try:
            amount = float(args[1])
            day = int(args[-1])
            cat_input = args[2].lower()
            matched_cat = next((c for c in CATS if cat_input in c.lower() or c.lower().startswith(cat_input)), "Personal/Shopping")
            desc = " ".join(args[3:-1])
        except:
            await update.message.reply_text("❌ Invalid format.", parse_mode="Markdown"); return
        add_recurring(chat_id, amount, desc, matched_cat, day)
        icon = CAT_ICONS.get(matched_cat,"•")
        await update.message.reply_text(f"✅ *Recurring added!*\n\n{icon} *{matched_cat}*\n💸 {fmt(amount)} — {desc}\n📅 Logs automatically on day {day} each month", parse_mode="Markdown"); return

    if args[0] == "remove":
        if not recurrings:
            await update.message.reply_text("No recurring expenses to remove!", parse_mode="Markdown"); return
        keyboard = [[InlineKeyboardButton(f"{CAT_ICONS.get(r[4],'•')} {fmt(r[2])} {r[3]}", callback_data=f"delrec_{r[0]}")] for r in recurrings]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="del_cancel")])
        await update.message.reply_text("Which recurring expense to remove?", reply_markup=InlineKeyboardMarkup(keyboard)); return

async def delrec_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    chat_id = str(update.effective_chat.id)
    r_id = int(query.data.replace("delrec_",""))
    del_recurring(r_id, chat_id)
    await query.edit_message_text("✅ Recurring expense removed.")

async def month_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: `/month YYYY-MM`", parse_mode="Markdown"); return
    try: datetime.strptime(args[0], "%Y-%m")
    except ValueError:
        await update.message.reply_text("❌ Use format YYYY-MM e.g. `2026-02`", parse_mode="Markdown"); return
    await summary(update, ctx, month=args[0])

async def handle_reply_keyboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle persistent reply keyboard button taps."""
    text = update.message.text
    mapping = {
        "📊 Summary": summary,
        "📋 Spent": spent_cmd,
        "🏆 Report": report_cmd,
        "🧾 Recent": recent,
        "💰 Budgets": budgets_cmd,
        "🗑 Delete": delete_cmd,
        "🔁 Recurring": recurring_cmd,
        "❓ Help": help_cmd,
    }
    if text in mapping:
        await mapping[text](update, ctx)
    else:
        await smart_add(update, ctx)

async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤷 Unknown command. Type /help to see all commands.")

# ── SCHEDULED TASKS ───────────────────────────────────────────────────────────
async def weekly_digest(app):
    """Send weekly summary every Sunday."""
    chats = get_all_chats()
    for chat_id in chats:
        try:
            month = get_month()
            income, spent, by_cat = calc_summary(chat_id, month)
            budgets = get_budgets(chat_id)
            total_budget = sum(budgets.values())
            pct = (spent/total_budget*100) if total_budget else 0
            grade, grade_msg = get_grade(pct/100 if total_budget else 0)
            text = (f"📅 *Weekly Digest — {month_label()}*\n"
                    f"{'─'*28}\n"
                    f"Grade so far: *{grade}* {grade_msg}\n\n"
                    f"💵 Income:  {fmt(income)}\n"
                    f"💸 Spent:   {fmt(spent)}\n"
                    f"💰 Left:    *{fmt(income-spent)}*\n")
            if total_budget:
                text += f"\n{budget_status(pct)} `{progress_bar(pct,12)}` {pct:.0f}% of budget used\n"
            if by_cat:
                top = sorted(by_cat.items(), key=lambda x:-x[1])[:3]
                text += "\n*Top spending:*\n"
                for cat, amt in top:
                    text += f"{CAT_ICONS.get(cat,'•')} {cat}: {fmt(amt)}\n"
            text += "\n_Type /report for full breakdown_"
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            print(f"Weekly digest error for {chat_id}: {e}")

async def process_recurring(app):
    """Auto-log recurring expenses on their due date."""
    today = datetime.now()
    chats = get_all_chats()
    for chat_id in chats:
        recurrings = get_recurring(chat_id)
        for r in recurrings:
            if r[5] == today.day:
                date = today.strftime("%Y-%m-%d")
                add_tx(chat_id, "expense", r[2], r[3], r[4], date, "Auto")
                icon = CAT_ICONS.get(r[4],"•")
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔁 *Recurring expense logged!*\n\n{icon} *{r[4]}*\n💸 {fmt(r[2])} — {r[3]}",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    print(f"Recurring error: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        print("ERROR: BOT_TOKEN not set!"); return
    print(f"Starting... token: {TOKEN[:10]}")
    print(f"DB: {'PostgreSQL' if DATABASE_URL else 'SQLite'}")
    init_db()
    try:
        app = Application.builder().token(TOKEN).build()

        # Commands
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_cmd))
        app.add_handler(CommandHandler("add", add_expense))
        app.add_handler(CommandHandler("income", add_income))
        app.add_handler(CommandHandler("summary", summary))
        app.add_handler(CommandHandler("spent", spent_cmd))
        app.add_handler(CommandHandler("report", report_cmd))
        app.add_handler(CommandHandler("recent", recent))
        app.add_handler(CommandHandler("delete", delete_cmd))
        app.add_handler(CommandHandler("budget", budget_cmd))
        app.add_handler(CommandHandler("budgets", budgets_cmd))
        app.add_handler(CommandHandler("recurring", recurring_cmd))
        app.add_handler(CommandHandler("month", month_cmd))
        app.add_handler(CommandHandler("help", help_cmd))

        # Callbacks
        app.add_handler(CallbackQueryHandler(delete_callback, pattern="^del_"))
        app.add_handler(CallbackQueryHandler(quickcat_callback, pattern="^quickcat_"))
        app.add_handler(CallbackQueryHandler(budcat_callback, pattern="^budcat_"))
        app.add_handler(CallbackQueryHandler(delrec_callback, pattern="^delrec_"))

        # Smart quick-add (non-command messages)
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply_keyboard))
        app.add_handler(MessageHandler(filters.COMMAND, unknown))

        # Scheduler
        scheduler = AsyncIOScheduler()
        scheduler.add_job(weekly_digest, 'cron', day_of_week='sun', hour=9, minute=0, args=[app])
        scheduler.add_job(process_recurring, 'cron', hour=8, minute=0, args=[app])
        scheduler.start()

        print("🤖 Budget bot is running!")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback; traceback.print_exc(); raise

if __name__ == "__main__":
    main()
