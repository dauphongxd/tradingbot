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
         """


def get_db_connection():
    """Establishes a connection to the database."""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row  # Allows accessing columns by name
    return conn


def init_db(initial_balance=1000.0, initial_leverage=20.0, initial_risk=50.0): # <-- Add initial_risk
    """Initializes the database tables and default settings."""
    with get_db_connection() as conn:
        conn.executescript(SCHEMA)
        # Set default balance only if it doesn't exist
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("balance", str(initial_balance))
        )
        # Set default leverage only if it doesn't exist
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("leverage", str(initial_leverage))
        )
        # --- ADD THIS BLOCK ---
        # Set default risk per trade only if it doesn't exist
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("risk_per_trade", str(initial_risk))
        )
        # --- END ADDITION ---
        conn.commit()
    print("Database initialized successfully.")


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
    # SQLite doesn't have a list/dict type, so we store tp_levels as a JSON string
    trade_dict['tp_levels'] = json.dumps(trade_dict['tp_levels'])

    with get_db_connection() as conn:
        conn.execute(
            """INSERT INTO open_trades (trade_id, pair, entry_price, sl_price, initial_size, remaining_size, leverage,
                                        is_long, tp_levels, sl_moved_to_be)
               VALUES (:trade_id, :pair, :entry_price, :sl_price, :initial_size, :remaining_size, :leverage, :is_long,
                       :tp_levels, :sl_moved_to_be)""",
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
                   sl_moved_to_be = ?
               WHERE trade_id = ?""",
            (trade.sl_price, trade.remaining_size, json.dumps(trade.tp_levels), trade.sl_moved_to_be, trade.trade_id)
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