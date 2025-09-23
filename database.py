# database.py

import sqlite3
import json
from dataclasses import asdict
from bot import PaperTrade  # Import the dataclass to help with type hinting

DATABASE_FILE = "trading_bot.db"

# A key-value table for simple settings like balance and leverage.
# The open_trades table schema matches the PaperTrade dataclass.
# The trade_history table stores closed trades.
SCHEMA = """
         CREATE TABLE IF NOT EXISTS settings \
         ( \
             key \
             TEXT \
             PRIMARY \
             KEY, \
             value \
             TEXT \
             NOT \
             NULL
         );

         CREATE TABLE IF NOT EXISTS open_trades \
         ( \
             trade_id \
             TEXT \
             PRIMARY \
             KEY, \
             pair \
             TEXT \
             NOT \
             NULL, \
             entry_price \
             REAL \
             NOT \
             NULL, \
             sl_price \
             REAL \
             NOT \
             NULL, \
             initial_size \
             REAL \
             NOT \
             NULL, \
             remaining_size \
             REAL \
             NOT \
             NULL, \
             leverage \
             REAL \
             NOT \
             NULL, \
             is_long \
             INTEGER \
             NOT \
             NULL, \
             tp_levels \
             TEXT, \
             sl_moved_to_be \
             INTEGER \
             NOT \
             NULL
         );

         CREATE TABLE IF NOT EXISTS trade_history \
         ( \
             trade_id \
             TEXT \
             PRIMARY \
             KEY, \
             pair \
             TEXT \
             NOT \
             NULL, \
             pnl \
             REAL \
             NOT \
             NULL, \
             direction \
             TEXT \
             NOT \
             NULL, \
             entry_price \
             REAL \
             NOT \
             NULL, \
             exit_price \
             REAL \
             NOT \
             NULL, \
             status \
             TEXT \
             NOT \
             NULL, \
             close_timestamp \
             DATETIME \
             DEFAULT \
             CURRENT_TIMESTAMP
         ); \
         -- NEW TABLE FOR LIVE BOT'S PEAK P/L --
         CREATE TABLE IF NOT EXISTS high_water_marks (
            symbol TEXT PRIMARY KEY,
            highest_pnl REAL NOT NULL DEFAULT 0.0
         );
         
         CREATE TABLE IF NOT EXISTS pending_orders (
            order_id TEXT PRIMARY KEY,
            pair TEXT NOT NULL,
            entry_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            is_long INTEGER NOT NULL,
            risk_setting TEXT NOT NULL,
            tp_logic TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
         
         """


def get_db_connection():
    """Establishes a connection to the database."""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row  # Allows accessing columns by name
    return conn


def migrate_database():
    """
    Safely adds new columns and tables to an existing database without deleting data.
    This is safe to run every time the application starts.
    """
    print("Checking database schema for required migrations...")
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # --- Migration 1: Add 'highest_pnl' to 'open_trades' for the paper bot ---
        cursor.execute("PRAGMA table_info(open_trades)")
        columns = [row['name'] for row in cursor.fetchall()]

        if 'highest_pnl' not in columns:
            print("Applying migration: Adding 'highest_pnl' column to 'open_trades' table...")
            cursor.execute("ALTER TABLE open_trades ADD COLUMN highest_pnl REAL NOT NULL DEFAULT 0.0")
            print("Migration successful.")
        else:
            print("'highest_pnl' column already exists in 'open_trades'. No changes needed.")

        cursor.execute("PRAGMA table_info(open_trades)")
        columns = [row['name'] for row in cursor.fetchall()]
        if 'cumulative_pnl' not in columns:
            print("Applying migration: Adding 'cumulative_pnl' column to 'open_trades' table...")
            cursor.execute("ALTER TABLE open_trades ADD COLUMN cumulative_pnl REAL NOT NULL DEFAULT 0.0")
            print("Migration successful.")
        else:
            print("'cumulative_pnl' column already exists in 'open_trades'. No changes needed.")

        # --- Migration 2: Add 'high_water_marks' table for the live testnet bot ---
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='high_water_marks'")
        table_exists = cursor.fetchone()

        if not table_exists:
            print("Applying migration: Creating 'high_water_marks' table...")
            cursor.execute("""
                           CREATE TABLE high_water_marks
                           (
                               symbol      TEXT PRIMARY KEY,
                               highest_pnl REAL NOT NULL DEFAULT 0.0
                           )
                           """)
            print("Migration successful.")
        else:
            print("'high_water_marks' table already exists. No changes needed.")

        cursor.execute("PRAGMA table_info(pending_orders)")
        columns = [row['name'] for row in cursor.fetchall()]
        if 'created_at' not in columns:
            print("Applying migration: Adding 'created_at' column to 'pending_orders' table...")
            cursor.execute("ALTER TABLE pending_orders ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
            print("Migration successful.")
        else:
            print("'created_at' column already exists in 'pending_orders'. No changes needed.")

        conn.commit()


def init_db(initial_balance=1000.0, initial_leverage=20.0, initial_risk=50.0):
    """Initializes the database, creates tables if they don't exist, and runs migrations."""
    with get_db_connection() as conn:
        # This part creates the tables only if they don't exist at all
        conn.executescript(SCHEMA)

    # Run the migration to add new columns/tables to the existing structure
    migrate_database()

    with get_db_connection() as conn:
        # This part sets default settings only if they don't exist
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("balance", str(initial_balance))
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("leverage", str(initial_leverage))
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("risk_per_trade", str(initial_risk))
        )
        conn.commit()
    print("Database initialization and migration check complete.")


def get_setting(key):
    """Retrieves a setting value from the database."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row['value'] if row else None


def update_setting(key, value):
    """Updates a setting value in the database."""
    with get_db_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )
        conn.commit()


def add_trade(trade: PaperTrade):
    """Adds a new open trade to the database."""
    trade_dict = asdict(trade)
    trade_dict['tp_levels'] = json.dumps(trade_dict['tp_levels'])

    with get_db_connection() as conn:
        conn.execute(
            """INSERT INTO open_trades (trade_id, pair, entry_price, sl_price, initial_size, remaining_size, leverage,
                                        is_long, tp_levels, sl_moved_to_be, highest_pnl, cumulative_pnl)
               VALUES (:trade_id, :pair, :entry_price, :sl_price, :initial_size, :remaining_size, :leverage, :is_long,
                       :tp_levels, :sl_moved_to_be, :highest_pnl, :cumulative_pnl)""", # <-- Add :cumulative_pnl
            trade_dict
        )
        conn.commit()


def update_trade(trade: PaperTrade):
    """Updates an existing open trade in the database."""
    with get_db_connection() as conn:
        conn.execute(
            """UPDATE open_trades
               SET sl_price       = ?,
                   remaining_size = ?,
                   tp_levels      = ?,
                   sl_moved_to_be = ?,
                   highest_pnl    = ?,
                   cumulative_pnl = ?   -- <-- ADD THIS LINE
               WHERE trade_id = ?""",
            (trade.sl_price, trade.remaining_size, json.dumps(trade.tp_levels), trade.sl_moved_to_be,
             trade.highest_pnl, trade.cumulative_pnl, trade.trade_id) # <-- Add trade.cumulative_pnl
        )
        conn.commit()


def get_open_trades():
    """Retrieves all open trades and returns them as a list of PaperTrade objects."""
    trades = []
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM open_trades").fetchall()
        for row in rows:
            trade_data = dict(row)
            # Deserialize the tp_levels from a JSON string back into a list
            trade_data['tp_levels'] = json.loads(trade_data['tp_levels']) if trade_data['tp_levels'] else None
            trades.append(PaperTrade(**trade_data))
    return trades


def get_trade_by_id(trade_id: str):
    """Retrieves a single trade by its ID."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM open_trades WHERE trade_id = ?", (trade_id,)).fetchone()
        if not row:
            return None
        trade_data = dict(row)
        trade_data['tp_levels'] = json.loads(trade_data['tp_levels']) if trade_data['tp_levels'] else None
        return PaperTrade(**trade_data)


def close_trade(trade_id: str, status: str, exit_price: float, pnl: float):
    """Atomically moves a trade from 'open_trades' to 'trade_history'."""
    trade = get_trade_by_id(trade_id)
    if not trade:
        return  # Trade already closed or never existed

    with get_db_connection() as conn:
        # Start a transaction
        cursor = conn.cursor()
        try:
            # 1. Insert into history
            cursor.execute(
                """INSERT INTO trade_history (trade_id, pair, pnl, direction, entry_price, exit_price, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (trade.trade_id, trade.pair, pnl, "LONG" if trade.is_long else "SHORT", trade.entry_price, exit_price,
                 status)
            )
            # 2. Delete from open_trades
            cursor.execute("DELETE FROM open_trades WHERE trade_id = ?", (trade_id,))

            # 3. Commit the transaction
            conn.commit()
        except Exception as e:
            # If any step fails, roll back the entire transaction
            conn.rollback()
            print(f"Failed to close trade {trade_id}. Transaction rolled back. Error: {e}")


def get_trade_history():
    """Retrieves all closed trades from the history table."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM trade_history ORDER BY close_timestamp DESC").fetchall()
        # Return as a list of dictionaries for easy use in the web UI
        return [dict(row) for row in rows]

def update_high_water_mark(symbol: str, pnl: float):
    """Inserts or updates the highest PNL for a given symbol."""
    with get_db_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO high_water_marks (symbol, highest_pnl) VALUES (?, ?)",
            (symbol, pnl)
        )
        conn.commit()

def get_high_water_marks():
    """Retrieves all high-water marks as a dictionary for easy lookup."""
    marks = {}
    with get_db_connection() as conn:
        rows = conn.execute("SELECT symbol, highest_pnl FROM high_water_marks").fetchall()
        for row in rows:
            marks[row['symbol']] = row['highest_pnl']
    return marks

def delete_high_water_mark(symbol: str):
    """Removes a high-water mark record when a trade is closed."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM high_water_marks WHERE symbol = ?", (symbol,))
        conn.commit()

def add_pending_order(order_id, pair, entry_price, sl_price, is_long, risk_setting, tp_logic):
    """Adds a new pending order to the database."""
    with get_db_connection() as conn:
        conn.execute(
            """INSERT INTO pending_orders (order_id, pair, entry_price, sl_price, is_long, risk_setting, tp_logic)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (order_id, pair, entry_price, sl_price, is_long, risk_setting, json.dumps(tp_logic))
        )
        conn.commit()

def get_pending_orders():
    """Retrieves all pending orders from the database."""
    orders = []
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM pending_orders").fetchall()
        for row in rows:
            order_data = dict(row)
            order_data['tp_logic'] = json.loads(order_data['tp_logic'])
            orders.append(order_data)
    return orders

def delete_pending_order(order_id: str):
    """Deletes a pending order once it has been filled or cancelled."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM pending_orders WHERE order_id = ?", (order_id,))
        conn.commit()