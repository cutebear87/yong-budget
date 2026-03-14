import os
import json
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TOKEN = os.environ.get("BOT_TOKEN", "")
DB    = "budget.db"

CATS = [
    "Housing/Rent", "Groceries", "Dining Out", "Subscriptions",
    "Transportation", "Utilities", "Entertainment", "Savings", "Personal/Shopping"
]
CAT_ICONS = {
    "Housing/Rent":"🏠","Groceries":"🛒","Dining Out":"🍽",
    "Subscriptions":"📱","Transportation":"🚗","Utilities":"⚡",
    "Entertainment":"🎬","Savings":"💰","Personal/Shopping":"🛍","Income":"💵"
}

# ── DATABASE ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            type TEXT,
            amount REAL,
            description TEXT,
            category TEXT,
            date TEXT,
            added_by TEXT,
            month TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            chat_id TEXT,
            category TEXT,
            amount REAL,
            PRIMARY KEY (chat_id, category)
        )
    """)
    con.commit()
    con.close()

def db():
    return sqlite3.connect(DB)

def get_month():
    return datetime.now().strftime("%Y-%m")

def get_txs(chat_id, month=None):
    month = month or get_month()
    con = db()
    rows = con.execute(
        "SELECT * FROM transactions WHERE chat_id=? AND month=? ORDER BY date DESC, id DESC",
        (chat_id, month)
    ).fetchall()
    con.close()
    return rows  # id,chat_id,type,amount,desc,cat,date,added_by,month

def add_tx(chat_id, type_, amount, desc, cat, date, added_by):
    month = date[:7]
    con = db()
    con.execute(
        "INSERT INTO transactions (chat_id,type,amount,description,category,date,added_by,month) VALUES (?,?,?,?,?,?,?,?)",
        (chat_id, type_, amount, desc, cat, date, added_by, month)
    )
    con.commit()
    con.close()

def del_tx(tx_id, chat_id):
    con = db()
    con.execute("DELETE FROM transactions WHERE id=? AND chat_id=?", (tx_id, chat_id))
    con.commit()
    con.close()

def get_budgets(chat_id):
    con = db()
    rows = con.execute("SELECT category, amount FROM budgets WHERE chat_id=?", (chat_id,)).fetchall()
    con.close()
    return {r[0]: r[1] for r in rows}

def set_budget(chat_id, cat, amount):
    con = db()
    con.execute("INSERT OR REPLACE INTO budgets (chat_id,category,amount) VALUES (?,?,?)",
                (chat_id, cat, amount))
    con.commit()
    con.close()

# ── HELPERS ───────────────────────────────────────────────────────────────────
def fmt(n):
    return f"${n:,.2f}"

def month_label(month_str=None):
    m = month_str or get_month()
    dt = datetime.strptime(m, "%Y-%m")
    return dt.strftime("%B %Y")

def calc_summary(chat_id, month=None):
    txs = get_txs(chat_id, month)
    income = sum(t[3] for t in txs if t[2] == "income")
    spent  = sum(t[3] for t in txs if t[2] == "expense")
    by_cat = {}
    for t in txs:
        if t[2] == "expense":
            by_cat[t[5]] = by_cat.get(t[5], 0) + t[3]
    return income, spent, by_cat

def progress_bar(pct, width=10):
    filled = round(pct / 100 * width)
    filled = max(0, min(filled, width))
    bar = "█" * filled + "░" * (width - filled)
    return bar

# ── COMMANDS ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Welcome to your Family Budget Bot!*\n\n"
        "Both you and your wife can use this chat to track spending together.\n\n"
        "*Quick commands:*\n"
        "➕ `/add 45.50 Groceries lunch` — log an expense\n"
        "➕ `/income 5000 Paycheck` — log income\n"
        "📊 `/summary` — monthly overview\n"
        "📋 `/spent` — breakdown by category\n"
        "🧾 `/recent` — last 10 transactions\n"
        "💰 `/budget Housing 3200` — set a budget limit\n"
        "📅 `/month 2026-02` — view a past month\n"
        "❓ `/help` — all commands\n\n"
        "_Tip: Add your wife to this chat so you both see every transaction in real time!_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *All Commands*\n\n"
        "*Logging:*\n"
        "`/add <amount> <category> <description>`\n"
        "  e.g. `/add 64 dining dinner at nobu`\n\n"
        "`/income <amount> <description>`\n"
        "  e.g. `/income 5000 paycheck`\n\n"
        "*Viewing:*\n"
        "`/summary` — income, spent, remaining\n"
        "`/spent` — by category with budget bars\n"
        "`/recent` — last 10 transactions\n"
        "`/month YYYY-MM` — view past month\n\n"
        "*Budgets:*\n"
        "`/budget <category> <amount>`\n"
        "  e.g. `/budget groceries 400`\n"
        "`/budgets` — see all budget limits\n\n"
        "*Managing:*\n"
        "`/delete` — delete a recent transaction\n\n"
        "*Categories:*\n"
        + "  ".join(f"{CAT_ICONS.get(c,'•')} {c}" for c in CATS)
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def add_expense(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/add <amount> <category> <description>`\n"
            "Example: `/add 64 dining dinner at nobu`",
            parse_mode="Markdown"
        )
        return

    try:
        amount = float(args[0].replace("$","").replace(",",""))
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Example: `/add 64.50 groceries trader joes`", parse_mode="Markdown")
        return

    # match category
    cat_input = args[1].lower()
    matched_cat = None
    for c in CATS:
        if cat_input in c.lower() or c.lower().startswith(cat_input):
            matched_cat = c
            break
    if not matched_cat:
        matched_cat = "Personal/Shopping"

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
        pct = min((cat_spent / cat_budget) * 100, 100)
        bar = progress_bar(pct, 8)
        budget_line = f"\n`{bar}` {pct:.0f}% of {fmt(cat_budget)} budget"
        if pct >= 90:
            budget_line += " ⚠️"

    text = (
        f"✅ *Expense logged!*\n\n"
        f"{icon} *{matched_cat}*\n"
        f"💸 {fmt(amount)} — {desc}\n"
        f"👤 Added by {added_by}{budget_line}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def add_income(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = ctx.args
    if not args:
        await update.message.reply_text("❌ Usage: `/income <amount> <description>`\nExample: `/income 5000 paycheck`", parse_mode="Markdown")
        return

    try:
        amount = float(args[0].replace("$","").replace(",",""))
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", parse_mode="Markdown")
        return

    desc = " ".join(args[1:]) if len(args) > 1 else "Income"
    date = datetime.now().strftime("%Y-%m-%d")
    added_by = update.effective_user.first_name or "Someone"
    add_tx(chat_id, "income", amount, desc, "Income", date, added_by)

    await update.message.reply_text(
        f"✅ *Income logged!*\n\n💵 {fmt(amount)} — {desc}\n👤 Added by {added_by}",
        parse_mode="Markdown"
    )

async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE, month=None):
    chat_id = str(update.effective_chat.id)
    month = month or get_month()
    income, spent, by_cat = calc_summary(chat_id, month)
    remaining = income - spent
    budgets = get_budgets(chat_id)
    total_budget = sum(budgets.values())
    pct = (spent / total_budget * 100) if total_budget else 0
    bar = progress_bar(pct, 12)

    rem_icon = "✅" if remaining >= 0 else "🔴"

    text = (
        f"📊 *{month_label(month)} Summary*\n\n"
        f"💵 Income:     {fmt(income)}\n"
        f"💸 Spent:      {fmt(spent)}\n"
        f"{rem_icon} Remaining: *{fmt(abs(remaining))}*{'  left' if remaining >= 0 else '  over!'}\n"
    )
    if total_budget:
        text += f"\n`{bar}` {pct:.0f}% of {fmt(total_budget)} total budget\n"

    if by_cat:
        text += "\n*Top categories:*\n"
        for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])[:5]:
            icon = CAT_ICONS.get(cat, "•")
            bud = budgets.get(cat, 0)
            bud_str = f" / {fmt(bud)}" if bud else ""
            text += f"{icon} {cat}: {fmt(amt)}{bud_str}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def spent_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    month = get_month()
    income, total_spent, by_cat = calc_summary(chat_id, month)
    budgets = get_budgets(chat_id)

    if not by_cat:
        await update.message.reply_text(f"No expenses yet for {month_label()}. Use `/add` to log one!", parse_mode="Markdown")
        return

    text = f"📋 *Spending — {month_label()}*\n\n"
    for cat in CATS:
        spent = by_cat.get(cat, 0)
        bud = budgets.get(cat, 0)
        if not spent and not bud:
            continue
        icon = CAT_ICONS.get(cat, "•")
        if bud:
            pct = min((spent / bud) * 100, 100)
            bar = progress_bar(pct, 8)
            status = "⚠️" if pct >= 90 else ("🔴" if spent > bud else "")
            text += f"{icon} *{cat}*\n`{bar}` {fmt(spent)} / {fmt(bud)} {status}\n\n"
        else:
            text += f"{icon} *{cat}*: {fmt(spent)}\n\n"

    text += f"💸 *Total: {fmt(total_spent)}*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    txs = get_txs(chat_id)[:10]
    if not txs:
        await update.message.reply_text("No transactions this month yet!", parse_mode="Markdown")
        return

    text = f"🧾 *Recent Transactions*\n\n"
    for t in txs:
        # t: id,chat_id,type,amount,desc,cat,date,added_by,month
        icon = "💵" if t[2] == "income" else CAT_ICONS.get(t[5], "•")
        sign = "+" if t[2] == "income" else "-"
        date_str = datetime.strptime(t[6], "%Y-%m-%d").strftime("%b %d")
        text += f"{icon} `{sign}{fmt(t[3])}` {t[4]} _{date_str} · {t[7]}_\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    txs = get_txs(chat_id)[:8]
    if not txs:
        await update.message.reply_text("No transactions to delete!", parse_mode="Markdown")
        return

    keyboard = []
    for t in txs:
        icon = "💵" if t[2] == "income" else CAT_ICONS.get(t[5], "•")
        sign = "+" if t[2] == "income" else "-"
        label = f"{icon} {sign}${t[3]:.0f} {t[4][:20]}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"del_{t[0]}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="del_cancel")])

    await update.message.reply_text(
        "Which transaction do you want to delete?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def delete_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = str(update.effective_chat.id)

    if query.data == "del_cancel":
        await query.edit_message_text("Cancelled.")
        return

    tx_id = int(query.data.replace("del_", ""))
    del_tx(tx_id, chat_id)
    await query.edit_message_text("✅ Transaction deleted.")

async def budget_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = ctx.args

    if len(args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/budget <category> <amount>`\n"
            "Example: `/budget groceries 400`\n\n"
            "Categories: " + ", ".join(CATS),
            parse_mode="Markdown"
        )
        return

    try:
        amount = float(args[-1].replace("$","").replace(",",""))
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", parse_mode="Markdown")
        return

    cat_input = " ".join(args[:-1]).lower()
    matched_cat = None
    for c in CATS:
        if cat_input in c.lower() or c.lower().startswith(cat_input):
            matched_cat = c
            break

    if not matched_cat:
        await update.message.reply_text(
            f"❌ Category not found. Choose from:\n" + "\n".join(f"• {c}" for c in CATS),
            parse_mode="Markdown"
        )
        return

    set_budget(chat_id, matched_cat, amount)
    icon = CAT_ICONS.get(matched_cat, "•")
    await update.message.reply_text(
        f"✅ Budget set!\n\n{icon} *{matched_cat}*: {fmt(amount)}/month",
        parse_mode="Markdown"
    )

async def budgets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    bud = get_budgets(chat_id)
    if not bud:
        await update.message.reply_text(
            "No budgets set yet!\nUse `/budget <category> <amount>` to set one.",
            parse_mode="Markdown"
        )
        return

    total = sum(bud.values())
    text = f"💰 *Monthly Budgets*\n\n"
    for cat in CATS:
        if cat in bud:
            icon = CAT_ICONS.get(cat, "•")
            text += f"{icon} {cat}: *{fmt(bud[cat])}*\n"
    text += f"\n📊 Total: *{fmt(total)}/month*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def month_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: `/month YYYY-MM`\nExample: `/month 2026-02`", parse_mode="Markdown")
        return
    try:
        datetime.strptime(args[0], "%Y-%m")
    except ValueError:
        await update.message.reply_text("❌ Invalid format. Use YYYY-MM, e.g. `2026-02`", parse_mode="Markdown")
        return
    await summary(update, ctx, month=args[0])

async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤷 Unknown command. Type /help to see all commands.",
        parse_mode="Markdown"
    )

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        print("ERROR: BOT_TOKEN environment variable not set!")
        return

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_expense))
    app.add_handler(CommandHandler("income", add_income))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("spent", spent_cmd))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("budget", budget_cmd))
    app.add_handler(CommandHandler("budgets", budgets_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern="^del_"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("🤖 Budget bot is running!")
    app.run_polling()

if __name__ == "__main__":
    main()
