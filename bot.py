import os
import sqlite3
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List
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
import asyncio
import json

TOKEN        = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ALERT_THRESHOLD = 0.8  # 80% budget alert
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_SHEETS_CREDS = os.environ.get("GOOGLE_SHEETS_CREDS", "")   # service account JSON string
GOOGLE_SHEET_ID     = os.environ.get("GOOGLE_SHEET_ID", "")       # spreadsheet ID from URL

# ── CATEGORY DEFINITIONS ──────────────────────────────────────────────────────
@dataclass
class Category:
    name: str
    icon: str
    keywords: List[str] = field(default_factory=list)

CATEGORIES = [
    Category("Housing/Rent",      "🏠", ["rent", "mortgage", "condo", "hdb", "housing", "property"]),
    Category("Groceries",         "🛒", ["grocery", "groceries", "supermarket", "market", "fairprice", "ntuc",
                                          "cold storage", "giant", "trader", "walmart", "costco",
                                          "food", "lunch", "dinner", "breakfast", "meal", "supper"]),
    Category("Dining Out",        "🍽", ["dining", "restaurant", "cafe", "coffee", "starbucks", "mcdonald",
                                          "burger", "pizza", "sushi", "hawker", "kopitiam", "grab food",
                                          "deliveroo", "foodpanda", "nobu", "brunch"]),
    Category("Subscriptions",     "📱", ["netflix", "spotify", "apple", "google", "subscription", "sub",
                                          "amazon prime", "youtube", "disney", "hulu"]),
    Category("Transportation",    "🚗", ["grab", "taxi", "uber", "mrt", "bus", "transport", "petrol",
                                          "gas", "parking", "ez-link", "train", "gojek", "car"]),
    Category("Utilities",         "⚡", ["electric", "water", "internet", "phone", "bill", "utility",
                                          "telco", "singtel", "starhub", "m1"]),
    Category("Entertainment",     "🎬", ["movie", "cinema", "concert", "game", "shopping", "mall", "entertainment"]),
    Category("Savings",           "💰", ["savings", "save", "investment", "invest", "cpf"]),
    Category("Personal/Shopping", "🛍", ["shopping", "clothes", "shoes", "haircut", "gym", "beauty",
                                          "personal", "lazada", "shopee", "amazon"]),
]

# Derived lookups — the rest of the code uses these unchanged
CATS         = [c.name for c in CATEGORIES]
CAT_ICONS    = {c.name: c.icon for c in CATEGORIES} | {"Income": "💵"}
CAT_KEYWORDS = {c.name: c.keywords for c in CATEGORIES}

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
    return tx_id, date, month, type_, amount, cat, desc, added_by

def del_tx(tx_id, chat_id):
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(f"DELETE FROM transactions WHERE id={p} AND chat_id={p}", (tx_id, chat_id))
    conn.commit(); conn.close()

def update_tx_category(tx_id, chat_id, new_category: str):
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(
        f"UPDATE transactions SET category={p} WHERE id={p} AND chat_id={p}",
        (new_category, tx_id, chat_id),
    )
    conn.commit(); conn.close()

def get_tx(tx_id, chat_id):
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(
        f"SELECT id,chat_id,type,amount,description,category,date,added_by,month "
        f"FROM transactions WHERE id={p} AND chat_id={p}",
        (tx_id, chat_id),
    )
    row = cur.fetchone()
    conn.close()
    return row

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

def get_txs_by_date(chat_id, date_str):
    conn, db_type = get_conn(); p = ph(db_type)
    cur = conn.cursor()
    cur.execute(
        f"SELECT id,chat_id,type,amount,description,category,date,added_by,month "
        f"FROM transactions WHERE chat_id={p} AND date={p} ORDER BY id ASC",
        (chat_id, date_str)
    )
    rows = cur.fetchall(); conn.close(); return rows

def get_all_chats():
    conn, db_type = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT chat_id FROM transactions")
    rows = cur.fetchall(); conn.close()
    return [r[0] for r in rows]

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
_gs_client = None
_gs_sheet  = None

SHEET_DASHBOARD = "Dashboard"
SHEET_TX        = "Transactions"
SHEET_BUDGET    = "Budget Tracker"
SHEET_SUMMARY   = "Summary"

TX_HEADERS  = ["ID", "Date", "Month", "Type", "Amount (SGD)", "Category", "Description", "Added By", "Running Balance"]
BUD_HEADERS = ["Category", "Monthly Budget", "Spent This Month", "Remaining", "Progress Bar", "% Used", "Status"]
SUM_HEADERS = ["Category", "Jan Budget", "Jan Spent", "Jan %", "Feb Budget", "Feb Spent", "Feb %", "Mar Budget", "Mar Spent", "Mar %", "3-Month Avg", "Trend"]

# ── colour helpers ─────────────────────────────────────────────────────────────
_GREEN  = {"red": 0.851, "green": 0.957, "blue": 0.859}   # #D9F5DB
_AMBER  = {"red": 0.996, "green": 0.957, "blue": 0.875}   # #FEF4DF
_RED    = {"red": 0.988, "green": 0.910, "blue": 0.902}   # #FCE8E6
_HEADER = {"red": 0.235, "green": 0.522, "blue": 0.282}   # #3C8548 dark green
_WHITE  = {"red": 1.0,   "green": 1.0,   "blue": 1.0}

def _color_cell(bg, bold=False, font_color=None, font_size=10, h_align="LEFT"):
    fmt = {
        "backgroundColor": bg,
        "textFormat": {"bold": bold, "fontSize": font_size},
        "horizontalAlignment": h_align,
    }
    if font_color:
        fmt["textFormat"]["foregroundColor"] = font_color
    return fmt

def _get_gs():
    global _gs_client, _gs_sheet
    if _gs_sheet is not None:
        return _gs_client, _gs_sheet
    if not GOOGLE_SHEETS_CREDS or not GOOGLE_SHEET_ID:
        return None, None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        info  = json.loads(GOOGLE_SHEETS_CREDS)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        _gs_client = gspread.authorize(creds)
        _gs_sheet  = _gs_client.open_by_key(GOOGLE_SHEET_ID)
        _ensure_sheets(_gs_sheet)
        print("Google Sheets connected OK.")
    except Exception as e:
        print(f"Google Sheets init error: {e}")
        _gs_client = _gs_sheet = None
    return _gs_client, _gs_sheet

def _get_or_create_ws(spreadsheet, title, headers, cols=None):
    try:
        ws = spreadsheet.worksheet(title)
    except Exception:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=cols or len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        _fmt_header_row(ws, len(headers))
    return ws

def _fmt_header_row(ws, num_cols):
    """Bold, freeze, and colour the header row dark green with white text."""
    try:
        col_letter = chr(ord('A') + num_cols - 1)
        ws.format(f"A1:{col_letter}1", {
            "backgroundColor": _HEADER,
            "textFormat": {"bold": True, "foregroundColor": _WHITE, "fontSize": 10},
            "horizontalAlignment": "CENTER",
        })
        ws.freeze(rows=1)
    except Exception:
        pass

def _fmt_col_currency(ws, col_letter, start_row, end_row):
    """Apply SGD currency format to a column range."""
    try:
        ws.format(f"{col_letter}{start_row}:{col_letter}{end_row}", {
            "numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0.00'}
        })
    except Exception:
        pass

def _fmt_col_pct(ws, col_letter, start_row, end_row):
    try:
        ws.format(f"{col_letter}{start_row}:{col_letter}{end_row}", {
            "numberFormat": {"type": "NUMBER", "pattern": "0.0\"%\""}
        })
    except Exception:
        pass

def _autofit_cols(ws, num_cols):
    """Auto-resize all columns to fit content."""
    try:
        body = {
            "requests": [{
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": num_cols,
                    }
                }
            }]
        }
        ws.spreadsheet.batch_update(body)
    except Exception:
        pass

def _apply_banding(ws, num_cols, num_rows):
    """Apply alternating row colours (banding) to the data range."""
    try:
        col_letter = chr(ord('A') + num_cols - 1)
        ws.spreadsheet.batch_update({"requests": [{
            "addBanding": {
                "bandedRange": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "endRowIndex": num_rows + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    },
                    "rowProperties": {
                        "headerColor":     _HEADER,
                        "firstBandColor":  _WHITE,
                        "secondBandColor": {"red": 0.973, "green": 0.984, "blue": 0.973},
                    }
                }
            }
        }]})
    except Exception:
        pass

def _add_filter(ws, num_cols):
    """Enable auto-filter on the header row."""
    try:
        ws.spreadsheet.batch_update({"requests": [{
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    }
                }
            }
        }]})
    except Exception:
        pass

def _status_color(pct):
    if pct >= 100: return _RED
    if pct >= 80:  return _AMBER
    return _GREEN

def _status_text(pct):
    if pct >= 100: return "Over budget"
    if pct >= 80:  return "Warning"
    return "OK"

def _progress_bar_str(pct, width=10):
    filled = max(0, min(round(pct / 100 * width), width))
    return "█" * filled + "░" * (width - filled)

def _ensure_sheets(spreadsheet):
    _get_or_create_ws(spreadsheet, SHEET_DASHBOARD, [], cols=10)
    _get_or_create_ws(spreadsheet, SHEET_TX,        TX_HEADERS)
    _get_or_create_ws(spreadsheet, SHEET_BUDGET,    BUD_HEADERS)
    _get_or_create_ws(spreadsheet, SHEET_SUMMARY,   SUM_HEADERS)
    try:
        default = spreadsheet.worksheet("Sheet1")
        spreadsheet.del_worksheet(default)
    except Exception:
        pass

# ── Transactions sheet ────────────────────────────────────────────────────────
def gs_append_tx(tx_id, date, month, type_, amount, cat, desc, added_by):
    _, spreadsheet = _get_gs()
    if not spreadsheet:
        return
    try:
        ws = _get_or_create_ws(spreadsheet, SHEET_TX, TX_HEADERS)
        all_vals = ws.get_all_values()
        next_row = len(all_vals) + 1

        sign = 1 if type_ == "income" else -1
        if next_row == 2:
            running_bal_formula = f"=E2"
        else:
            running_bal_formula = f"=I{next_row - 1}+IF(D{next_row}=\"income\",E{next_row},-E{next_row})"

        ws.append_row(
            [str(tx_id), date, month, type_, amount, cat, desc, added_by, running_bal_formula],
            value_input_option="USER_ENTERED"
        )
        _fmt_col_currency(ws, "E", next_row, next_row)
        _fmt_col_currency(ws, "I", next_row, next_row)

        income_color = {"red": 0.851, "green": 0.957, "blue": 0.859}
        expense_color = {"red": 0.988, "green": 0.910, "blue": 0.902}
        ws.format(f"D{next_row}", {
            "backgroundColor": income_color if type_ == "income" else expense_color,
            "textFormat": {"bold": True, "fontSize": 10},
            "horizontalAlignment": "CENTER",
        })
        _autofit_cols(ws, len(TX_HEADERS))
    except Exception as e:
        print(f"gs_append_tx error: {e}")

def gs_delete_tx(tx_id):
    _, spreadsheet = _get_gs()
    if not spreadsheet:
        return
    try:
        ws   = _get_or_create_ws(spreadsheet, SHEET_TX, TX_HEADERS)
        cell = ws.find(str(tx_id), in_column=1)
        if cell:
            ws.delete_rows(cell.row)
    except Exception as e:
        print(f"gs_delete_tx error: {e}")

def gs_update_tx_category(tx_id, new_category):
    _, spreadsheet = _get_gs()
    if not spreadsheet:
        return
    try:
        ws   = _get_or_create_ws(spreadsheet, SHEET_TX, TX_HEADERS)
        cell = ws.find(str(tx_id), in_column=1)
        if cell:
            ws.update_cell(cell.row, 6, new_category)
    except Exception as e:
        print(f"gs_update_tx_category error: {e}")

# ── Budget Tracker sheet ──────────────────────────────────────────────────────
def _refresh_budget_sheet(spreadsheet, chat_id, month):
    budgets     = get_budgets(chat_id)
    _, _, by_cat = calc_summary(chat_id, month)

    ws = _get_or_create_ws(spreadsheet, SHEET_BUDGET, BUD_HEADERS)
    ws.clear()
    ws.append_row(BUD_HEADERS, value_input_option="USER_ENTERED")
    _fmt_header_row(ws, len(BUD_HEADERS))

    bud_rows  = []
    row_colors = []

    for cat in CATS:
        spent = by_cat.get(cat, 0)
        bud   = budgets.get(cat, 0)
        if not spent and not bud:
            continue
        remaining = round(bud - spent, 2) if bud else ""
        pct       = round(spent / bud * 100, 1) if bud else 0
        bar       = _progress_bar_str(pct) if bud else ""
        status    = _status_text(pct) if bud else "No budget set"
        bud_rows.append([cat, bud or "", round(spent, 2), remaining, bar, pct if bud else "", status])
        row_colors.append(_status_color(pct) if bud else _WHITE)

    if not bud_rows:
        return

    total_bud   = sum(budgets.get(c, 0) for c in CATS)
    total_spent = sum(by_cat.get(c, 0) for c in CATS if budgets.get(c, 0))
    total_rem   = round(total_bud - total_spent, 2)
    total_pct   = round(total_spent / total_bud * 100, 1) if total_bud else 0
    bud_rows.append(["TOTAL", total_bud, round(total_spent, 2), total_rem, _progress_bar_str(total_pct), total_pct, _status_text(total_pct)])
    row_colors.append(_WHITE)

    ws.append_rows(bud_rows, value_input_option="USER_ENTERED")

    data_end = len(bud_rows) + 1
    _fmt_col_currency(ws, "B", 2, data_end)
    _fmt_col_currency(ws, "C", 2, data_end)
    _fmt_col_currency(ws, "D", 2, data_end)
    _fmt_col_pct(ws, "F", 2, data_end)

    requests = []
    for i, color in enumerate(row_colors):
        row_idx = i + 1
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": row_idx,
                    "endRowIndex": row_idx + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(BUD_HEADERS),
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    if row_colors:
        last_row_idx = len(row_colors)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": last_row_idx,
                    "endRowIndex": last_row_idx + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(BUD_HEADERS),
                },
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.902, "green": 0.902, "blue": 0.902},
                    "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold",
            }
        })

    if requests:
        spreadsheet.batch_update({"requests": requests})

    _add_filter(ws, len(BUD_HEADERS))
    _autofit_cols(ws, len(BUD_HEADERS))

# ── Summary sheet ─────────────────────────────────────────────────────────────
def _refresh_summary_sheet(spreadsheet, chat_id):
    month    = get_month()
    budgets  = get_budgets(chat_id)
    dt       = datetime.strptime(month, "%Y-%m")
    months   = [(dt - timedelta(days=30 * i)).strftime("%Y-%m") for i in range(2, -1, -1)]

    ws = _get_or_create_ws(spreadsheet, SHEET_SUMMARY, SUM_HEADERS)
    ws.clear()

    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in months]
    headers = ["Category"]
    for lbl in month_labels:
        headers += [f"{lbl} Budget", f"{lbl} Spent", f"{lbl} %"]
    headers += ["3-Month Avg", "Trend"]
    ws.append_row(headers, value_input_option="USER_ENTERED")
    _fmt_header_row(ws, len(headers))

    by_cat_per_month = [calc_summary(chat_id, m)[2] for m in months]
    sum_rows   = []
    row_colors = []

    for cat in CATS:
        row   = [cat]
        spends = []
        for i, m in enumerate(months):
            bud   = budgets.get(cat, 0)
            spent = round(by_cat_per_month[i].get(cat, 0), 2)
            pct   = round(spent / bud * 100, 1) if bud else ""
            row  += [bud or "", spent, pct]
            spends.append(spent)
        avg = round(sum(spends) / len(spends), 2) if any(spends) else 0
        if spends[0] == 0 and spends[-1] == 0:
            trend = "—"
        elif spends[-1] > spends[0]:
            trend = "▲ Increasing"
        elif spends[-1] < spends[0]:
            trend = "▼ Decreasing"
        else:
            trend = "→ Stable"
        row += [avg, trend]
        if any(spends) or budgets.get(cat, 0):
            latest_pct = round(spends[-1] / budgets[cat] * 100, 1) if budgets.get(cat, 0) else 0
            sum_rows.append(row)
            row_colors.append(_status_color(latest_pct) if budgets.get(cat, 0) else _WHITE)

    if sum_rows:
        ws.append_rows(sum_rows, value_input_option="USER_ENTERED")
        data_end = len(sum_rows) + 1
        for col in ["B", "C", "E", "F", "H", "I", "K"]:
            _fmt_col_currency(ws, col, 2, data_end)
        for col in ["D", "G", "J"]:
            _fmt_col_pct(ws, col, 2, data_end)

        requests = []
        for i, color in enumerate(row_colors):
            row_idx = i + 1
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": row_idx,
                        "endRowIndex": row_idx + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(headers),
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })
        if requests:
            spreadsheet.batch_update({"requests": requests})

    _add_filter(ws, len(headers))
    _autofit_cols(ws, len(headers))

# ── Dashboard sheet ───────────────────────────────────────────────────────────
def _delete_all_charts(spreadsheet, sheet_id):
    """Remove all existing charts from a sheet so we don't stack duplicates on refresh."""
    try:
        meta = spreadsheet.fetch_sheet_metadata()
        sheets = meta.get("sheets", [])
        for s in sheets:
            if s["properties"]["sheetId"] == sheet_id:
                charts = s.get("charts", [])
                if not charts:
                    return
                reqs = [{"deleteEmbeddedObject": {"objectId": c["chartId"]}} for c in charts]
                spreadsheet.batch_update({"requests": reqs})
                return
    except Exception as e:
        print(f"_delete_all_charts error: {e}")

def _add_charts(spreadsheet, ws, by_cat, budgets, months_data):
    """
    Embed three charts on the Dashboard sheet anchored to column H:
      - Donut : spending by category   (H1)
      - Bar   : spent vs budget        (H22)
      - Line  : 3-month spend trend    (H43)
    months_data = [(label, spent_dict), ...] ordered oldest → newest

    Strategy: write all scratch data to cols I–Z (hidden area) FIRST in one
    batch_update, then issue the addChart requests in a second batch_update.
    This avoids the race condition where chart ranges are referenced before
    the data cells exist.
    """
    sheet_id = ws.id

    active_cats = [c for c in CATS if by_cat.get(c, 0) or budgets.get(c, 0)]
    spending_cats = [c for c in active_cats if by_cat.get(c, 0) > 0]
    if not active_cats:
        return

    # ── Scratch layout (col I = index 8, all 0-indexed) ───────────────────────
    # Row 1  (idx 0): pie category labels
    # Row 2  (idx 1): pie category values
    # Row 4  (idx 3): bar category labels
    # Row 5  (idx 4): bar budget values
    # Row 6  (idx 5): bar spent values
    # Row 8  (idx 7): line month labels
    # Row 9  (idx 8): line total spent per month
    # Row 10 (idx 9): line budget reference (flat)

    SC = 8  # scratch start column index (col I)

    pie_labels     = [c for c in spending_cats]
    pie_values     = [round(by_cat.get(c, 0), 2) for c in spending_cats]
    bar_labels     = active_cats
    bar_budgets_v  = [budgets.get(c, 0) for c in active_cats]
    bar_spent_v    = [round(by_cat.get(c, 0), 2) for c in active_cats]
    line_labels    = [m[0] for m in months_data]
    line_spent     = [round(sum(m[1].values()), 2) for m in months_data]
    total_budget   = sum(budgets.values())
    line_budget    = [total_budget] * len(months_data)

    nc  = len(active_cats)
    npc = len(spending_cats)
    nm  = len(months_data)

    # ── STEP 1: write all scratch data in one call ────────────────────────────
    scratch_grid = [
        pie_labels  + [""] * (max(nc, nm) - npc),   # row 1  (idx 0)
        pie_values  + [""] * (max(nc, nm) - npc),   # row 2  (idx 1)
        [""],                                         # row 3  (idx 2) spacer
        bar_labels,                                   # row 4  (idx 3)
        bar_budgets_v,                                # row 5  (idx 4)
        bar_spent_v,                                  # row 6  (idx 5)
        [""],                                         # row 7  (idx 6) spacer
        line_labels,                                  # row 8  (idx 7)
        line_spent,                                   # row 9  (idx 8)
        line_budget,                                  # row 10 (idx 9)
    ]

    # Pad all rows to same width
    max_w = max(len(r) for r in scratch_grid)
    scratch_grid = [r + [""] * (max_w - len(r)) for r in scratch_grid]

    # Convert to Sheets batchUpdate updateCells request (bypasses gspread rate limits)
    def _cell(v):
        if isinstance(v, (int, float)):
            return {"userEnteredValue": {"numberValue": v}}
        return {"userEnteredValue": {"stringValue": str(v)}}

    rows_data = [{"values": [_cell(v) for v in row]} for row in scratch_grid]

    write_req = {
        "updateCells": {
            "rows": rows_data,
            "fields": "userEnteredValue",
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": SC},
        }
    }

    try:
        print(f"[CHARTS] Writing scratch data: {len(scratch_grid)} rows x {max_w} cols starting at col {SC}")
        result = spreadsheet.batch_update({"requests": [write_req]})
        print(f"[CHARTS] Scratch write OK: {result.get('spreadsheetId','?')}")
    except Exception as e:
        import traceback
        print(f"[CHARTS] Chart scratch write error: {e}")
        print(traceback.format_exc())
        return

    # ── STEP 2: build chart specs referencing the now-committed ranges ────────
    def src(r1, r2, c1, c2):
        return {"sheetId": sheet_id, "startRowIndex": r1, "endRowIndex": r2,
                "startColumnIndex": c1, "endColumnIndex": c2}

    def anchor(row, col=8):  # col 8 = column I (0-indexed) — charts sit right of data
        return {
            "overlayPosition": {
                "anchorCell": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": col},
                "offsetXPixels": 10, "offsetYPixels": 0,
                "widthPixels": 500, "heightPixels": 280,
            }
        }

    # Donut — rows 1-2, SC..SC+npc
    donut_req = {
        "spec": {
            "title": f"Spending by category — {month_label()}",
            "titleTextFormat": {"bold": True, "fontSize": 11},
            "pieChart": {
                "legendPosition": "RIGHT_LEGEND",
                "pieHole": 0.5,
                "domain": {"data": {"sourceRange": {"sources": [src(0, 1, SC, SC + npc)]}}},
                "series": {"data": {"sourceRange": {"sources": [src(1, 2, SC, SC + npc)]}}},
            },
        },
        "position": anchor(1),
    }

    # Bar — rows 4-6, SC..SC+nc
    bar_req = {
        "spec": {
            "title": "Spent vs budget by category",
            "titleTextFormat": {"bold": True, "fontSize": 11},
            "basicChart": {
                "chartType": "BAR",
                "legendPosition": "BOTTOM_LEGEND",
                "axis": [
                    {"position": "BOTTOM_AXIS", "title": "SGD ($)"},
                    {"position": "LEFT_AXIS",   "title": "Category"},
                ],
                "domains": [{"domain": {"data": {"sourceRange": {"sources": [src(3, 4, SC, SC + nc)]}}}}],
                "series": [
                    {
                        "series": {"data": {"sourceRange": {"sources": [src(4, 5, SC, SC + nc)]}}},
                        "targetAxis": "BOTTOM_AXIS",
                        "color": {"red": 0.204, "green": 0.659, "blue": 0.325},
                    },
                    {
                        "series": {"data": {"sourceRange": {"sources": [src(5, 6, SC, SC + nc)]}}},
                        "targetAxis": "BOTTOM_AXIS",
                        "color": {"red": 0.918, "green": 0.263, "blue": 0.208},
                    },
                ],
                "headerCount": 1,
            },
        },
        "position": anchor(22),
    }

    # Line — rows 8-10, SC..SC+nm
    line_req = {
        "spec": {
            "title": "3-month spending trend",
            "titleTextFormat": {"bold": True, "fontSize": 11},
            "basicChart": {
                "chartType": "LINE",
                "legendPosition": "BOTTOM_LEGEND",
                "axis": [
                    {"position": "BOTTOM_AXIS", "title": "Month"},
                    {"position": "LEFT_AXIS",   "title": "SGD ($)"},
                ],
                "domains": [{"domain": {"data": {"sourceRange": {"sources": [src(7, 8, SC, SC + nm)]}}}}],
                "series": [
                    {
                        "series": {"data": {"sourceRange": {"sources": [src(8, 9, SC, SC + nm)]}}},
                        "targetAxis": "LEFT_AXIS",
                        "color": {"red": 0.259, "green": 0.522, "blue": 0.957},
                    },
                    {
                        "series": {"data": {"sourceRange": {"sources": [src(9, 10, SC, SC + nm)]}}},
                        "targetAxis": "LEFT_AXIS",
                        "color": {"red": 0.7, "green": 0.7, "blue": 0.7},
                    },
                ],
                "headerCount": 1,
            },
        },
        "position": anchor(43),
    }

    # ── STEP 3: create all three charts in one batch ──────────────────────────
    try:
        print(f"[CHARTS] sheet_id={sheet_id}, npc={npc}, nc={nc}, nm={nm}, SC={SC}")
        print(f"[CHARTS] pie_labels={pie_labels}")
        print(f"[CHARTS] bar_labels={bar_labels}")
        print(f"[CHARTS] line_labels={line_labels}, line_spent={line_spent}")
        result = spreadsheet.batch_update({"requests": [
            {"addChart": {"chart": donut_req}},
            {"addChart": {"chart": bar_req}},
            {"addChart": {"chart": line_req}},
        ]})
        print(f"[CHARTS] batch_update result: {result}")
        print("Charts added successfully.")
    except Exception as e:
        import traceback
        print(f"[CHARTS] _add_charts error: {e}")
        print(traceback.format_exc())


def _refresh_dashboard(spreadsheet, chat_id):
    """Write dashboard data then add charts. Each step logged independently."""
    month   = get_month()
    budgets = get_budgets(chat_id)
    _, _, by_cat = calc_summary(chat_id, month)

    # Step 1: delete old charts BEFORE clearing/rewriting the sheet
    try:
        ws = _get_or_create_ws(spreadsheet, SHEET_DASHBOARD, [], cols=30)
        _delete_all_charts(spreadsheet, ws.id)
    except Exception as e:
        print(f"_refresh_dashboard delete charts error: {e}")

    # Step 2: write dashboard data + scratch rows
    try:
        _refresh_dashboard_data(spreadsheet, chat_id)
    except Exception as e:
        print(f"_refresh_dashboard data error: {e}")
        return

    # Step 3: add charts — scratch data is now committed
    try:
        dt = datetime.strptime(month, "%Y-%m")
        months_list = [(dt - timedelta(days=30 * i)).strftime("%Y-%m") for i in range(2, -1, -1)]
        months_data = [
            (datetime.strptime(m, "%Y-%m").strftime("%b %Y"), calc_summary(chat_id, m)[2])
            for m in months_list
        ]
        ws = _get_or_create_ws(spreadsheet, SHEET_DASHBOARD, [], cols=30)
        _add_charts(spreadsheet, ws, by_cat, budgets, months_data)
    except Exception as e:
        print(f"_refresh_dashboard charts error: {e}")

# ── Master refresh ────────────────────────────────────────────────────────────
def gs_refresh_summary(chat_id):
    _, spreadsheet = _get_gs()
    if not spreadsheet:
        return
    try:
        month = get_month()
        _refresh_budget_sheet(spreadsheet, chat_id, month)
        _refresh_summary_sheet(spreadsheet, chat_id)
        _refresh_dashboard(spreadsheet, chat_id)
    except Exception as e:
        print(f"gs_refresh_summary error: {e}")

async def gs_sync_async(chat_id, tx_id=None, date=None, month=None,
                        type_=None, amount=None, cat=None, desc=None,
                        added_by=None, delete=False, update_category=False):
    loop = asyncio.get_event_loop()
    if delete and tx_id:
        await loop.run_in_executor(None, gs_delete_tx, tx_id)
    elif update_category and tx_id and cat:
        await loop.run_in_executor(None, gs_update_tx_category, tx_id, cat)
    elif tx_id:
        await loop.run_in_executor(None, gs_append_tx,
                                   tx_id, date, month, type_,
                                   amount, cat, desc, added_by)
    await loop.run_in_executor(None, gs_refresh_summary, chat_id)


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
    for cat_obj in CATEGORIES:
        if any(kw in text_lower for kw in cat_obj.keywords):
            return cat_obj.name
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
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not configured"}

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    if image_bytes[:4] == b'\x89PNG':
        media_type = "image/png"
    elif image_bytes[:2] == b'\xff\xd8':
        media_type = "image/jpeg"
    elif image_bytes[:4] == b'RIFF':
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"
    print(f"Detected media type: {media_type}, size: {len(image_bytes)}")

    # Build valid category list dynamically from CATEGORIES
    valid_categories = ", ".join(c.name for c in CATEGORIES)

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
                        "text": f"""Analyze this receipt image and extract the following information.
Reply ONLY with a JSON object, no other text:
{{
  "merchant": "store/restaurant name",
  "total": 12.34,
  "date": "YYYY-MM-DD or null if not visible",
  "category": "one of: {valid_categories}",
  "items": ["item1 $x.xx", "item2 $x.xx"],
  "confidence": "high/medium/low"
}}

If you cannot read the receipt clearly, return:
{{"error": "Cannot read receipt clearly"}}"""
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
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    added_by = update.effective_user.first_name or "Someone"

    if not ANTHROPIC_API_KEY:
        await update.message.reply_text(
            "⚠️ Receipt scanning not configured yet.\nAsk Jonathan to add the ANTHROPIC_API_KEY!",
            parse_mode="Markdown"
        )
        return

    scanning_msg = await update.message.reply_text("📸 Scanning receipt... give me a sec! ⏳")

    try:
        photo = update.message.photo[-1]
        photo_file = await ctx.bot.get_file(photo.file_id)
        image_bytes = await photo_file.download_as_bytearray()
        image_bytes = bytes(image_bytes)

        print(f"Downloaded image: {len(image_bytes)} bytes")

        if len(image_bytes) < 1000:
            raise Exception(f"Downloaded image too small ({len(image_bytes)} bytes)")

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

        tx_info = add_tx(chat_id, "expense", total, merchant, category, date, added_by)
        asyncio.create_task(gs_sync_async(chat_id, *tx_info))
        tx_id = tx_info[0]

        icon = CAT_ICONS.get(category, "•")
        conf_emoji = "🟢" if confidence == "high" else "🟡" if confidence == "medium" else "🔴"

        items_text = ""
        if items:
            items_text = "\n" + "\n".join(f"  • {item}" for item in items[:5])
            if len(items) > 5:
                items_text += f"\n  _...and {len(items)-5} more_"

        keyboard = []
        row = []
        for i, cat_obj in enumerate(CATEGORIES):
            row.append(InlineKeyboardButton(
                f"{cat_obj.icon} {cat_obj.name.split('/')[0]}",
                callback_data=f"quickcat_{tx_id}_{cat_obj.name}"
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

        await check_budget_alert(update, ctx, chat_id, category)

    except Exception as e:
        print(f"Receipt scan error: {e}")
        await scanning_msg.edit_text(
            "😕 *Something went wrong scanning that receipt.*\n\nTry again or log manually:\n`amount description`",
            parse_mode="Markdown"
        )

# ── SMART QUICK-ADD ───────────────────────────────────────────────────────────
async def smart_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
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
        date = datetime.now().strftime("%Y-%m-%d")
        added_by = update.effective_user.first_name or "Someone"
        tx_info = add_tx(chat_id, "expense", amount, desc, guessed_cat, date, added_by)
        asyncio.create_task(gs_sync_async(chat_id, *tx_info))
        tx_id = tx_info[0]
        icon = CAT_ICONS.get(guessed_cat, "•")

        keyboard = []
        row = []
        for i, cat_obj in enumerate(CATEGORIES):
            row.append(InlineKeyboardButton(
                f"{cat_obj.icon} {cat_obj.name.split('/')[0]}",
                callback_data=f"quickcat_{tx_id}_{cat_obj.name}"
            ))
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
        keyboard = []
        row = []
        for i, cat_obj in enumerate(CATEGORIES):
            row.append(InlineKeyboardButton(
                f"{cat_obj.icon} {cat_obj.name.split('/')[0]}",
                callback_data=f"quickcat_{amount}_{desc[:20]}_{cat_obj.name}"
            ))
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
    updated_cat = "_".join(parts[1:]) if parts else ""
    try:
        maybe_id = int(parts[0])
    except Exception:
        maybe_id = None

    if maybe_id is not None and updated_cat in CATS:
        tx = get_tx(maybe_id, chat_id)
        if not tx:
            await query.edit_message_text("⚠️ Couldn't find that transaction anymore.", parse_mode="Markdown")
            return
        update_tx_category(maybe_id, chat_id, updated_cat)
        asyncio.create_task(gs_sync_async(chat_id, tx_id=maybe_id, cat=updated_cat, update_category=True))
        icon = CAT_ICONS.get(updated_cat, "•")
        amount = tx[3]
        desc = tx[4]
        added_by = tx[7]
        await query.edit_message_text(
            f"✅ *Updated!*\n{icon} *{updated_cat}*\n💸 {fmt(amount)} — {desc}\n👤 {added_by}",
            parse_mode="Markdown",
        )
        await check_budget_alert_query(query, ctx, chat_id, updated_cat)
        return

    # Legacy fallback
    amount = float(parts[0])
    desc = parts[1]
    cat = "_".join(parts[2:])
    txs = get_txs(chat_id)
    for t in txs:
        if t[3] == amount and t[4] == desc:
            del_tx(t[0], chat_id)
            asyncio.create_task(gs_sync_async(chat_id, tx_id=t[0], delete=True))
            break
    date = datetime.now().strftime("%Y-%m-%d")
    added_by = update.effective_user.first_name or "Someone"
    tx_info = add_tx(chat_id, "expense", amount, desc, cat, date, added_by)
    asyncio.create_task(gs_sync_async(chat_id, *tx_info))
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
    if 0.8 <= pct < 0.82:
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
        "📅 `/daily` — today's spending summary\n"
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
        "`/daily` — today's spending + date picker\n"
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
    tx_info = add_tx(chat_id, "expense", amount, desc, matched_cat, date, added_by)
    asyncio.create_task(gs_sync_async(chat_id, *tx_info))
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
    tx_info = add_tx(chat_id, "income", amount, desc, "Income", date, added_by)
    asyncio.create_task(gs_sync_async(chat_id, *tx_info))
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
    chat_id = str(update.effective_chat.id)
    month = get_month()
    income, spent, by_cat = calc_summary(chat_id, month)
    budgets = get_budgets(chat_id)
    total_budget = sum(budgets.values())

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
    txs = get_txs(chat_id)[:15]
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
    txs = get_txs(chat_id)[:15]
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
    tx_id = int(query.data.replace("del_",""))
    del_tx(tx_id, chat_id)
    asyncio.create_task(gs_sync_async(chat_id, tx_id=tx_id, delete=True))
    await query.edit_message_text("✅ Transaction deleted.")

async def budget_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id); args = ctx.args
    if len(args) < 2:
        keyboard = [[InlineKeyboardButton(f"{cat_obj.icon} {cat_obj.name.split('/')[0]}", callback_data=f"budcat_{cat_obj.name}")] for cat_obj in CATEGORIES]
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

async def daily_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d")
    await _send_daily(update, str(update.effective_chat.id), today, is_callback=False)

async def _send_daily(update_or_query, chat_id: str, date_str: str, is_callback: bool):
    txs        = get_txs_by_date(chat_id, date_str)
    expenses   = [t for t in txs if t[2] == "expense"]
    total_today = sum(t[3] for t in expenses)

    month      = date_str[:7]
    _, month_spent, by_cat_month = calc_summary(chat_id, month)
    budgets    = get_budgets(chat_id)
    total_budget = sum(budgets.values())

    try:
        day_num   = int(date_str[8:])
        daily_avg = month_spent / day_num if day_num else 0
    except Exception:
        daily_avg = 0

    try:
        from calendar import monthrange
        days_in_month = monthrange(int(date_str[:4]), int(date_str[5:7]))[1]
        daily_budget_share = (total_budget / days_in_month) if total_budget else 0
        budget_left_today  = daily_budget_share - total_today
    except Exception:
        daily_budget_share = 0
        budget_left_today  = 0

    by_cat_today: dict = {}
    for t in expenses:
        by_cat_today[t[5]] = by_cat_today.get(t[5], 0) + t[3]

    today_dt   = datetime.now().strftime("%Y-%m-%d")
    date_dt    = datetime.strptime(date_str, "%Y-%m-%d")
    if date_str == today_dt:
        date_label = f"Today · {date_dt.strftime('%a, %d %b %Y')}"
    elif date_str == (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"):
        date_label = f"Yesterday · {date_dt.strftime('%a, %d %b %Y')}"
    else:
        date_label = date_dt.strftime("%A, %d %b %Y")

    text = f"📅 *Daily Summary — {date_label}*\n{'─'*30}\n\n"

    if not expenses:
        text += "🎉 *No spending today!*\n_Great job keeping the wallet closed_ 👏\n\n"
    else:
        text += "*Transactions:*\n"
        for t in expenses:
            icon = CAT_ICONS.get(t[5], "•")
            text += f"{icon} {fmt(t[3])}  _{t[4]}_  · `{t[5].split('/')[0]}`\n"
        text += "\n"
        text += f"💸 *Total today:* {fmt(total_today)}\n"

        if daily_avg > 0:
            diff    = total_today - daily_avg
            arrow   = "📈" if diff > 0 else "📉"
            diff_str = f"+{fmt(abs(diff))}" if diff > 0 else f"-{fmt(abs(diff))}"
            text += f"{arrow} vs daily avg: *{fmt(daily_avg)}*  ({diff_str})\n"

        if by_cat_today:
            text += f"\n*By category:*\n"
            for cat, amt in sorted(by_cat_today.items(), key=lambda x: -x[1]):
                icon = CAT_ICONS.get(cat, "•")
                month_cat_spent = by_cat_month.get(cat, 0)
                bud = budgets.get(cat, 0)
                if bud:
                    pct = min((month_cat_spent / bud) * 100, 100)
                    status = budget_status(pct)
                    text += f"{icon} {cat}: *{fmt(amt)}*  {status} _{pct:.0f}% monthly budget used_\n"
                else:
                    text += f"{icon} {cat}: *{fmt(amt)}*\n"
        text += "\n"

    if total_budget and daily_budget_share > 0:
        if budget_left_today >= 0:
            text += f"✅ *Daily budget left:* {fmt(budget_left_today)}\n"
        else:
            text += f"⚠️ *Over daily budget by:* {fmt(abs(budget_left_today))}\n"

    text += f"\n📊 *{month_label(month)} so far:* {fmt(month_spent)}"
    if total_budget:
        pct_month = (month_spent / total_budget * 100)
        text += f"  /  {fmt(total_budget)}  {budget_status(pct_month)}"
    text += "\n"

    pivot    = datetime.strptime(date_str, "%Y-%m-%d")
    base_day = min(pivot, datetime.strptime(today_dt, "%Y-%m-%d"))
    dates    = [base_day - timedelta(days=i) for i in range(4, -1, -1)]
    date_row = []
    for d in dates:
        ds    = d.strftime("%Y-%m-%d")
        label = d.strftime("%d/%m")
        if ds == date_str:
            label = f"[{label}]"
        date_row.append(InlineKeyboardButton(label, callback_data=f"daily_{ds}"))

    keyboard = [date_row, [InlineKeyboardButton("◀ Earlier", callback_data=f"daily_prev_{date_str}")]]

    if is_callback:
        await update_or_query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update_or_query.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def daily_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    chat_id = str(update.effective_chat.id)
    data    = query.data

    if data.startswith("daily_prev_"):
        anchor    = datetime.strptime(data.replace("daily_prev_", ""), "%Y-%m-%d")
        new_date  = (anchor - timedelta(days=5)).strftime("%Y-%m-%d")
    else:
        new_date = data.replace("daily_", "")

    await _send_daily(query, chat_id, new_date, is_callback=True)

async def handle_reply_keyboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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

async def refresh_sheets_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Force-rebuild all Google Sheets tabs and report any chart errors directly to chat."""
    chat_id = str(update.effective_chat.id)
    if not GOOGLE_SHEETS_CREDS or not GOOGLE_SHEET_ID:
        await update.message.reply_text(
            "⚠️ Google Sheets not configured.\nAdd `GOOGLE_SHEETS_CREDS` and `GOOGLE_SHEET_ID` to your environment.",
            parse_mode="Markdown"
        )
        return
    msg = await update.message.reply_text("🔄 Refreshing Google Sheets... give me a moment!")

    errors = []

    def _run():
        _, spreadsheet = _get_gs()
        if not spreadsheet:
            errors.append("Could not connect to Google Sheets.")
            return

        month = get_month()
        try:
            _refresh_budget_sheet(spreadsheet, chat_id, month)
        except Exception as e:
            errors.append(f"Budget sheet: {e}")

        try:
            _refresh_summary_sheet(spreadsheet, chat_id)
        except Exception as e:
            errors.append(f"Summary sheet: {e}")

        # Dashboard data + formatting (no charts yet)
        try:
            _refresh_dashboard_data(spreadsheet, chat_id)
        except Exception as e:
            errors.append(f"Dashboard data: {e}")

        # Charts separately so we get granular error info
        try:
            budgets = get_budgets(chat_id)
            _, _, by_cat = calc_summary(chat_id, month)
            dt = datetime.strptime(month, "%Y-%m")
            months_list = [(dt - timedelta(days=30 * i)).strftime("%Y-%m") for i in range(2, -1, -1)]
            months_data = [
                (datetime.strptime(m, "%Y-%m").strftime("%b %Y"), calc_summary(chat_id, m)[2])
                for m in months_list
            ]
            ws = spreadsheet.worksheet(SHEET_DASHBOARD)
            _delete_all_charts(spreadsheet, ws.id)
            _add_charts(spreadsheet, ws, by_cat, budgets, months_data)
        except Exception as e:
            import traceback
            errors.append(f"Charts: {e}\n{traceback.format_exc()[-300:]}")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _run)

    if errors:
        err_text = "\n\n".join(f"❌ {e}" for e in errors)
        await msg.edit_text(
            f"⚠️ *Sheets refreshed with errors:*\n\n`{err_text[:1000]}`",
            parse_mode="Markdown"
        )
    else:
        await msg.edit_text(
            "✅ *Google Sheets updated!*\n\n"
            "All 4 tabs rebuilt:\n"
            "• Dashboard (with charts)\n"
            "• Transactions\n"
            "• Budget Tracker\n"
            "• Summary",
            parse_mode="Markdown"
        )


def _refresh_dashboard_data(spreadsheet, chat_id):
    """Dashboard data + formatting only — no charts. Called separately so chart errors are isolated."""
    month   = get_month()
    budgets = get_budgets(chat_id)
    income, spent, by_cat = calc_summary(chat_id, month)
    last_month        = (datetime.now().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    _, last_spent, _  = calc_summary(chat_id, last_month)
    total_budget  = sum(budgets.values())
    remaining     = income - spent
    savings_rate  = round(remaining / income * 100, 1) if income > 0 else 0
    budget_pct    = round(spent / total_budget * 100, 1) if total_budget else 0
    mom_diff      = round(spent - last_spent, 2)
    mom_arrow     = "▲" if mom_diff > 0 else "▼"
    updated_at    = datetime.now().strftime("%d %b %Y %H:%M")

    ws = _get_or_create_ws(spreadsheet, SHEET_DASHBOARD, [], cols=30)
    # Only clear the visible data area (A:G), preserve scratch cols I+ used by charts
    ws.batch_clear(["A1:G1000"])

    rows = [
        [f"Family Budget Dashboard — {month_label(month)}", "", "", "", "", f"Updated: {updated_at}"],
        [""],
        ["MONTHLY OVERVIEW", "", "", "", "", ""],
        ["Income", f"${income:,.2f}", "Spent", f"${spent:,.2f}", "Remaining", f"${remaining:,.2f}"],
        ["Savings rate", f"{savings_rate}%", "Budget used", f"{budget_pct}%", "vs Last Month", f"{mom_arrow} ${abs(mom_diff):,.2f}"],
        [""],
        ["CATEGORY BREAKDOWN", "", "", "", "", ""],
        ["Category", "Budget", "Spent", "Remaining", "Progress", "Status"],
    ]

    active_cats = [c for c in CATS if by_cat.get(c, 0) or budgets.get(c, 0)]
    for cat in active_cats:
        bud    = budgets.get(cat, 0)
        s      = round(by_cat.get(cat, 0), 2)
        rem    = round(bud - s, 2) if bud else ""
        pct    = round(s / bud * 100, 1) if bud else 0
        bar    = _progress_bar_str(pct) if bud else ""
        status = _status_text(pct) if bud else "—"
        rows.append([cat, bud or "", s, rem, bar, status])

    rows += [[""], ["TOP 3 SPENDING CATEGORIES", "", "", "", "", ""]]
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])[:3]:
        rows.append([cat, f"${amt:,.2f}", "", "", "", ""])

    ws.update("A1", rows, value_input_option="USER_ENTERED")

    fmt_requests = []

    def bold_row(row_idx, bg=None):
        req = {
            "repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                           "startColumnIndex": 0, "endColumnIndex": 6},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 11}}},
                "fields": "userEnteredFormat.textFormat",
            }
        }
        if bg:
            req["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"] = bg
            req["repeatCell"]["fields"] += ",userEnteredFormat.backgroundColor"
        fmt_requests.append(req)

    bold_row(0, _HEADER)
    fmt_requests.append({"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                   "startColumnIndex": 0, "endColumnIndex": 6},
        "cell": {"userEnteredFormat": {"textFormat": {"foregroundColor": _WHITE, "bold": True, "fontSize": 12}}},
        "fields": "userEnteredFormat.textFormat"
    }})
    bold_row(2)
    bold_row(6)
    bold_row(7, {"red": 0.902, "green": 0.902, "blue": 0.902})

    for i, cat in enumerate(active_cats):
        bud = budgets.get(cat, 0)
        s   = by_cat.get(cat, 0)
        pct = round(s / bud * 100, 1) if bud else 0
        color = _status_color(pct) if bud else _WHITE
        fmt_requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 8 + i, "endRowIndex": 9 + i,
                       "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {"backgroundColor": color}},
            "fields": "userEnteredFormat.backgroundColor",
        }})

    if fmt_requests:
        spreadsheet.batch_update({"requests": fmt_requests})
    _autofit_cols(ws, 6)

async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤷 Unknown command. Type /help to see all commands.")

# ── SCHEDULED TASKS ───────────────────────────────────────────────────────────
async def weekly_digest(app):
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
    today = datetime.now()
    chats = get_all_chats()
    for chat_id in chats:
        recurrings = get_recurring(chat_id)
        for r in recurrings:
            if r[5] == today.day:
                date = today.strftime("%Y-%m-%d")
                tx_info = add_tx(chat_id, "expense", r[2], r[3], r[4], date, "Auto")
                # Use create_task here consistently (was incorrectly awaited before)
                asyncio.create_task(gs_sync_async(chat_id, *tx_info))
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
        app.add_handler(CommandHandler("daily", daily_cmd))
        app.add_handler(CommandHandler("refreshsheets", refresh_sheets_cmd))

        app.add_handler(CallbackQueryHandler(delete_callback, pattern="^del_"))
        app.add_handler(CallbackQueryHandler(quickcat_callback, pattern="^quickcat_"))
        app.add_handler(CallbackQueryHandler(budcat_callback, pattern="^budcat_"))
        app.add_handler(CallbackQueryHandler(delrec_callback, pattern="^delrec_"))
        app.add_handler(CallbackQueryHandler(daily_callback, pattern="^daily_"))

        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply_keyboard))
        app.add_handler(MessageHandler(filters.COMMAND, unknown))

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
