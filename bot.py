import os, sqlite3, re, base64, httpx, asyncio, json, time, difflib
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TOKEN             = os.environ.get("BOT_TOKEN", "")
DATABASE_URL      = os.environ.get("DATABASE_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_SHEETS_CREDS = os.environ.get("GOOGLE_SHEETS_CREDS", "")
GOOGLE_SHEET_ID     = os.environ.get("GOOGLE_SHEET_ID", "")

# CATS order is FIXED — never reorder. Chart ranges A2:C10 depend on row stability.
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
    "Groceries":["grocery","groceries","supermarket","fairprice","ntuc","cold storage","giant","walmart","costco"],
    "Dining Out":["dining","restaurant","cafe","coffee","starbucks","mcdonald","burger","pizza","sushi","hawker",
                  "kopitiam","deliveroo","foodpanda","nobu","brunch","lunch","dinner","breakfast","meal","supper",
                  "grab food","grabfood","eatery","bistro","ramen","pho","bak kut"],
    "Transportation":["grab","taxi","uber","mrt","bus","transport","petrol","gas","parking","ez-link","train","gojek","car","lyft","commute"],
    "Subscriptions":["netflix","spotify","apple","google","subscription","sub","amazon prime","youtube","disney","hulu","paramount","icloud"],
    "Entertainment":["movie","cinema","concert","game","entertainment","arcade","bowling","escape room","museum","zoo","theme park"],
    "Utilities":["electric","water","internet","phone","bill","utility","telco","singtel","starhub","m1","electricity","sp group"],
    "Housing/Rent":["rent","mortgage","condo","hdb","housing","property","maintenance","strata"],
    "Savings":["savings","save","investment","invest","cpf","srs","endowment","stocks","etf"],
    "Personal/Shopping":["shopping","clothes","shoes","haircut","gym","beauty","personal","lazada","shopee","amazon","taobao","uniqlo","zara","h&m"],
}

# Keywords with strong category signal but lower weight (generic food terms)
CAT_KEYWORDS_LOW = {
    "Groceries": ["food","market","fresh","produce"],
}

# Keywords that suggest a message is income rather than an expense
INCOME_KEYWORDS = [
    "salary","paycheck","payday","pay","wage","bonus","dividend","refund",
    "reimbursement","allowance","freelance","invoice","transfer in","received",
    "commission","interest","cashback","rebate","income"
]


# ── CONNECTION POOL ───────────────────────────────────────────────────────────
# Neon free tier: 10 connections max.
# We use 1–3 so the rest stay available for Neon's own overhead + future use.
# SQLite uses a single persistent connection (no pool needed).

_sqlite_conn = None   # reused for the lifetime of the process
_pg_pool     = None   # psycopg2 ThreadedConnectionPool
_DB_TYPE     = "sqlite"

def _init_pool():
    """Called once at startup from init_db(). Sets up the right backend."""
    global _sqlite_conn, _pg_pool, _DB_TYPE
    if DATABASE_URL:
        import psycopg2.pool
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=3,   # safe for Neon free tier (limit is 10)
            dsn=DATABASE_URL.replace("postgres://", "postgresql://", 1)
        )
        _DB_TYPE = "pg"
        print("PostgreSQL pool initialised (1–3 connections, Neon-safe)")
    else:
        _sqlite_conn = sqlite3.connect("budget.db", check_same_thread=False)
        _sqlite_conn.isolation_level = None   # autocommit mode
        _DB_TYPE = "sqlite"
        print("SQLite connection initialised")

def _get_conn():
    """
    Borrow a connection from the pool (Postgres) or return the
    shared SQLite connection. Always pair with _release_conn().
    """
    if _pg_pool:
        return _pg_pool.getconn(), True
    return _sqlite_conn, False

def _release_conn(conn, pooled: bool):
    """Return a Postgres connection to the pool. No-op for SQLite."""
    if pooled and _pg_pool:
        _pg_pool.putconn(conn)

def ph(): return "%s" if _DB_TYPE == "pg" else "?"

# ── DB HELPERS ────────────────────────────────────────────────────────────────

def _execute(query: str, params: tuple = (), *, fetch: str = "none"):
    """
    Central query runner. Borrows a connection, runs the query,
    releases back to pool in finally block, returns rows or None.

    fetch: "none" | "one" | "all"
    Writes (INSERT/UPDATE/DELETE) are committed automatically.
    """
    conn, pooled = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        if fetch == "all":
            return cur.fetchall()
        if fetch == "one":
            return cur.fetchone()
        # write — commit for SQLite (Postgres autocommits via pool default)
        if _DB_TYPE == "sqlite":
            conn.commit()
        else:
            conn.commit()
        return None
    except Exception as e:
        print(f"DB error: {e}\nQuery: {query}\nParams: {params}")
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _release_conn(conn, pooled)

def init_db():
    """Create tables, indexes, and initialise the connection pool."""
    _init_pool()
    p = ph()

    # Tables
    _execute("""CREATE TABLE IF NOT EXISTS transactions (
        id BIGINT PRIMARY KEY, chat_id TEXT, type TEXT, amount REAL,
        description TEXT, category TEXT, date TEXT, added_by TEXT, month TEXT
    )""")
    _execute("""CREATE TABLE IF NOT EXISTS budgets (
        chat_id TEXT, category TEXT, amount REAL,
        PRIMARY KEY (chat_id, category)
    )""")
    _execute("""CREATE TABLE IF NOT EXISTS recurring (
        id BIGINT PRIMARY KEY, chat_id TEXT, amount REAL,
        description TEXT, category TEXT, day_of_month INTEGER
    )""")
    _execute("""CREATE TABLE IF NOT EXISTS settings (
        chat_id TEXT PRIMARY KEY,
        weekly_digest BOOLEAN DEFAULT TRUE,
        alert_threshold REAL DEFAULT 0.8
    )""")
    _execute("""CREATE TABLE IF NOT EXISTS learned_mappings (
        chat_id TEXT, keyword TEXT, category TEXT,
        count INTEGER DEFAULT 1,
        PRIMARY KEY (chat_id, keyword)
    )""")

    # Indexes — speeds up the most common query patterns
    _execute("CREATE INDEX IF NOT EXISTS idx_tx_chat_month ON transactions(chat_id, month)")
    _execute("CREATE INDEX IF NOT EXISTS idx_tx_chat_date  ON transactions(chat_id, date)")
    _execute("CREATE INDEX IF NOT EXISTS idx_learned_chat  ON learned_mappings(chat_id)")

    print("Database initialised OK.")

# ── TRANSACTION FUNCTIONS ─────────────────────────────────────────────────────

def get_txs(chat_id, month=None):
    month = month or get_month()
    p = ph()
    rows = _execute(
        f"SELECT id,chat_id,type,amount,description,category,date,added_by,month "
        f"FROM transactions WHERE chat_id={p} AND month={p} "
        f"ORDER BY date DESC, id DESC",
        (chat_id, month), fetch="all"
    )
    return rows or []

def add_tx(chat_id, type_, amount, desc, cat, date, added_by):
    month = date[:7]
    tx_id = int(datetime.now().timestamp() * 1000)
    p = ph()
    _execute(
        f"INSERT INTO transactions "
        f"(id,chat_id,type,amount,description,category,date,added_by,month) "
        f"VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})",
        (tx_id, chat_id, type_, amount, desc, cat, date, added_by, month)
    )
    return tx_id, date, month, type_, amount, cat, desc, added_by

def del_tx(tx_id, chat_id):
    p = ph()
    _execute(f"DELETE FROM transactions WHERE id={p} AND chat_id={p}", (tx_id, chat_id))

def update_tx_category(tx_id, chat_id, new_cat):
    p = ph()
    _execute(
        f"UPDATE transactions SET category={p} WHERE id={p} AND chat_id={p}",
        (new_cat, tx_id, chat_id)
    )

def get_tx(tx_id, chat_id):
    p = ph()
    return _execute(
        f"SELECT id,chat_id,type,amount,description,category,date,added_by,month "
        f"FROM transactions WHERE id={p} AND chat_id={p}",
        (tx_id, chat_id), fetch="one"
    )

def get_txs_by_date(chat_id, date_str):
    p = ph()
    rows = _execute(
        f"SELECT id,chat_id,type,amount,description,category,date,added_by,month "
        f"FROM transactions WHERE chat_id={p} AND date={p} ORDER BY id ASC",
        (chat_id, date_str), fetch="all"
    )
    return rows or []

def get_all_chats():
    rows = _execute("SELECT DISTINCT chat_id FROM transactions", fetch="all")
    return [r[0] for r in rows] if rows else []

# ── BUDGET FUNCTIONS ──────────────────────────────────────────────────────────

def get_budgets(chat_id):
    p = ph()
    rows = _execute(
        f"SELECT category, amount FROM budgets WHERE chat_id={p}", (chat_id,), fetch="all"
    )
    return {r[0]: r[1] for r in rows} if rows else {}

def set_budget(chat_id, cat, amount):
    p = ph()
    if _DB_TYPE == "pg":
        _execute(
            f"INSERT INTO budgets (chat_id,category,amount) VALUES ({p},{p},{p}) "
            f"ON CONFLICT (chat_id,category) DO UPDATE SET amount={p}",
            (chat_id, cat, amount, amount)
        )
    else:
        _execute(
            f"INSERT OR REPLACE INTO budgets (chat_id,category,amount) VALUES ({p},{p},{p})",
            (chat_id, cat, amount)
        )

# ── RECURRING FUNCTIONS ───────────────────────────────────────────────────────

def get_recurring(chat_id):
    p = ph()
    rows = _execute(
        f"SELECT id,chat_id,amount,description,category,day_of_month "
        f"FROM recurring WHERE chat_id={p}",
        (chat_id,), fetch="all"
    )
    return rows or []

def add_recurring(chat_id, amount, desc, cat, day):
    r_id = int(datetime.now().timestamp() * 1000)
    p = ph()
    _execute(
        f"INSERT INTO recurring (id,chat_id,amount,description,category,day_of_month) "
        f"VALUES ({p},{p},{p},{p},{p},{p})",
        (r_id, chat_id, amount, desc, cat, day)
    )

def del_recurring(r_id, chat_id):
    p = ph()
    _execute(f"DELETE FROM recurring WHERE id={p} AND chat_id={p}", (r_id, chat_id))

# ── LEARNED MAPPINGS ──────────────────────────────────────────────────────────

def get_learned_mappings(chat_id):
    """Return {keyword: category} dict for this chat, most-used first."""
    p = ph()
    rows = _execute(
        f"SELECT keyword, category FROM learned_mappings "
        f"WHERE chat_id={p} ORDER BY count DESC",
        (chat_id,), fetch="all"
    )
    return {r[0]: r[1] for r in rows} if rows else {}

def save_learned_mapping(chat_id, desc, category):
    """Upsert a confirmed description→category mapping for future guessing."""
    keyword = desc.lower().strip()
    if not keyword:
        return
    p = ph()
    if _DB_TYPE == "pg":
        _execute(
            f"INSERT INTO learned_mappings (chat_id,keyword,category,count) "
            f"VALUES ({p},{p},{p},1) "
            f"ON CONFLICT (chat_id,keyword) "
            f"DO UPDATE SET category={p}, count=learned_mappings.count+1",
            (chat_id, keyword, category, category)
        )
    else:
        _execute(
            f"INSERT INTO learned_mappings (chat_id,keyword,category,count) "
            f"VALUES ({p},{p},{p},1) "
            f"ON CONFLICT(chat_id,keyword) "
            f"DO UPDATE SET category={p}, count=count+1",
            (chat_id, keyword, category, category)
        )


# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
_gs_client = None
_gs_sheet  = None

SHEET_TX     = "Transactions"
SHEET_BUDGET = "Budget Tracker"
SHEET_SUM    = "Summary"
SHEET_DASH   = "Dashboard"

BUD_HEADERS = ["Category","Budget","Spent","Remaining","Progress","% Used","Status"]
TX_HEADERS  = ["ID","Date","Month","Type","Amount","Category","Description","Added By"]
SUM_HEADERS = ["Category","Jan Spent","Feb Spent","Mar Spent","3-Month Avg","Trend"]

_GREEN  = {"red":0.851,"green":0.957,"blue":0.859}
_AMBER  = {"red":0.996,"green":0.957,"blue":0.875}
_RED    = {"red":0.988,"green":0.910,"blue":0.902}
_HEADER = {"red":0.235,"green":0.522,"blue":0.282}
_GREY   = {"red":0.902,"green":0.902,"blue":0.902}
_WHITE  = {"red":1.0,  "green":1.0,  "blue":1.0}

def _sc(pct):
    if pct>=100: return _RED
    if pct>=80:  return _AMBER
    return _GREEN

def _st(pct):
    if pct>=100: return "Over budget"
    if pct>=80:  return "Warning"
    return "OK"

def _bar(pct, w=10):
    f = max(0, min(round(pct/100*w), w))
    return "█"*f + "░"*(w-f)

def _cv(v):
    if isinstance(v,(int,float)): return {"userEnteredValue":{"numberValue":v}}
    return {"userEnteredValue":{"stringValue":str(v)}}

def _get_gs():
    global _gs_client, _gs_sheet
    if _gs_sheet is not None: return _gs_client, _gs_sheet
    if not GOOGLE_SHEETS_CREDS or not GOOGLE_SHEET_ID: return None, None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_SHEETS_CREDS),
            scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"])
        _gs_client = gspread.authorize(creds)
        _gs_sheet  = _gs_client.open_by_key(GOOGLE_SHEET_ID)
        _ensure_sheets(_gs_sheet)
        print("Google Sheets connected OK.")
    except Exception as e:
        print(f"Google Sheets init error: {e}")
        _gs_client = _gs_sheet = None
    return _gs_client, _gs_sheet

def _get_or_create_ws(spreadsheet, title, headers):
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=max(len(headers),10))
        if headers: ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws

def _ensure_sheets(spreadsheet):
    for title, headers in [(SHEET_DASH,[]),(SHEET_TX,TX_HEADERS),(SHEET_BUDGET,BUD_HEADERS),(SHEET_SUM,SUM_HEADERS)]:
        _get_or_create_ws(spreadsheet, title, headers)
    try: spreadsheet.del_worksheet(spreadsheet.worksheet("Sheet1"))
    except Exception: pass


def gs_refresh_budget(spreadsheet, chat_id, month):
    budgets      = get_budgets(chat_id)
    _, _, by_cat = calc_summary(chat_id, month)
    ws           = _get_or_create_ws(spreadsheet, SHEET_BUDGET, BUD_HEADERS)
    sid          = ws.id
    n = len(CATS)

    data_rows  = []
    row_colors = []
    for cat in CATS:
        bud   = budgets.get(cat, 0)
        spent = round(by_cat.get(cat, 0), 2)
        rem   = round(bud-spent, 2) if bud else ""
        pct   = round(spent/bud*100, 1) if bud else 0
        bar   = _bar(pct) if bud else ""
        status = _st(pct) if bud else "—"
        data_rows.append([cat, bud or 0, spent, rem, bar, pct if bud else "", status])
        row_colors.append(_sc(pct) if bud else _WHITE)

    total_bud   = sum(budgets.get(c,0) for c in CATS)
    total_spent = round(sum(by_cat.get(c,0) for c in CATS), 2)
    total_rem   = round(total_bud-total_spent, 2)
    total_pct   = round(total_spent/total_bud*100,1) if total_bud else 0
    total_row   = ["TOTAL", total_bud, total_spent, total_rem, _bar(total_pct), total_pct if total_bud else "", _st(total_pct) if total_bud else "—"]
    spacer_row  = [""]*7

    all_rows = [BUD_HEADERS] + data_rows + [spacer_row] + [total_row]
    cell_data = [{"values":[_cv(v) for v in r]} for r in all_rows]

    reqs = [
        {"updateCells":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":500,"startColumnIndex":0,"endColumnIndex":7},"fields":"userEnteredValue,userEnteredFormat"}},
        {"updateCells":{"rows":cell_data,"fields":"userEnteredValue","start":{"sheetId":sid,"rowIndex":0,"columnIndex":0}}},
        {"repeatCell":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":0,"endColumnIndex":7},"cell":{"userEnteredFormat":{"backgroundColor":_HEADER,"textFormat":{"bold":True,"foregroundColor":_WHITE,"fontSize":10},"horizontalAlignment":"CENTER"}},"fields":"userEnteredFormat"}},
        {"repeatCell":{"range":{"sheetId":sid,"startRowIndex":n+2,"endRowIndex":n+3,"startColumnIndex":0,"endColumnIndex":7},"cell":{"userEnteredFormat":{"backgroundColor":_GREY,"textFormat":{"bold":True}}},"fields":"userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold"}},
        {"updateSheetProperties":{"properties":{"sheetId":sid,"gridProperties":{"frozenRowCount":1}},"fields":"gridProperties.frozenRowCount"}},
        {"autoResizeDimensions":{"dimensions":{"sheetId":sid,"dimension":"COLUMNS","startIndex":0,"endIndex":7}}},
    ]
    for col in [1,2,3]:
        reqs.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":1,"endRowIndex":n+3,"startColumnIndex":col,"endColumnIndex":col+1},"cell":{"userEnteredFormat":{"numberFormat":{"type":"CURRENCY","pattern":"\"$\"#,##0.00"}}},"fields":"userEnteredFormat.numberFormat"}})
    reqs.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":1,"endRowIndex":n+3,"startColumnIndex":5,"endColumnIndex":6},"cell":{"userEnteredFormat":{"numberFormat":{"type":"NUMBER","pattern":"0.0\"%\""}}},"fields":"userEnteredFormat.numberFormat"}})
    for i, color in enumerate(row_colors):
        reqs.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":i+1,"endRowIndex":i+2,"startColumnIndex":0,"endColumnIndex":7},"cell":{"userEnteredFormat":{"backgroundColor":color}},"fields":"userEnteredFormat.backgroundColor"}})
    spreadsheet.batch_update({"requests":reqs})


def gs_refresh_summary(spreadsheet, chat_id):
    month   = get_month()
    dt      = datetime.strptime(month, "%Y-%m")
    months  = [(dt-timedelta(days=30*i)).strftime("%Y-%m") for i in range(2,-1,-1)]
    ws = _get_or_create_ws(spreadsheet, SHEET_SUM, SUM_HEADERS)
    ws.clear()
    month_labels = [datetime.strptime(m,"%Y-%m").strftime("%b %Y") for m in months]
    headers = ["Category"] + [f"{l} Spent" for l in month_labels] + ["3-Month Avg","Trend"]
    ws.append_row(headers, value_input_option="USER_ENTERED")
    rows = []
    for cat in CATS:
        spends = [round(calc_summary(chat_id,m)[2].get(cat,0),2) for m in months]
        avg    = round(sum(spends)/3, 2)
        if   spends[-1]>spends[0]: trend="\u25b2 Increasing"
        elif spends[-1]<spends[0]: trend="\u25bc Decreasing"
        elif all(s==0 for s in spends): trend="\u2014"
        else: trend="\u2192 Stable"
        rows.append([cat]+spends+[avg,trend])
    if rows: ws.append_rows(rows, value_input_option="USER_ENTERED")

def gs_refresh_dashboard(spreadsheet, chat_id):
    month        = get_month()
    budgets      = get_budgets(chat_id)
    income, spent, by_cat = calc_summary(chat_id, month)
    last_month   = (datetime.now().replace(day=1)-timedelta(days=1)).strftime("%Y-%m")
    _, last_spent, _ = calc_summary(chat_id, last_month)
    total_budget = sum(budgets.values())
    remaining    = income-spent
    savings_rate = round(remaining/income*100,1) if income>0 else 0
    budget_pct   = round(spent/total_budget*100,1) if total_budget else 0
    mom_diff     = round(spent-last_spent,2)
    mom_arrow    = "\u25b2" if mom_diff>0 else "\u25bc"
    updated_at   = datetime.now().strftime("%d %b %Y %H:%M")
    ws = _get_or_create_ws(spreadsheet, SHEET_DASH, [])
    sid = ws.id
    rows = [
        [f"Family Budget Dashboard \u2014 {month_label(month)}","","","","",f"Updated: {updated_at}"],
        [""],
        ["MONTHLY OVERVIEW","","","","",""],
        ["Income",f"${income:,.2f}","Spent",f"${spent:,.2f}","Remaining",f"${remaining:,.2f}"],
        ["Savings rate",f"{savings_rate}%","Budget used",f"{budget_pct}%","vs Last Month",f"{mom_arrow} ${abs(mom_diff):,.2f}"],
        [""],["TOP 3 SPENDING","","","","",""],
    ]
    for cat, amt in sorted(by_cat.items(), key=lambda x:-x[1])[:3]:
        rows.append([f"{CAT_ICONS.get(cat,chr(8226))} {cat}",f"${amt:,.2f}","","","",""])
    cell_rows = [{"values":[_cv(v) for v in r]} for r in rows]
    spreadsheet.batch_update({"requests":[
        {"updateCells":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":200,"startColumnIndex":0,"endColumnIndex":8},"fields":"userEnteredValue,userEnteredFormat"}},
        {"updateCells":{"rows":cell_rows,"fields":"userEnteredValue","start":{"sheetId":sid,"rowIndex":0,"columnIndex":0}}},
        {"repeatCell":{"range":{"sheetId":sid,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":0,"endColumnIndex":6},"cell":{"userEnteredFormat":{"backgroundColor":_HEADER,"textFormat":{"bold":True,"foregroundColor":_WHITE,"fontSize":12}}},"fields":"userEnteredFormat"}},
        {"autoResizeDimensions":{"dimensions":{"sheetId":sid,"dimension":"COLUMNS","startIndex":0,"endIndex":6}}},
    ]})

def gs_append_tx(tx_id, date, month, type_, amount, cat, desc, added_by):
    _, spreadsheet = _get_gs()
    if not spreadsheet: return
    try:
        ws = _get_or_create_ws(spreadsheet, SHEET_TX, TX_HEADERS)
        ws.append_row([str(tx_id),date,month,type_,amount,cat,desc,added_by], value_input_option="USER_ENTERED")
    except Exception as e: print(f"gs_append_tx error: {e}")

def gs_delete_tx(tx_id):
    _, spreadsheet = _get_gs()
    if not spreadsheet: return
    try:
        ws = _get_or_create_ws(spreadsheet, SHEET_TX, TX_HEADERS)
        cell = ws.find(str(tx_id), in_column=1)
        if cell: ws.delete_rows(cell.row)
    except Exception as e: print(f"gs_delete_tx error: {e}")

def gs_update_tx_category(tx_id, new_cat):
    _, spreadsheet = _get_gs()
    if not spreadsheet: return
    try:
        ws = _get_or_create_ws(spreadsheet, SHEET_TX, TX_HEADERS)
        cell = ws.find(str(tx_id), in_column=1)
        if cell: ws.update_cell(cell.row, 6, new_cat)
    except Exception as e: print(f"gs_update_tx_category error: {e}")

def gs_refresh_all(chat_id):
    _, spreadsheet = _get_gs()
    if not spreadsheet: return
    month = get_month()
    for label, fn, args in [
        ("Budget Tracker", gs_refresh_budget,   (spreadsheet, chat_id, month)),
        ("Summary",        gs_refresh_summary,  (spreadsheet, chat_id)),
        ("Dashboard",      gs_refresh_dashboard,(spreadsheet, chat_id)),
    ]:
        try: fn(*args); print(f"{label} OK")
        except Exception as e: print(f"{label} error: {e}")
        time.sleep(3)

async def gs_sync_async(chat_id, tx_id=None, date=None, month=None, type_=None, amount=None, cat=None, desc=None, added_by=None, delete=False, update_category=False, bot=None):
    """Sync to Google Sheets. Pass bot= to enable failure notifications to the user."""
    loop = asyncio.get_event_loop()
    try:
        if delete and tx_id:
            await loop.run_in_executor(None, gs_delete_tx, tx_id)
        elif update_category and tx_id and cat:
            await loop.run_in_executor(None, gs_update_tx_category, tx_id, cat)
        elif tx_id:
            await loop.run_in_executor(None, gs_append_tx, tx_id, date, month, type_, amount, cat, desc, added_by)
        await loop.run_in_executor(None, gs_refresh_all, chat_id)
    except Exception as e:
        print(f"gs_sync_async error for {chat_id}: {e}")
        if bot:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ *Sheets sync failed* — your transaction is saved in the database but the Google Sheet may be out of date. Try /refreshsheets later.",
                    parse_mode="Markdown"
                )
            except Exception: pass


def fmt(n): return f"${n:,.2f}"
def get_month(): return datetime.now().strftime("%Y-%m")
def month_label(m=None):
    m = m or get_month()
    return datetime.strptime(m,"%Y-%m").strftime("%B %Y")

def calc_summary(chat_id, month=None):
    txs    = get_txs(chat_id, month)
    income = sum(t[3] for t in txs if t[2]=="income")
    spent  = sum(t[3] for t in txs if t[2]=="expense")
    by_cat = {}
    for t in txs:
        if t[2]=="expense": by_cat[t[5]] = by_cat.get(t[5],0)+t[3]
    return income, spent, by_cat

def progress_bar(pct, width=10):
    filled = max(0, min(round(pct/100*width), width))
    return "█"*filled + "░"*(width-filled)

def budget_status(pct):
    if pct>=100: return "🔴"
    if pct>=80:  return "🟡"
    return "🟢"

def get_grade(pct_used):
    if pct_used<=0.7:  return "A","🌟 Excellent!"
    if pct_used<=0.85: return "B","👍 Good job!"
    if pct_used<=0.95: return "C","😐 Watch your spending"
    if pct_used<=1.0:  return "D","⚠️ Almost over budget!"
    return "F","🔴 Over budget!"

def main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Summary"),  KeyboardButton("📋 Spent")],
        [KeyboardButton("🏆 Report"),   KeyboardButton("🧾 Recent")],
        [KeyboardButton("👫 Who"),      KeyboardButton("💰 Budgets")],
        [KeyboardButton("🔁 Recurring"),KeyboardButton("🗑 Delete")],
        [KeyboardButton("❓ Help"),     KeyboardButton("🔄 Sheets")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, persistent=True)


# ── IMPROVED CATEGORY GUESSING ────────────────────────────────────────────────

def _is_income_desc(desc: str) -> bool:
    """Return True if the description sounds like income rather than an expense."""
    tl = desc.lower()
    return any(kw in tl for kw in INCOME_KEYWORDS)

def _score_categories(desc: str) -> dict:
    """
    Return a score dict {category: score} using keyword matching + fuzzy matching.
    High-confidence keywords score 3; low-confidence (generic) keywords score 1;
    fuzzy matches score 1. Higher total = stronger match.
    """
    tl = desc.lower()
    words = re.findall(r'\w+', tl)
    scores = {cat: 0 for cat in CATS}

    for cat, kws in CAT_KEYWORDS.items():
        for kw in kws:
            # Exact substring match — high confidence
            if kw in tl:
                scores[cat] += 3
                continue
            # Fuzzy word-level match — catches typos like "resturant"
            for word in words:
                ratio = difflib.SequenceMatcher(None, word, kw).ratio()
                if ratio >= 0.82:          # ~1-2 character difference
                    scores[cat] += 2

    # Low-weight generic keywords (e.g. "food" could be Groceries OR Dining Out)
    for cat, kws in CAT_KEYWORDS_LOW.items():
        for kw in kws:
            if kw in tl:
                scores[cat] += 1

    return scores

def guess_category(desc: str, learned: dict | None = None) -> str | None:
    """
    Improved category guesser. Priority order:
      1. Exact learned mapping (user-confirmed before)
      2. Fuzzy learned mapping (close match to a learned key)
      3. Multi-score keyword matching
    Returns None if no confident guess.
    """
    tl = desc.lower().strip()

    # 1. Exact learned mapping
    if learned:
        if tl in learned:
            return learned[tl]

        # 2. Fuzzy match against learned keys
        close = difflib.get_close_matches(tl, learned.keys(), n=1, cutoff=0.80)
        if close:
            return learned[close[0]]

    # 3. Multi-category keyword scoring
    scores = _score_categories(desc)
    best_cat  = max(scores, key=scores.get)
    best_score = scores[best_cat]

    if best_score == 0:
        return None  # No match at all → ask the user

    # If the best score is low and there's a tie, don't guess — ask instead
    top_two = sorted(scores.values(), reverse=True)[:2]
    if best_score < 2 and len(top_two) > 1 and top_two[0] == top_two[1]:
        return None

    return best_cat

async def claude_categorise(desc: str) -> str | None:
    """
    Ask Claude to categorise a description when keyword matching fails.
    Returns a category string or None on error.
    Falls back gracefully — never blocks the user flow.
    """
    if not ANTHROPIC_API_KEY:
        return None
    valid_cats = ", ".join(CATS)
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 50,
        "messages": [{
            "role": "user",
            "content": (
                f"Categorise this personal finance expense into exactly one of these categories: {valid_cats}.\n"
                f"Expense description: \"{desc}\"\n"
                f"Reply with ONLY the category name, nothing else."
            )
        }]
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json=payload
            )
            if r.status_code != 200:
                return None
            text = r.json()["content"][0]["text"].strip()
            # Validate the response is actually one of our categories
            for cat in CATS:
                if cat.lower() == text.lower():
                    return cat
            # Partial match fallback
            for cat in CATS:
                if cat.lower() in text.lower() or text.lower() in cat.lower():
                    return cat
            return None
    except Exception as e:
        print(f"Claude categorise error: {e}")
        return None


# ── SMART ADD ────────────────────────────────────────────────────────────────

async def smart_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Support "income 5000 salary" prefix
    income_prefix = re.match(r'^income\s+(\d+\.?\d*)\s*(.*)$', text, re.IGNORECASE)
    if income_prefix:
        try: amount = float(income_prefix.group(1))
        except: return
        desc = income_prefix.group(2).strip() or "Income"
        chat_id = str(update.effective_chat.id)
        added_by = update.effective_user.first_name or "Someone"
        date = datetime.now().strftime("%Y-%m-%d")
        tx_info = add_tx(chat_id, "income", amount, desc, "Income", date, added_by)
        asyncio.create_task(gs_sync_async(chat_id, *tx_info))
        await update.message.reply_text(
            f"✅ *Income logged!*\n\n💵 *{fmt(amount)}* — {desc}\n👤 {added_by}",
            parse_mode="Markdown"
        )
        return

    # Standard "amount description" pattern
    match = re.match(r'^(\d+\.?\d*)\s+(.+)$', text)
    if not match:
        return
    try:
        amount = float(match.group(1))
        desc   = match.group(2).strip()
    except:
        return

    chat_id  = str(update.effective_chat.id)
    added_by = update.effective_user.first_name or "Someone"
    desc = desc[:100]  # cap at 100 chars — Telegram UI truncates beyond this

    # Detect income from description keywords
    if _is_income_desc(desc):
        date = datetime.now().strftime("%Y-%m-%d")
        tx_info = add_tx(chat_id, "income", amount, desc, "Income", date, added_by)
        asyncio.create_task(gs_sync_async(chat_id, *tx_info))
        await update.message.reply_text(
            f"✅ *Income logged!* 💵\n\n*{fmt(amount)}* — {desc}\n👤 {added_by}\n\n"
            f"_Was this actually an expense? Use /delete then /add_",
            parse_mode="Markdown"
        )
        return

    # Load learned mappings for this chat
    learned = get_learned_mappings(chat_id)

    # Try local guessing first (fast, free)
    guessed_cat = guess_category(desc, learned)

    # Fall back to Claude if no confident local guess
    used_claude = False
    if guessed_cat is None and ANTHROPIC_API_KEY:
        guessed_cat = await claude_categorise(desc)
        used_claude = True

    if guessed_cat:
        date    = datetime.now().strftime("%Y-%m-%d")
        tx_info = add_tx(chat_id, "expense", amount, desc, guessed_cat, date, added_by)
        asyncio.create_task(gs_sync_async(chat_id, *tx_info))
        tx_id = tx_info[0]
        icon  = CAT_ICONS.get(guessed_cat, "•")
        ai_tag = " 🤖" if used_claude else ""

        keyboard = []; row = []
        for cat in CATS:
            row.append(InlineKeyboardButton(
                f"{CAT_ICONS.get(cat, chr(8226))} {cat.split('/')[0]}",
                callback_data=f"quickcat|id|{tx_id}|{cat}"
            ))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)

        await update.message.reply_text(
            f"✅ *Logged!* _{desc}_\n{icon} {guessed_cat}{ai_tag} · {fmt(amount)}\n_Tap to change category_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        await check_budget_alert(update, ctx, chat_id, guessed_cat)
    else:
        # No guess — show category picker without logging yet
        safe_desc = desc[:30].replace("|", "-")  # | is our separator
        keyboard = []; row = []
        for cat in CATS:
            row.append(InlineKeyboardButton(
                f"{CAT_ICONS.get(cat, chr(8226))} {cat.split('/')[0]}",
                callback_data=f"quickcat|new|{amount}|{safe_desc}|{cat}"
            ))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
        await update.message.reply_text(
            f"💸 *{fmt(amount)}* — _{desc}_\nWhich category?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def quickcat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Callback data format (| separator — safe against underscores in descriptions):
      Already-logged tx:  quickcat|id|<tx_id>|<category>
      Unknown category:   quickcat|new|<amount>|<desc>|<category>
    """
    query = update.callback_query; await query.answer()
    chat_id = str(update.effective_chat.id)
    parts = query.data.split("|")   # ["quickcat", mode, ...]

    mode = parts[1] if len(parts) > 1 else ""

    if mode == "id":
        # Already logged — just update the category
        try: tx_id = int(parts[2])
        except: await query.edit_message_text("⚠️ Invalid callback data."); return
        updated_cat = parts[3] if len(parts) > 3 else ""
        if updated_cat not in CATS:
            await query.edit_message_text("⚠️ Unknown category."); return

        tx = get_tx(tx_id, chat_id)
        if not tx:
            await query.edit_message_text("⚠️ Couldn't find that transaction.", parse_mode="Markdown")
            return
        update_tx_category(tx_id, chat_id, updated_cat)
        asyncio.create_task(gs_sync_async(chat_id, tx_id=tx_id, cat=updated_cat, update_category=True))
        icon = CAT_ICONS.get(updated_cat, "•")

        # ── Learn from this correction ────────────────────────────────────────
        save_learned_mapping(chat_id, tx[4], updated_cat)   # tx[4] = description
        # ─────────────────────────────────────────────────────────────────────

        await query.edit_message_text(
            f"✅ *Updated!*\n{icon} *{updated_cat}*\n💸 {fmt(tx[3])} — {tx[4]}\n👤 {tx[7]}",
            parse_mode="Markdown"
        )
        await check_budget_alert_query(query, ctx, chat_id, updated_cat)
        return

    # mode == "new" — logging a transaction whose category was unknown
    try: amount = float(parts[2])
    except: await query.edit_message_text("⚠️ Invalid amount."); return
    desc = parts[3] if len(parts) > 3 else ""
    cat  = parts[4] if len(parts) > 4 else ""
    if cat not in CATS:
        await query.edit_message_text("⚠️ Unknown category."); return

    for t in get_txs(chat_id):
        if t[3] == amount and t[4] == desc:
            del_tx(t[0], chat_id)
            asyncio.create_task(gs_sync_async(chat_id, tx_id=t[0], delete=True))
            break

    date     = datetime.now().strftime("%Y-%m-%d")
    added_by = update.effective_user.first_name or "Someone"
    tx_info  = add_tx(chat_id, "expense", amount, desc, cat, date, added_by)
    asyncio.create_task(gs_sync_async(chat_id, *tx_info))

    # ── Learn from explicit user selection ───────────────────────────────────
    save_learned_mapping(chat_id, desc, cat)
    # ─────────────────────────────────────────────────────────────────────────

    icon = CAT_ICONS.get(cat, "•")
    await query.edit_message_text(
        f"✅ *Logged!*\n{icon} *{cat}*\n💸 {fmt(amount)} — {desc}\n👤 {added_by}",
        parse_mode="Markdown"
    )
    await check_budget_alert_query(query, ctx, chat_id, cat)


async def check_budget_alert(update, ctx, chat_id, cat):
    budgets = get_budgets(chat_id); bud = budgets.get(cat, 0)
    if not bud: return
    _, _, by_cat = calc_summary(chat_id); spent = by_cat.get(cat, 0); pct = spent / bud
    if 0.8 <= pct < 0.82:
        await update.message.reply_text(
            f"⚠️ *Budget Alert!*\n\n{CAT_ICONS.get(cat, chr(8226))} {cat} is at *{pct*100:.0f}%* of your {fmt(bud)} budget.\n_{fmt(bud-spent)} remaining_",
            parse_mode="Markdown"
        )

async def check_budget_alert_query(query, ctx, chat_id, cat):
    budgets = get_budgets(chat_id); bud = budgets.get(cat, 0)
    if not bud: return
    _, _, by_cat = calc_summary(chat_id); spent = by_cat.get(cat, 0); pct = spent / bud
    if 0.8 <= pct < 0.82:
        await query.message.reply_text(
            f"⚠️ *Budget Alert!*\n\n{CAT_ICONS.get(cat, chr(8226))} {cat} is at *{pct*100:.0f}%* of your {fmt(bud)} budget.\n_{fmt(bud-spent)} remaining_",
            parse_mode="Markdown"
        )


async def scan_receipt_with_claude(image_bytes: bytes) -> dict:
    if not ANTHROPIC_API_KEY: return {"error": "ANTHROPIC_API_KEY not configured"}
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    if image_bytes[:4]==b'\x89PNG': mt="image/png"
    elif image_bytes[:2]==b'\xff\xd8': mt="image/jpeg"
    elif image_bytes[:4]==b'RIFF': mt="image/webp"
    else: mt="image/jpeg"
    valid_cats = ", ".join(CATS)
    payload = {"model":"claude-sonnet-4-6","max_tokens":1024,"messages":[{"role":"user","content":[
        {"type":"image","source":{"type":"base64","media_type":mt,"data":image_b64}},
        {"type":"text","text":f"""Analyze this receipt. Reply ONLY with JSON:
{{"merchant":"name","total":12.34,"date":"YYYY-MM-DD or null","category":"one of: {valid_cats}","items":["item $x"],"confidence":"high/medium/low"}}
If unreadable: {{"error":"Cannot read receipt clearly"}}"""}
    ]}]}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json=payload)
        if r.status_code!=200: return {"error":f"API error {r.status_code}"}
        text = r.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        return json.loads(text)

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); added_by=update.effective_user.first_name or "Someone"
    if not ANTHROPIC_API_KEY:
        await update.message.reply_text("⚠️ Receipt scanning not configured."); return
    scanning_msg = await update.message.reply_text("📸 Scanning receipt... ⏳")
    try:
        photo=update.message.photo[-1]; pf=await ctx.bot.get_file(photo.file_id)
        image_bytes=bytes(await pf.download_as_bytearray())
        if len(image_bytes)<1000: raise Exception("Image too small")
        result=await scan_receipt_with_claude(image_bytes)
        if "error" in result:
            await scanning_msg.edit_text(f"😕 *Couldn't read receipt*\n_{result['error']}_\n\nTry: `amount description`",parse_mode="Markdown"); return
        merchant=result.get("merchant","Unknown"); total=float(result.get("total",0))
        category=result.get("category","Personal/Shopping"); items=result.get("items",[])
        date=result.get("date") or datetime.now().strftime("%Y-%m-%d"); confidence=result.get("confidence","medium")
        if total<=0:
            await scanning_msg.edit_text("😕 Couldn't find a total. Try clearer photo!"); return
        tx_info=add_tx(chat_id,"expense",total,merchant,category,date,added_by)
        asyncio.create_task(gs_sync_async(chat_id,*tx_info)); tx_id=tx_info[0]
        icon=CAT_ICONS.get(category,"•"); ce="🟢" if confidence=="high" else "🟡" if confidence=="medium" else "🔴"
        items_text=("\n"+"\n".join(f"  • {i}" for i in items[:5])) if items else ""
        keyboard=[]; row=[]
        for cat in CATS:
            row.append(InlineKeyboardButton(f"{CAT_ICONS.get(cat,chr(8226))} {cat.split('/')[0]}",callback_data=f"quickcat|id|{tx_id}|{cat}"))
            if len(row)==2: keyboard.append(row); row=[]
        if row: keyboard.append(row)
        await scanning_msg.edit_text(
            f"🧾 *Receipt Scanned!* {ce}\n{'-'*24}\n{icon} *{merchant}*{items_text}\n\n💸 Total: *{fmt(total)}*\n📁 Category: *{category}*\n👤 {added_by}\n\n_Tap to change category:_",
            parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(keyboard))
        await check_budget_alert(update,ctx,chat_id,category)
    except Exception as e:
        print(f"Receipt scan error: {e}")
        await scanning_msg.edit_text("😕 Something went wrong. Try: `amount description`",parse_mode="Markdown")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to Yong's Budget Bot!*\n\nTrack your family spending together 💑\n\n"
        "*✨ Quick tip:* Just type amount + description:\ne.g. `45.50 dinner at nobu` or `120 grab`\n\n"
        "*Log income:* `income 5000 salary` or `5000 salary`\n\n"
        "*Commands:*\n"
        "➕ `/add 45.50 dining dinner` — log expense\n"
        "➕ `/income 5000 paycheck` — log income\n"
        "📊 `/summary` — monthly overview\n"
        "📋 `/spent` — category breakdown\n"
        "🏆 `/report` — monthly report card\n"
        "🧾 `/recent` — last 15 transactions\n"
        "🔁 `/recurring` — manage recurring\n"
        "💰 `/budget groceries 400` — set budget\n"
        "📅 `/month 2026-02` — view past month\n"
        "📅 `/daily` — today's spending\n"
        "🗑 `/delete` — delete a transaction\n"
        "🔄 `/refreshsheets` — rebuild Google Sheets\n"
        "❓ `/help` — all commands",
        parse_mode="Markdown", reply_markup=main_keyboard())

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *All Commands*\n\n"
        "*Smart add:* `45.50 dinner at nobu`\n"
        "*Smart income:* `income 5000 salary` or `5000 paycheck`\n\n"
        "*Logging:*\n`/add <amount> <category> <desc>`\n`/income <amount> <desc>`\n\n"
        "*Viewing:*\n`/summary` · `/spent` · `/report` · `/recent` · `/daily` · `/month YYYY-MM`\n\n"
        "*Budgets:*\n`/budget <category> <amount>` · `/budgets`\n\n"
        "*Recurring:*\n`/recurring` — view/add/remove\n\n"
        "*Managing:*\n`/delete` · `/refreshsheets`",
        parse_mode="Markdown")

async def add_expense(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); args=ctx.args
    if len(args)<2: await update.message.reply_text("❌ Usage: `/add <amount> <category> <description>`",parse_mode="Markdown"); return
    try: amount=float(args[0].replace("$","").replace(",",""))
    except ValueError: await update.message.reply_text("❌ Invalid amount.",parse_mode="Markdown"); return
    cat_input=args[1].lower()
    matched_cat=next((c for c in CATS if cat_input in c.lower() or c.lower().startswith(cat_input)),"Personal/Shopping")
    desc=" ".join(args[2:]) if len(args)>2 else matched_cat
    date=datetime.now().strftime("%Y-%m-%d"); added_by=update.effective_user.first_name or "Someone"
    tx_info=add_tx(chat_id,"expense",amount,desc,matched_cat,date,added_by)
    asyncio.create_task(gs_sync_async(chat_id,*tx_info))
    icon=CAT_ICONS.get(matched_cat,"•"); budgets=get_budgets(chat_id)
    _,_,by_cat=calc_summary(chat_id); cat_budget=budgets.get(matched_cat,0)
    budget_line=""
    if cat_budget:
        pct=min((by_cat.get(matched_cat,0)/cat_budget)*100,100)
        budget_line=f"\n{budget_status(pct)} `{progress_bar(pct,8)}` {pct:.0f}% of {fmt(cat_budget)}"
    await update.message.reply_text(f"✅ *Expense logged!*\n\n{icon} *{matched_cat}*\n💸 {fmt(amount)} — {desc}\n👤 {added_by}{budget_line}",parse_mode="Markdown")
    await check_budget_alert(update,ctx,chat_id,matched_cat)

async def add_income(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); args=ctx.args
    if not args: await update.message.reply_text("❌ Usage: `/income <amount> <description>`",parse_mode="Markdown"); return
    try: amount=float(args[0].replace("$","").replace(",",""))
    except ValueError: await update.message.reply_text("❌ Invalid amount.",parse_mode="Markdown"); return
    desc=" ".join(args[1:]) if len(args)>1 else "Income"
    date=datetime.now().strftime("%Y-%m-%d"); added_by=update.effective_user.first_name or "Someone"
    tx_info=add_tx(chat_id,"income",amount,desc,"Income",date,added_by)
    asyncio.create_task(gs_sync_async(chat_id,*tx_info))
    await update.message.reply_text(f"✅ *Income logged!*\n\n💵 *{fmt(amount)}* — {desc}\n👤 {added_by}",parse_mode="Markdown")

async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE, month=None):
    chat_id=str(update.effective_chat.id); month=month or get_month()
    income,spent,by_cat=calc_summary(chat_id,month)
    remaining=income-spent; budgets=get_budgets(chat_id)
    total_budget=sum(budgets.values()); pct=(spent/total_budget*100) if total_budget else 0
    savings_rate=(remaining/income*100) if income>0 else 0; rem_icon="✅" if remaining>=0 else "🔴"
    text=(f"📊 *{month_label(month)} Summary*\n{'-'*28}\n"
          f"💵 Income:     *{fmt(income)}*\n💸 Spent:      *{fmt(spent)}*\n"
          f"{rem_icon} Remaining: *{fmt(abs(remaining))}*{'  left' if remaining>=0 else '  over!'}\n")
    if income>0: text+=f"📈 Savings rate: *{savings_rate:.0f}%*\n"
    if total_budget: text+=f"\n{budget_status(pct)} `{progress_bar(pct,12)}` {pct:.0f}% of {fmt(total_budget)} budget\n"
    if by_cat:
        text+=f"\n*Top categories:*\n{'-'*28}\n"
        for cat,amt in sorted(by_cat.items(),key=lambda x:-x[1])[:5]:
            bud=budgets.get(cat,0); status=budget_status((amt/bud*100) if bud else 0) if bud else "  "
            bud_str=f" / {fmt(bud)}" if bud else ""
            text+=f"{CAT_ICONS.get(cat,chr(8226))} {cat}: *{fmt(amt)}*{bud_str} {status}\n"
    await update.message.reply_text(text,parse_mode="Markdown")

async def report_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); month=get_month()
    income,spent,by_cat=calc_summary(chat_id,month); budgets=get_budgets(chat_id); total_budget=sum(budgets.values())
    last_month=(datetime.now().replace(day=1)-timedelta(days=1)).strftime("%Y-%m")
    _,last_spent,_=calc_summary(chat_id,last_month); diff=spent-last_spent
    diff_str=f"{chr(128200)+' +' if diff>0 else chr(128201)+' ' }{fmt(abs(diff))} vs last month"
    pct_used=(spent/total_budget) if total_budget else 0; grade,grade_msg=get_grade(pct_used)
    savings_rate=((income-spent)/income*100) if income>0 else 0
    text=(f"🏆 *{month_label()} Report Card*\n{'-'*28}\nGrade: *{grade}* — {grade_msg}\n{'-'*28}\n"
          f"💵 Income: *{fmt(income)}*\n💸 Spent: *{fmt(spent)}*\n💰 Saved: *{fmt(income-spent)}* ({savings_rate:.0f}%)\n{diff_str}\n")
    if total_budget:
        text+=f"\n*Category breakdown:*\n{'-'*28}\n"
        for cat in CATS:
            s=by_cat.get(cat,0); b=budgets.get(cat,0)
            if not s and not b: continue
            icon=CAT_ICONS.get(cat,"•")
            if b:
                pct=min((s/b)*100,100); status=budget_status(pct)
                text+=f"{icon} {cat.split('/')[0]}\n`{progress_bar(pct,6)}` {fmt(s)}/{fmt(b)} {status}\n"
            else: text+=f"{icon} {cat.split('/')[0]}: {fmt(s)}\n"
    await update.message.reply_text(text,parse_mode="Markdown")

async def spent_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); _,total_spent,by_cat=calc_summary(chat_id); budgets=get_budgets(chat_id)
    if not by_cat: await update.message.reply_text(f"No expenses yet for {month_label()}.\n\nType `45.50 dinner` to log one!",parse_mode="Markdown"); return
    text=f"📋 *Spending — {month_label()}*\n{'-'*28}\n\n"
    for cat in CATS:
        spent=by_cat.get(cat,0); bud=budgets.get(cat,0)
        if not spent and not bud: continue
        icon=CAT_ICONS.get(cat,"•")
        if bud:
            pct=min((spent/bud)*100,100); status=budget_status(pct)
            text+=f"{icon} *{cat}*\n`{progress_bar(pct,8)}` {fmt(spent)} / {fmt(bud)} {status}\n\n"
        else: text+=f"{icon} *{cat}*: {fmt(spent)}\n\n"
    text+=f"{'-'*28}\n💸 *Total: {fmt(total_spent)}*"
    await update.message.reply_text(text,parse_mode="Markdown")

async def recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); txs=get_txs(chat_id)[:15]
    if not txs: await update.message.reply_text("No transactions this month yet!\nType `45.50 dinner` to log one.",parse_mode="Markdown"); return
    text=f"🧾 *Recent Transactions*\n{'-'*28}\n\n"
    for t in txs:
        icon="💵" if t[2]=="income" else CAT_ICONS.get(t[5],"•")
        sign="+" if t[2]=="income" else "-"
        date_str=datetime.strptime(t[6],"%Y-%m-%d").strftime("%b %d")
        text+=f"{icon} *{sign}{fmt(t[3])}*  {t[4]}\n_{date_str} · {t[7]}_\n\n"
    await update.message.reply_text(text,parse_mode="Markdown")

async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); txs=get_txs(chat_id)[:15]
    if not txs: await update.message.reply_text("No transactions to delete!",parse_mode="Markdown"); return
    keyboard=[]
    for t in txs:
        icon="💵" if t[2]=="income" else CAT_ICONS.get(t[5],"•")
        sign="+" if t[2]=="income" else "-"
        date_str=datetime.strptime(t[6],"%Y-%m-%d").strftime("%b %d")
        keyboard.append([InlineKeyboardButton(f"{icon} {sign}${t[3]:.0f} {t[4][:18]} · {date_str}",callback_data=f"del_{t[0]}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel",callback_data="del_cancel")])
    await update.message.reply_text("🗑 *Which transaction to delete?*",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer(); chat_id=str(update.effective_chat.id)
    if query.data=="del_cancel": await query.edit_message_text("Cancelled."); return
    tx_id=int(query.data.replace("del_",""))
    del_tx(tx_id,chat_id); asyncio.create_task(gs_sync_async(chat_id,tx_id=tx_id,delete=True))
    await query.edit_message_text("✅ Transaction deleted.")

async def budget_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); args=ctx.args
    if len(args)<2:
        keyboard=[[InlineKeyboardButton(f"{CAT_ICONS.get(c,chr(8226))} {c.split('/')[0]}",callback_data=f"budcat_{c}")] for c in CATS]
        await update.message.reply_text("💰 *Which category to set budget for?*",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(keyboard)); return
    try: amount=float(args[-1].replace("$","").replace(",",""))
    except ValueError: await update.message.reply_text("❌ Invalid amount.",parse_mode="Markdown"); return
    cat_input=" ".join(args[:-1]).lower()
    matched_cat=next((c for c in CATS if cat_input in c.lower() or c.lower().startswith(cat_input)),None)
    if not matched_cat: await update.message.reply_text("❌ Category not found.",parse_mode="Markdown"); return
    set_budget(chat_id,matched_cat,amount)
    await update.message.reply_text(f"✅ *Budget set!*\n\n{CAT_ICONS.get(matched_cat,chr(8226))} *{matched_cat}*\n💰 {fmt(amount)}/month",parse_mode="Markdown")

async def budcat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer(); cat=query.data.replace("budcat_","")
    await query.edit_message_text(f"💰 Setting budget for *{cat}*\n\nReply with:\n`/budget {cat.lower().split('/')[0]} 500`",parse_mode="Markdown")

async def budgets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); bud=get_budgets(chat_id); _,_,by_cat=calc_summary(chat_id)
    if not bud: await update.message.reply_text("No budgets set yet!\nUse `/budget <category> <amount>`",parse_mode="Markdown"); return
    text=f"💰 *Monthly Budgets*\n{'-'*28}\n\n"
    for cat in CATS:
        if cat in bud:
            s=by_cat.get(cat,0); pct=min((s/bud[cat])*100,100) if bud[cat] else 0
            text+=f"{CAT_ICONS.get(cat,chr(8226))} *{cat}*: {fmt(bud[cat])} {budget_status(pct)}\n"
    text+=f"\n{'-'*28}\n📊 Total: *{fmt(sum(bud.values()))}/month*"
    await update.message.reply_text(text,parse_mode="Markdown")

async def recurring_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id); args=ctx.args; recurrings=get_recurring(chat_id)
    if not args:
        text=f"🔁 *Recurring Expenses*\n{'-'*28}\n\n"
        if recurrings:
            for r in recurrings: text+=f"{CAT_ICONS.get(r[4],chr(8226))} *{fmt(r[2])}* — {r[3]}\n_{r[4]} · day {r[5]} of month_\n\n"
        else: text+="_None set yet._\n\n"
        text+="Add: `/recurring add <amount> <category> <desc> <day>`\nRemove: `/recurring remove`"
        await update.message.reply_text(text,parse_mode="Markdown"); return
    if args[0]=="add":
        if len(args)<5: await update.message.reply_text("Usage: `/recurring add <amount> <category> <desc> <day>`",parse_mode="Markdown"); return
        try:
            amount=float(args[1]); day=int(args[-1]); cat_input=args[2].lower()
            matched_cat=next((c for c in CATS if cat_input in c.lower() or c.lower().startswith(cat_input)),"Personal/Shopping")
            desc=" ".join(args[3:-1])
        except: await update.message.reply_text("❌ Invalid format.",parse_mode="Markdown"); return
        add_recurring(chat_id,amount,desc,matched_cat,day)
        await update.message.reply_text(f"✅ *Recurring added!*\n\n{CAT_ICONS.get(matched_cat,chr(8226))} *{matched_cat}*\n💸 {fmt(amount)} — {desc}\n📅 Day {day} each month",parse_mode="Markdown"); return
    if args[0]=="remove":
        if not recurrings: await update.message.reply_text("Nothing to remove!",parse_mode="Markdown"); return
        keyboard=[[InlineKeyboardButton(f"{CAT_ICONS.get(r[4],chr(8226))} {fmt(r[2])} {r[3]}",callback_data=f"delrec_{r[0]}")] for r in recurrings]
        keyboard.append([InlineKeyboardButton("❌ Cancel",callback_data="del_cancel")])
        await update.message.reply_text("Which to remove?",reply_markup=InlineKeyboardMarkup(keyboard)); return

async def delrec_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer(); chat_id=str(update.effective_chat.id)
    del_recurring(int(query.data.replace("delrec_","")),chat_id)
    await query.edit_message_text("✅ Recurring expense removed.")

async def month_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args=ctx.args
    if not args: await update.message.reply_text("Usage: `/month YYYY-MM`",parse_mode="Markdown"); return
    try: datetime.strptime(args[0],"%Y-%m")
    except ValueError: await update.message.reply_text("❌ Use format YYYY-MM e.g. `2026-02`",parse_mode="Markdown"); return
    await summary(update,ctx,month=args[0])

async def daily_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _send_daily(update,str(update.effective_chat.id),datetime.now().strftime("%Y-%m-%d"),is_callback=False)

async def _send_daily(update_or_query, chat_id: str, date_str: str, is_callback: bool):
    txs=get_txs_by_date(chat_id,date_str); expenses=[t for t in txs if t[2]=="expense"]
    total_today=sum(t[3] for t in expenses); month=date_str[:7]
    _,month_spent,by_cat_month=calc_summary(chat_id,month); budgets=get_budgets(chat_id)
    total_budget=sum(budgets.values())
    try: day_num=int(date_str[8:]); daily_avg=month_spent/day_num if day_num else 0
    except: daily_avg=0
    try:
        from calendar import monthrange
        days_in_month=monthrange(int(date_str[:4]),int(date_str[5:7]))[1]
        daily_budget_share=(total_budget/days_in_month) if total_budget else 0
        budget_left_today=daily_budget_share-total_today
    except: daily_budget_share=0; budget_left_today=0
    by_cat_today={}
    for t in expenses: by_cat_today[t[5]]=by_cat_today.get(t[5],0)+t[3]
    today_dt=datetime.now().strftime("%Y-%m-%d"); date_dt=datetime.strptime(date_str,"%Y-%m-%d")
    if date_str==today_dt: date_label=f"Today · {date_dt.strftime('%a, %d %b %Y')}"
    elif date_str==(datetime.now()-timedelta(days=1)).strftime("%Y-%m-%d"): date_label=f"Yesterday · {date_dt.strftime('%a, %d %b %Y')}"
    else: date_label=date_dt.strftime("%A, %d %b %Y")
    text=f"📅 *Daily Summary — {date_label}*\n{'-'*30}\n\n"
    if not expenses: text+="🎉 *No spending today!* 👏\n\n"
    else:
        text+="*Transactions:*\n"
        for t in expenses: text+=f"{CAT_ICONS.get(t[5],chr(8226))} {fmt(t[3])}  _{t[4]}_  · `{t[5].split('/')[0]}`\n"
        text+=f"\n💸 *Total today:* {fmt(total_today)}\n"
        if daily_avg>0:
            diff=total_today-daily_avg; arrow="📈" if diff>0 else "📉"
            diff_str=f"+{fmt(abs(diff))}" if diff>0 else f"-{fmt(abs(diff))}"
            text+=f"{arrow} vs daily avg: *{fmt(daily_avg)}*  ({diff_str})\n"
        if by_cat_today:
            text+="\n*By category:*\n"
            for cat,amt in sorted(by_cat_today.items(),key=lambda x:-x[1]):
                bud=budgets.get(cat,0)
                if bud:
                    pct=min((by_cat_month.get(cat,0)/bud)*100,100); status=budget_status(pct)
                    text+=f"{CAT_ICONS.get(cat,chr(8226))} {cat}: *{fmt(amt)}*  {status} _{pct:.0f}% of monthly budget_\n"
                else: text+=f"{CAT_ICONS.get(cat,chr(8226))} {cat}: *{fmt(amt)}*\n"
        text+="\n"
    if total_budget and daily_budget_share>0:
        if budget_left_today>=0:
            text+=f"\u2705 *Daily budget left:* {fmt(abs(budget_left_today))}\n"
        else:
            text+=f"\u26a0\ufe0f *Daily budget over by:* {fmt(abs(budget_left_today))}\n"
    text+=f"\n📊 *{month_label(month)} so far:* {fmt(month_spent)}"
    if total_budget: text+=f"  /  {fmt(total_budget)}  {budget_status(month_spent/total_budget*100)}"
    text+="\n"
    pivot=datetime.strptime(date_str,"%Y-%m-%d"); base_day=min(pivot,datetime.strptime(today_dt,"%Y-%m-%d"))
    dates=[base_day-timedelta(days=i) for i in range(4,-1,-1)]; date_row=[]
    for d in dates:
        ds=d.strftime("%Y-%m-%d"); label=f"[{d.strftime('%d/%m')}]" if ds==date_str else d.strftime("%d/%m")
        date_row.append(InlineKeyboardButton(label,callback_data=f"daily_{ds}"))
    keyboard=[date_row,[InlineKeyboardButton("◄ Earlier",callback_data=f"daily_prev_{date_str}")]]
    if is_callback: await update_or_query.edit_message_text(text,parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(keyboard))
    else: await update_or_query.message.reply_text(text,parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(keyboard))

async def daily_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer(); chat_id=str(update.effective_chat.id); data=query.data
    if data.startswith("daily_prev_"): new_date=(datetime.strptime(data.replace("daily_prev_",""),"%Y-%m-%d")-timedelta(days=5)).strftime("%Y-%m-%d")
    else: new_date=data.replace("daily_","")
    await _send_daily(query,chat_id,new_date,is_callback=True)

async def handle_reply_keyboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text=update.message.text
    mapping={"📊 Summary":summary,"📋 Spent":spent_cmd,"🏆 Report":report_cmd,"🧾 Recent":recent,"👫 Who":who_cmd,"💰 Budgets":budgets_cmd,"🗑 Delete":delete_cmd,"🔁 Recurring":recurring_cmd,"❓ Help":help_cmd,"🔄 Sheets":refresh_sheets_cmd}
    if text in mapping: await mapping[text](update,ctx)
    else: await smart_add(update,ctx)

async def refresh_sheets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id=str(update.effective_chat.id)
    if not GOOGLE_SHEETS_CREDS or not GOOGLE_SHEET_ID:
        await update.message.reply_text("⚠️ Google Sheets not configured."); return
    msg=await update.message.reply_text("🔄 Refreshing Google Sheets...")
    errors=[]; loop=asyncio.get_event_loop()
    def _run():
        _,spreadsheet=_get_gs()
        if not spreadsheet: errors.append("Could not connect."); return
        month=get_month()
        for label,fn,args in [
            ("Budget Tracker",gs_refresh_budget,(spreadsheet,chat_id,month)),
            ("Summary",gs_refresh_summary,(spreadsheet,chat_id)),
            ("Dashboard",gs_refresh_dashboard,(spreadsheet,chat_id)),
        ]:
            try: fn(*args); print(f"{label} OK")
            except Exception as e: errors.append(f"{label}: {e}")
            time.sleep(3)
    await loop.run_in_executor(None,_run)
    if errors: await msg.edit_text(f"⚠️ *Errors:*\n`{chr(124).join(errors)[:600]}`",parse_mode="Markdown")
    else: await msg.edit_text(
        "✅ *Google Sheets updated!*\n\n"
        "Your chart ranges:\n"
        "• Donut  → `Budget Tracker!A2:A10, C2:C10`\n"
        "• Bar    → `Budget Tracker!A2:C10`\n"
        "• Line   → `Summary!A:D`",
        parse_mode="Markdown")

async def who_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    month   = get_month()
    txs     = get_txs(chat_id, month)
    expenses = [t for t in txs if t[2] == "expense"]

    if not expenses:
        await update.message.reply_text(
            f"No expenses logged yet for {month_label()}!",
            parse_mode="Markdown"
        )
        return

    by_person = {}
    by_person_cat = {}
    total_spent = sum(t[3] for t in expenses)

    for t in expenses:
        person = t[7]
        amount = t[3]
        cat    = t[5]
        by_person[person]          = by_person.get(person, 0) + amount
        if person not in by_person_cat:
            by_person_cat[person]  = {}
        by_person_cat[person][cat] = by_person_cat[person].get(cat, 0) + amount

    ranked = sorted(by_person.items(), key=lambda x: -x[1])

    text = f"\U0001f46b *Who spent what — {month_label()}*\n{'─'*28}\n\n"
    text += f"💸 *Total: {fmt(total_spent)}*\n\n"

    medals = ["🥇", "🥈", "🥉"]
    for i, (person, amount) in enumerate(ranked):
        pct    = round(amount / total_spent * 100) if total_spent else 0
        medal  = medals[i] if i < len(medals) else "👤"
        bar    = progress_bar(pct, 8)
        text  += f"{medal} *{person}*\n"
        text  += f"`{bar}` {fmt(amount)} ({pct}%)\n"

        cats = sorted(by_person_cat[person].items(), key=lambda x: -x[1])[:3]
        for cat, amt in cats:
            icon  = CAT_ICONS.get(cat, "•")
            text += f"  {icon} {cat}: {fmt(amt)}\n"
        text += "\n"

    priciest = max(expenses, key=lambda t: t[3])
    icon = CAT_ICONS.get(priciest[5], "•")
    text += f"{'─'*28}\n"
    text += f"🏷 *Biggest single spend:*\n{icon} {fmt(priciest[3])} — {priciest[4]}\n_{priciest[7]} on {datetime.strptime(priciest[6], '%Y-%m-%d').strftime('%d %b')}_"

    await update.message.reply_text(text, parse_mode="Markdown")


async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤷 Unknown command. Type /help to see all commands.")


async def weekly_digest(app):
    for chat_id in get_all_chats():
        try:
            month=get_month(); income,spent,by_cat=calc_summary(chat_id,month)
            budgets=get_budgets(chat_id); total_budget=sum(budgets.values())
            pct=(spent/total_budget*100) if total_budget else 0; grade,grade_msg=get_grade(pct/100 if total_budget else 0)
            text=(f"📅 *Weekly Digest — {month_label()}*\n{'-'*28}\nGrade so far: *{grade}* {grade_msg}\n\n"
                  f"💵 Income:  {fmt(income)}\n💸 Spent:   {fmt(spent)}\n💰 Left:    *{fmt(income-spent)}*\n")
            if total_budget: text+=f"\n{budget_status(pct)} `{progress_bar(pct,12)}` {pct:.0f}% of budget used\n"
            if by_cat:
                text+="\n*Top spending:*\n"
                for cat,amt in sorted(by_cat.items(),key=lambda x:-x[1])[:3]:
                    text+=f"{CAT_ICONS.get(cat,chr(8226))} {cat}: {fmt(amt)}\n"
            text+="\n_Type /report for full breakdown_"
            await app.bot.send_message(chat_id=chat_id,text=text,parse_mode="Markdown")
        except Exception as e: print(f"Weekly digest error for {chat_id}: {e}")

async def process_recurring(app):
    today=datetime.now()
    for chat_id in get_all_chats():
        for r in get_recurring(chat_id):
            if r[5]==today.day:
                date=today.strftime("%Y-%m-%d")
                # ── Duplicate guard: skip if already logged today ──────────────
                existing = get_txs_by_date(chat_id, date)
                already_logged = any(
                    t[4] == r[3] and t[3] == r[2] and t[2] == "expense"
                    for t in existing
                )
                if already_logged:
                    print(f"Recurring skip (already logged): {r[3]} for {chat_id}")
                    continue
                # ─────────────────────────────────────────────────────────────
                tx_info=add_tx(chat_id,"expense",r[2],r[3],r[4],date,"Auto")
                asyncio.create_task(gs_sync_async(chat_id,*tx_info,bot=app.bot))
                try:
                    await app.bot.send_message(chat_id=chat_id,
                        text=f"🔁 *Recurring logged!*\n\n{CAT_ICONS.get(r[4],chr(8226))} *{r[4]}*\n💸 {fmt(r[2])} — {r[3]}",
                        parse_mode="Markdown")
                except Exception as e: print(f"Recurring error: {e}")

def main():
    if not TOKEN: print("ERROR: BOT_TOKEN not set!"); return
    print(f"Starting... token: {TOKEN[:10]}")
    print(f"DB: {'PostgreSQL' if DATABASE_URL else 'SQLite'}")
    init_db()
    try:
        app=Application.builder().token(TOKEN).build()
        app.add_handler(CommandHandler("start",start))
        app.add_handler(CommandHandler("help",help_cmd))
        app.add_handler(CommandHandler("add",add_expense))
        app.add_handler(CommandHandler("income",add_income))
        app.add_handler(CommandHandler("summary",summary))
        app.add_handler(CommandHandler("spent",spent_cmd))
        app.add_handler(CommandHandler("report",report_cmd))
        app.add_handler(CommandHandler("recent",recent))
        app.add_handler(CommandHandler("delete",delete_cmd))
        app.add_handler(CommandHandler("budget",budget_cmd))
        app.add_handler(CommandHandler("budgets",budgets_cmd))
        app.add_handler(CommandHandler("recurring",recurring_cmd))
        app.add_handler(CommandHandler("month",month_cmd))
        app.add_handler(CommandHandler("daily",daily_cmd))
        app.add_handler(CommandHandler("refreshsheets",refresh_sheets_cmd))
        app.add_handler(CommandHandler("who",who_cmd))
        app.add_handler(CallbackQueryHandler(delete_callback,pattern="^del_"))
        app.add_handler(CallbackQueryHandler(quickcat_callback,pattern="^quickcat"))
        app.add_handler(CallbackQueryHandler(budcat_callback,pattern="^budcat_"))
        app.add_handler(CallbackQueryHandler(delrec_callback,pattern="^delrec_"))
        app.add_handler(CallbackQueryHandler(daily_callback,pattern="^daily_"))
        app.add_handler(MessageHandler(filters.PHOTO,handle_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_reply_keyboard))
        app.add_handler(MessageHandler(filters.COMMAND,unknown))
        scheduler=AsyncIOScheduler()
        scheduler.add_job(weekly_digest,"cron",day_of_week="sun",hour=9,minute=0,args=[app])
        scheduler.add_job(process_recurring,"cron",hour=8,minute=0,args=[app])
        scheduler.start()
        print("🤖 Budget bot is running!")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback; traceback.print_exc(); raise

if __name__=="__main__":
    main()
