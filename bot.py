# telegram_bot_v2.py

import asyncio
import json
import logging
import re
import os
from uuid import uuid4
from dataclasses import dataclass, asdict
import json
from datetime import datetime, timedelta
import requests

import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from extract_price import extract_prices_from_image
import database as db

from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AUTHORIZED_USER_ID = os.getenv('AUTHORIZED_USER_ID')
INITIAL_BALANCE = 1000.00
POLL_INTERVAL_SECONDS = 3  # Check prices more frequently
MARKET_ORDER_TOLERANCE = 0.0025  # 0.25% tolerance for immediate market orders
PENNY_COIN_MCAP_THRESHOLD = 50000000  # $50 Million market cap
MAX_CONCURRENT_LONGS = 10
MAX_CONCURRENT_SHORTS = 10

# --- Define keywords for the smart filter ---
BUY_WORDS = {'buy', 'long', 'bullish', 'buying', 'bought', 'longed'}
SELL_WORDS = {'sell', 'short', 'bearish', 'selling', 'sold', 'shorted'}
CLOSE_WORDS = {'close', 'closing', 'closed'} # <-- ADD THIS
BLACKLISTED_COINS = {'ETH', 'BTC'}

# We only care about words that open a trade for this logic
ALL_KEYWORDS = BUY_WORDS.union(SELL_WORDS)

# --- Setup Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # Quieter HTTP logs
logger = logging.getLogger(__name__)

SYMBOL_ALIASES = {}

def load_symbol_aliases():
    """Loads the symbol alias map from a JSON file."""
    global SYMBOL_ALIASES
    try:
        with open("symbol_aliases.json", "r") as f:
            SYMBOL_ALIASES = json.load(f)
        logger.info(f"✅ Loaded {len(SYMBOL_ALIASES)} symbol aliases from symbol_aliases.json")
    except FileNotFoundError:
        logger.warning("⚠️ symbol_aliases.json not found. No symbol aliases will be used.")
    except json.JSONDecodeError:
        logger.error("❌ Could not decode symbol_aliases.json. Please check for syntax errors in the file.")

# ==============================================================================
#  Refactored PaperTrade Data Class
# ==============================================================================
@dataclass
class PaperTrade:
    """A data class to hold the state of a trade, now with multi-TP support."""
    trade_id: str
    pair: str
    entry_price: float
    sl_price: float
    initial_size: float  # The original size of the position
    remaining_size: float # The currently active size of the position
    leverage: float
    is_long: bool
    tp_levels: list = None  # Will be a list of dicts, e.g., [{'price': 123, 'status': 'pending'}]
    sl_moved_to_be: bool = False
    highest_pnl: float = 0.0
    cumulative_pnl: float = 0.0


# ==============================================================================
#  Global State & Exchange Instance
# ==============================================================================
app_state = {
    "balance": 0.0,
    "leverage": 0.0,
    "pending_confirmations": {} # This is still useful in-memory
}
# Use a single, shared exchange instance for efficiency
exchange = ccxt.binanceusdm()


async def safe_exchange_call(func, *args, **kwargs):
    """
    A wrapper to safely call a ccxt function with a retry mechanism.
    Handles common network errors and exchange downtime.
    """
    max_retries = 3
    retry_delay_seconds = 5  # Wait 5 seconds between retries

    for attempt in range(max_retries):
        try:
            # Await the function call with its arguments
            return await func(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as e:
            logger.warning(
                f"[Exchange Call Failed] Attempt {attempt + 1}/{max_retries}. "
                f"Error: {e}. Retrying in {retry_delay_seconds}s..."
            )
            if attempt + 1 == max_retries:
                logger.critical(f"All {max_retries} attempts to contact the exchange failed. Giving up.")
                return None  # Return None if all retries fail
            await asyncio.sleep(retry_delay_seconds)
        except Exception as e:
            logger.error(f"[Exchange Call] An unexpected and non-retriable error occurred: {e}", exc_info=True)
            return None # Do not retry on unknown errors

    return None

# ==============================================================================
#  The Async Market Monitor
# ==============================================================================
async def market_monitor(application: Application):
    logger.info("Market monitor started.")
    while True:
        try:
            # --- Get BOTH pending orders and open trades ---
            pending_orders = db.get_pending_orders()
            open_trades = db.get_open_trades()

            if not open_trades and not pending_orders:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Create a combined set of all unique pairs to watch
            pairs_to_watch = set(trade.pair for trade in open_trades) | set(order['pair'] for order in pending_orders)

            if not pairs_to_watch:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            tickers = await safe_exchange_call(exchange.fetch_tickers, list(pairs_to_watch))

            if not tickers:
                logger.critical("[Monitor] Could not fetch market data from exchange. It may be down. Pausing for 60s.")
                await application.bot.send_message(
                    chat_id=int(AUTHORIZED_USER_ID),
                    text="🚨 **CRITICAL: Market Monitor** 🚨\n\nCould not connect to Binance to check SL/TP for open trades. The exchange may be down for maintenance. Will keep retrying."
                )
                await asyncio.sleep(60)
                continue

            for order in pending_orders:
                try:
                    # SQLite stores timestamps as strings; we need to parse them into datetime objects.
                    # The format matches 'YYYY-MM-DD HH:MM:SS'.
                    created_at_str = order['created_at']
                    if not created_at_str:  # Safety check for old orders that might not have a timestamp
                        continue

                    created_at = datetime.strptime(created_at_str, '%Y-%m-%d %H:%M:%S')

                    # If the order is older than 24 hours, cancel it.
                    if datetime.now() - created_at > timedelta(hours=24):
                        logger.warning(
                            f"Pending order for {order['pair']} (ID: {order['order_id']}) has expired. Canceling.")

                        # Delete from the database
                        db.delete_pending_order(order['order_id'])

                        # Notify the user
                        await application.bot.send_message(
                            chat_id=int(AUTHORIZED_USER_ID),
                            text=f"⏰ **Order Expired** ⏰\n\nThe pending order for **{order['pair']}** was not filled within 24 hours and has been automatically canceled.",
                            parse_mode='Markdown'
                        )
                        # Skip the rest of the checks for this canceled order
                        continue

                except (ValueError, TypeError) as e:
                    logger.error(f"Could not parse timestamp for order {order['order_id']}: {e}")
                    continue

                standardized_symbol = order['pair'].replace("USDT", "/USDT:USDT")
                if standardized_symbol not in tickers:
                    continue

                current_price = float(tickers[standardized_symbol]['last'])
                entry_price = order['entry_price']

                # Check for fill condition
                should_fill = False
                if order['is_long'] and current_price >= entry_price:
                    should_fill = True
                elif not order['is_long'] and current_price <= entry_price:
                    should_fill = True

                if should_fill:
                    # Use our new function to handle the execution
                    await execute_filled_order(application, order)

            for trade in open_trades:
                # --- START OF THE DEFINITIVE FIX ---
                # Step 1: Convert the stored pair ('MITOUSDT') to the EXACT standardized format ('MITO/USDT:USDT').
                standardized_symbol = trade.pair.replace("USDT", "/USDT:USDT")

                # Step 2: Use the correct symbol to check if the ticker exists.
                if standardized_symbol not in tickers:
                    # This check will now pass.
                    continue

                # Step 3: Safely get the price using the correct key and cast it to a float.
                try:
                    current_price = float(tickers[standardized_symbol]['last'])
                except (ValueError, TypeError, KeyError):
                    logger.warning(f"Could not parse 'last' price from ticker data for {standardized_symbol}.")
                    continue
                # --- END OF THE DEFINITIVE FIX ---

                price_diff = current_price - trade.entry_price
                if not trade.is_long:
                    price_diff = -price_diff
                current_pnl = price_diff * trade.remaining_size

                # Compare and update if the current PNL is a new high
                if current_pnl > trade.highest_pnl:
                    logger.info(f"New peak P/L for paper trade {trade.pair}: ${current_pnl:.2f}")
                    trade.highest_pnl = current_pnl
                    db.update_trade(trade)  # Save the new high to the database
                # --- END OF NEW LOGIC ---

                # --- 1. Check for STOP LOSS hit ---
                if (trade.is_long and current_price <= trade.sl_price) or \
                        (not trade.is_long and current_price >= trade.sl_price):
                    await process_trade_closure(application, trade, "SL_HIT", trade.sl_price)
                    continue

                # --- 2. Check for PARTIAL TAKE PROFIT hits ---
                if trade.tp_levels:
                    for i, level in enumerate(trade.tp_levels):
                        if level['status'] == 'pending':
                            if (trade.is_long and current_price >= level['price']) or \
                                    (not trade.is_long and current_price <= level['price']):

                                await process_partial_tp_closure(application, trade, level, i)

                                # --- NEW HYBRID STOP-LOSS LOGIC ---
                                new_sl_price = None
                                notification_reason = ""

                                if i == 1 and not trade.sl_moved_to_be:
                                    new_sl_price = trade.entry_price
                                    trade.sl_moved_to_be = True
                                    notification_reason = "TP2 hit. Trade is now risk-free."
                                elif i > 1:
                                    new_sl_price = trade.tp_levels[i - 2]['price']
                                    notification_reason = f"TP{i + 1} hit. Trailing stop-loss updated."

                                if new_sl_price and new_sl_price != trade.sl_price:
                                    original_sl = trade.sl_price
                                    trade.sl_price = new_sl_price
                                    db.update_trade(trade)

                                    message = (
                                        f"✅ **Stop-Loss Updated for {trade.pair}** ✅\n\n"
                                        f"{notification_reason}\n\n"
                                        f"Original SL: `{original_sl}`\n"
                                        f"**New SL: `{trade.sl_price}`**"
                                    )
                                    await application.bot.send_message(
                                        chat_id=int(AUTHORIZED_USER_ID), text=message, parse_mode='Markdown'
                                    )
                                    logger.info(f"Moved SL for trade {trade.trade_id} to {trade.sl_price}. Reason: {notification_reason}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except ccxt.NetworkError as e:
            logger.error(f"[Monitor] Network error: {e}. Retrying in 30s.")
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"[Monitor] An unexpected error occurred: {e}", exc_info=True)
            await asyncio.sleep(15)


async def process_partial_tp_closure(application: Application, trade: PaperTrade, level: dict, level_index: int):
    """Handles the logic for a single partial take-profit hit."""

    # Close 10% of the ORIGINAL position size
    size_to_close = trade.initial_size / 10

    # Calculate PNL for this portion
    exit_price = level['price']
    price_diff = exit_price - trade.entry_price
    if not trade.is_long:
        price_diff = -price_diff

    pnl = price_diff * size_to_close



    # Update state
    current_balance = float(db.get_setting("balance"))
    new_balance = current_balance + pnl
    db.update_setting("balance", new_balance)

    trade.remaining_size -= size_to_close
    trade.cumulative_pnl += pnl  # <-- ADD THIS LINE to update the running total
    level['status'] = 'hit'

    is_fully_closed = (level_index == 9) or trade.remaining_size < 1e-8
    if is_fully_closed:
        # Pass the final cumulative PNL to the history
        db.close_trade(trade.trade_id, f"TP{level_index + 1}_FULL_CLOSE", level['price'],
                       trade.cumulative_pnl)  # <-- FIX
    else:
        db.update_trade(trade)

    # Prepare notification message
    result_text = f"🎯🎯🎯 PARTIAL TAKE PROFIT {level_index + 1}/10 🎯🎯🎯\n\n"
    message = (
        f"{result_text}"
        f"Trade: **{trade.pair}**\n"
        f"Closed **10%** of position at `{level['price']}`\n"
        f"Portion PNL: `${pnl:,.2f}`\n\n"
        f"**New Balance: `${new_balance:,.2f}`**\n"
        f"Remaining Size: `{trade.remaining_size:.4f}`"
    )

    if is_fully_closed:
        message += "\n\n**Position fully closed.**"

    await application.bot.send_message(
        chat_id=AUTHORIZED_USER_ID, text=message, parse_mode='Markdown'
    )
    logger.info(f"Partial TP {level_index + 1}/10 hit for trade {trade.trade_id}. PNL: {pnl:,.2f}")


async def process_trade_closure(application: Application, trade: PaperTrade, status: str, exit_price: float):
    price_diff = exit_price - trade.entry_price
    if not trade.is_long:
        price_diff = -price_diff

    # This is the PNL of the FINAL SEGMENT only
    final_segment_pnl = price_diff * trade.remaining_size

    # Update the balance with only the PNL from this final part
    current_balance = float(db.get_setting("balance"))
    new_balance = current_balance + final_segment_pnl
    db.update_setting("balance", new_balance)

    # Add the final part's PNL to the running total to get the grand total
    total_trade_pnl = trade.cumulative_pnl + final_segment_pnl

    # Save the GRAND TOTAL to the trade history
    db.close_trade(trade.trade_id, status, exit_price, total_trade_pnl)

    result_text = "❌ STOP LOSS ❌\n\n" if "SL_HIT" in status else "🔵 MANUAL CLOSE 🔵\n\n"
    message = (
        f"{result_text}"
        f"Trade Closed: **{trade.pair}**\n"
        f"Exit: `{exit_price}`\n"
        f"Total PNL: `${total_trade_pnl:,.2f}`\n\n" # <-- FIX: Use the correct total
        f"**New Balance: `${new_balance:,.2f}`**"
    )
    await application.bot.send_message(
        chat_id=int(AUTHORIZED_USER_ID), text=message, parse_mode='Markdown'
    )
    logger.info(f"Trade {trade.trade_id} closed. Total PNL: {total_trade_pnl:,.2f}. Recorded to history.")


async def close_trade_by_symbol(symbol: str, application: Application):
    """Finds an open trade by its symbol and closes it at market price."""
    trade_to_close = None

    open_trades = db.get_open_trades()
    for trade in open_trades:
        # FIX: The check should match the stored format, e.g., 'MITOUSDT'
        if trade.pair.startswith(symbol + 'USDT'):
            trade_to_close = trade
            break

    if not trade_to_close:
        await application.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text=f"⚠️ Received close command for **{symbol}**, but no open trade was found.",
            parse_mode='Markdown'
        )
        return

    # --- The rest of your logic here is correct ---
    try:
        ticker = await safe_exchange_call(exchange.fetch_ticker, trade_to_close.pair)

        if not ticker:
            # ... error handling
            return

        exit_price = float(ticker['last']) # Also good practice to cast to float here
        await process_trade_closure(application, trade_to_close, "MANUAL_CLOSE", exit_price)
        logger.info(f"Closed trade for {symbol} via channel command.")

    except Exception as e:
        # Generic catch-all for any other unexpected errors
        logger.error(f"An unexpected error occurred while closing trade for {symbol}: {e}")
        await application.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text=f"🚨 An unexpected error occurred trying to close **{symbol}**. Error: {e}",
            parse_mode='Markdown'
        )

async def set_leverage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /setleverage command to dynamically update leverage."""
    if update.message.from_user.id != int(AUTHORIZED_USER_ID):
        return  # Ignore commands from unauthorized users

    try:
        # Get the new leverage value from the command arguments
        new_leverage = float(context.args[0])
        if new_leverage < 1 or new_leverage > 125:
            await update.message.reply_text("⚠️ **Invalid Value:** Leverage must be between 1 and 125.")
            return

        # Update the setting in the database
        db.update_setting("leverage", new_leverage)

        # IMPORTANT: Update the in-memory state as well
        app_state["leverage"] = new_leverage

        logger.info(f"Leverage updated to {new_leverage}x by user command.")
        await update.message.reply_text(
            f"✅ **Leverage Updated** ✅\n\n"
            f"New leverage is now set to **{new_leverage}x** for all future trades.",
            parse_mode='Markdown'
        )

    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/setleverage <value>` (e.g., `/setleverage 20`)", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error in set_leverage_command: {e}", exc_info=True)
        await update.message.reply_text(f"An error occurred: {e}")


async def set_risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /setrisk command to dynamically update risk, supporting fixed $ and %."""
    if update.message.from_user.id != int(AUTHORIZED_USER_ID):
        return

    try:
        # Get the risk value as a string from the command arguments
        risk_input = context.args[0]
        current_balance = float(db.get_setting("balance"))
        risk_value_to_store = ""
        reply_message = ""

        # Check if the input is a percentage
        if risk_input.endswith('%'):
            try:
                percentage = float(risk_input[:-1]) # Remove '%' and convert
                if not (0 < percentage <= 100):
                    await update.message.reply_text("⚠️ **Invalid Value:** Percentage risk must be between 0 and 100.")
                    return

                risk_value_to_store = f"{percentage}%"
                calculated_risk_amount = (percentage / 100) * current_balance
                reply_message = (
                    f"✅ **Risk Updated** ✅\n\n"
                    f"New risk per trade is now **{percentage:.2f}%** of your balance.\n"
                    f"On your current balance, this is **${calculated_risk_amount:,.2f}**."
                )
            except ValueError:
                await update.message.reply_text("⚠️ **Invalid Format:** Please enter a valid number for the percentage (e.g., `5%`).")
                return
        # Otherwise, treat it as a fixed dollar amount
        else:
            try:
                new_risk = float(risk_input)
                if new_risk <= 0:
                    await update.message.reply_text("⚠️ **Invalid Value:** Risk must be a positive number.")
                    return
                if new_risk > current_balance:
                    await update.message.reply_text(f"⚠️ **Warning:** New risk `${new_risk:,.2f}` is higher than your current balance of `${current_balance:,.2f}`.")

                risk_value_to_store = str(new_risk)
                reply_message = (
                    f"✅ **Risk Updated** ✅\n\n"
                    f"New risk per trade is now fixed at **${new_risk:,.2f}**."
                )
            except ValueError:
                await update.message.reply_text("⚠️ **Invalid Format:** Please enter a valid number for the amount (e.g., `50`).")
                return

        # Save the validated value (either "50" or "5%") to the database
        db.update_setting("risk_per_trade", risk_value_to_store)

        logger.info(f"Risk per trade updated to '{risk_value_to_store}' by user command.")
        await update.message.reply_text(reply_message, parse_mode='Markdown')

    except IndexError:
        await update.message.reply_text(
            "**Usage:**\n"
            "- `/setrisk <dollar_amount>` (e.g., `/setrisk 50`)\n"
            "- `/setrisk <percentage>%` (e.g., `/setrisk 5%`)",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error in set_risk_command: {e}", exc_info=True)
        await update.message.reply_text(f"An error occurred: {e}")


async def close_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /close_by_symbol command."""
    if update.message.from_user.id != int(AUTHORIZED_USER_ID):
        return  # Ensure the command is coming from our monitor (logged in as us)

    try:
        symbol = context.args[0].upper()
        await close_trade_by_symbol(symbol, context.application)
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /close_by_symbol <SYMBOL> (e.g., /close_by_symbol BTC)")


# ==============================================================================
#  Telegram Handlers (Largely the same, but simplified trade creation)
# ==============================================================================
# Dummy handlers for commands you haven't implemented fully
async def placeholder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("This command is not yet implemented.")


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = app_state['balance']
    await update.message.reply_text(f"Current Balance: **${balance:,.2f}**", parse_mode='Markdown')


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_trades = db.get_open_trades()
    if not open_trades:
        await update.message.reply_text("No open positions.")
        return

    message = "**Open Positions:**\n\n"
    for trade in open_trades:
        direction = "LONG" if trade.is_long else "SHORT"
        final_tp = trade.tp_levels[-1]['price'] if trade.tp_levels else "N/A"
        message += f"- **{trade.pair}** ({direction})\n"
        message += f"  Entry: `{trade.entry_price}`, SL: `{trade.sl_price}`, Final TP: `{final_tp}`\n"
        message += f"  Initial Size: `{trade.initial_size:.4f}`, Remaining: `{trade.remaining_size:.4f}`\n\n"

    await update.message.reply_text(message, parse_mode='Markdown')


async def tplevels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the status of all TP levels for a specific open trade."""
    if update.message.from_user.id != int(AUTHORIZED_USER_ID):
        return  # Ignore commands from unauthorized users

    try:
        # Get the coin symbol from the command arguments
        symbol = context.args[0].upper()
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/tplevels <SYMBOL>` (e.g., `/tplevels AVAX`)", parse_mode='Markdown')
        return

    # Find the trade in the database
    open_trades = db.get_open_trades()
    target_trade = None
    for trade in open_trades:
        # Check if the trade's pair starts with the symbol (e.g., 'AVAXUSDT' starts with 'AVAX')
        if trade.pair.startswith(symbol + 'USDT'):
            target_trade = trade
            break

    if not target_trade:
        await update.message.reply_text(f"❌ No open trade found for **{symbol}**.", parse_mode='Markdown')
        return

    if not target_trade.tp_levels:
        await update.message.reply_text(f"⚠️ No Take Profit levels are set for the **{target_trade.pair}** trade.", parse_mode='Markdown')
        return

    # Format the message with the TP levels
    message = f"🎯 **Take Profit Status for {target_trade.pair}**\n\n"
    message += f"**Entry Price:** `{target_trade.entry_price}`\n"
    message += f"**Stop-Loss:** `{target_trade.sl_price}`\n\n"

    for i, level in enumerate(target_trade.tp_levels):
        status_emoji = "✅" if level['status'] == 'hit' else "⏰"
        price = level['price']
        status_text = level['status'].capitalize()

        message += f"{status_emoji} **TP {i + 1}:** `{price}` ({status_text})\n"

    await update.message.reply_text(message, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays a list of all available commands."""
    if update.message.from_user.id != int(AUTHORIZED_USER_ID):
        return

    help_text = (
        "**Trading Bot Commands**\n\n"
        "**General:**\n"
        "`/help` - Shows this help message.\n"
        "`/balance` - Displays your current account balance.\n\n"
        "**Trade Management:**\n"
        "`/positions` - Shows all open positions.\n"
        "`/tplevels <SYMBOL>` - Shows the TP status for an open trade (e.g., `/tplevels BTC`).\n"
        "`/close_by_symbol <SYMBOL>` - Manually closes an open trade at market price.\n\n"
        "**Pending Order Management:**\n"
        "`/pending` - Lists all pending orders waiting to be filled.\n"
        "`/cancel <SYMBOL or ID>` - Cancels a pending order (e.g., `/cancel BTC` or `/cancel <order_id>`).\n\n"
        "**Settings:**\n"
        "`/setleverage <value>` - Sets the leverage for future trades (e.g., `/setleverage 20`).\n"
        "`/setrisk <amount>` - Sets risk as a fixed amount (e.g., `/setrisk 50`) or percentage (e.g., `/setrisk 1.5%`)."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays all pending orders that have not yet been filled."""
    if update.message.from_user.id != int(AUTHORIZED_USER_ID):
        return

    pending_orders = db.get_pending_orders()

    if not pending_orders:
        await update.message.reply_text("No pending orders.")
        return

    message = "**Pending Orders:**\n\n"
    for order in pending_orders:
        direction = "LONG" if order['is_long'] else "SHORT"
        message += f"- **{order['pair']}** ({direction})\n"
        message += f"  Entry: `{order['entry_price']}`, SL: `{order['sl_price']}`\n"
        # Display the first few characters of the ID for easy cancellation
        message += f"  ID: `{order['order_id'][:8]}...`\n\n"

    await update.message.reply_text(message, parse_mode='Markdown')


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels a pending order by its symbol or ID."""
    if update.message.from_user.id != int(AUTHORIZED_USER_ID):
        return

    try:
        identifier = context.args[0].upper()
    except IndexError:
        await update.message.reply_text("Usage: `/cancel <SYMBOL or Order ID>`")
        return

    pending_orders = db.get_pending_orders()
    order_to_cancel = None

    # Search for the order by symbol or partial/full ID
    for order in pending_orders:
        pair_symbol = order['pair'].replace("USDT", "")
        if pair_symbol == identifier or order['order_id'].upper().startswith(identifier):
            order_to_cancel = order
            break

    if order_to_cancel:
        db.delete_pending_order(order_to_cancel['order_id'])
        logger.info(f"Canceled pending order for {order_to_cancel['pair']} by user command.")
        await update.message.reply_text(
            f"✅ **Pending order for {order_to_cancel['pair']} has been canceled.**"
        )
    else:
        await update.message.reply_text(
            f"⚠️ No pending order found with the symbol or ID: **{identifier}**"
        )


async def create_pending_order(update: Update, context: ContextTypes.DEFAULT_TYPE, trading_pair: str,
                               photo_file_id: str):
    """
    Acts as a router. Analyzes a signal and decides whether to execute a trade
    immediately (market order) or create a pending limit order based on the
    coin's market cap and proximity to the entry price.
    """
    try:
        # --- Stage 1: Standard Signal Analysis (same as before) ---
        photo_file = await context.bot.get_file(photo_file_id)
        image_path = f"temp_{photo_file.file_id}.jpg"
        await photo_file.download_to_drive(image_path)
        extracted = extract_prices_from_image(image_path)
        os.remove(image_path)

        if not all(k in extracted for k in ['entry', 'stoploss']):
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                           text=f"❌ **Analysis Failed:** Missing entry/SL for {trading_pair}.")
            return

        entry = extracted['entry']
        sl = extracted['stoploss']
        is_long = sl < entry  # Determine the new signal's direction

        # --- START OF NEW GATEKEEPER LOGIC ---
        # Check the current number of open positions before proceeding.
        open_trades = db.get_open_trades()
        long_count = sum(1 for trade in open_trades if trade.is_long)
        short_count = sum(1 for trade in open_trades if not trade.is_long)

        if is_long and long_count >= MAX_CONCURRENT_LONGS:
            logger.warning(
                f"Signal for {trading_pair} ignored. Max concurrent LONG positions ({MAX_CONCURRENT_LONGS}) reached.")
            await context.bot.send_message(
                chat_id=int(AUTHORIZED_USER_ID),
                text=f"⚠️ **Signal Ignored: {trading_pair} (LONG)**\n\nThe maximum number of open LONG positions ({MAX_CONCURRENT_LONGS}) has been reached.",
                parse_mode='Markdown'
            )
            return  # Stop processing this signal

        elif not is_long and short_count >= MAX_CONCURRENT_SHORTS:
            logger.warning(
                f"Signal for {trading_pair} ignored. Max concurrent SHORT positions ({MAX_CONCURRENT_SHORTS}) reached.")
            await context.bot.send_message(
                chat_id=int(AUTHORIZED_USER_ID),
                text=f"⚠️ **Signal Ignored: {trading_pair} (SHORT)**\n\nThe maximum number of open SHORT positions ({MAX_CONCURRENT_SHORTS}) has been reached.",
                parse_mode='Markdown'
            )
            return  # Stop processing this signal


        tp = extracted.get('target')
        pair_tag = trading_pair.replace("USDT", "")

        # --- Stage 2: Check if it's a Penny Coin ---
        market_cap = get_market_cap(pair_tag)
        is_penny_trade = market_cap is not None and market_cap < PENNY_COIN_MCAP_THRESHOLD

        if is_penny_trade:
            logger.info(
                f"{trading_pair} identified as a penny coin (MCap: ${market_cap:,.0f}). Applying penny strategy.")
        else:
            logger.info(f"{trading_pair} is not a penny coin (MCap: ${market_cap:,.0f}). Applying standard strategy.")

        # --- Stage 3: The ROUTING Logic ---
        # Determine the TP logic based on the coin type
        if is_penny_trade:
            # For penny coins, use a 0.2R target
            tp_logic_data = {'type': 'rr', 'value': 0.2}
        else:
            # For everything else, use the standard logic
            tp_logic_data = {'type': 'rr', 'value': 10.0}  # Default to 10R
            if tp and ((is_long and tp > entry) or (not is_long and tp < entry)):
                tp_logic_data = {'type': 'target', 'value': tp}

        # For penny coins, check if we should do a market order
        if is_penny_trade:
            ticker = await safe_exchange_call(exchange.fetch_ticker, trading_pair)
            if ticker and ticker.get('last'):
                current_price = float(ticker['last'])
                price_diff_percent = abs(current_price - entry) / entry

                if price_diff_percent <= MARKET_ORDER_TOLERANCE:
                    # --- ROUTE 1: IMMEDIATE MARKET EXECUTION ---
                    logger.info(
                        f"Price is within tolerance ({price_diff_percent:.4%}). Executing market order for {trading_pair}.")
                    await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                                   text=f"⚡️ **Penny Coin Market Order** ⚡️\n\nPrice for **{trading_pair}** is close to entry. Filling immediately...")

                    # We create a "dummy" order dictionary and pass it directly to the execution function
                    dummy_order = {
                        "order_id": f"market_{str(uuid4())}",
                        "pair": trading_pair, "entry_price": current_price,  # Use current price for market order
                        "sl_price": sl, "is_long": is_long,
                        "risk_setting": db.get_setting('risk_per_trade'),
                        "tp_logic": tp_logic_data
                    }
                    await execute_filled_order(context.application, dummy_order, is_market_order=True)
                    return  # Stop here, the trade is done

        # --- ROUTE 2: PENDING LIMIT ORDER (Default for non-penny coins or penny coins outside the price tolerance) ---
        logger.info(f"Price is outside tolerance or it's a standard coin. Creating pending order for {trading_pair}.")
        order_id = str(uuid4())
        db.add_pending_order(
            order_id=order_id, pair=trading_pair, entry_price=entry,
            sl_price=sl, is_long=is_long,
            risk_setting=db.get_setting('risk_per_trade'),
            tp_logic=tp_logic_data
        )
        direction = "LONG" if is_long else "SHORT"
        await context.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text=f"⏳ **Pending Order Created: {trading_pair}** ({direction})\n\n"
                 f"🔹 **Entry:** `{entry}`\n🔹 **Stop-Loss:** `{sl}`\n"
                 f"The bot will fill this order when the entry price is reached.",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Error in create_pending_order: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text=f"A critical error occurred while trying to create the pending order: {e}"
        )


def get_market_cap(symbol: str) -> float | None:
    """
    Fetches the market cap for a given crypto symbol from the CoinGecko API.
    Returns the market cap as a float, or None if not found.
    """
    # CoinGecko's API is often slow to list brand new coins.
    # We use a cache to avoid spamming the API for the same symbols repeatedly.
    if 'coingecko_cache' not in app_state:
        app_state['coingecko_cache'] = {}

    if symbol in app_state['coingecko_cache']:
        return app_state['coingecko_cache'][symbol]

    try:
        # 1. First, we need to find the coin's 'id' on CoinGecko (e.g., 'bitcoin', 'aster')
        search_url = f"https://api.coingecko.com/api/v3/search?query={symbol}"
        response = requests.get(search_url, timeout=5)
        response.raise_for_status()
        search_data = response.json()

        if not search_data['coins']:
            logger.warning(f"[CoinGecko] Could not find a coin ID for symbol '{symbol}'")
            return None

        # Assume the first result is the correct one
        coin_id = search_data['coins'][0]['id']

        # 2. Now, get the detailed market data using the id
        market_url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_market_cap=true"
        response = requests.get(market_url, timeout=5)
        response.raise_for_status()
        market_data = response.json()

        market_cap = market_data.get(coin_id, {}).get('usd_market_cap')

        if market_cap is not None:
            logger.info(f"[CoinGecko] Fetched market cap for {symbol} ({coin_id}): ${market_cap:,.2f}")
            app_state['coingecko_cache'][symbol] = market_cap  # Cache the result
            return float(market_cap)
        else:
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"[CoinGecko] API Error fetching market cap for {symbol}: {e}")
        return None
    except (KeyError, IndexError):
        logger.error(f"[CoinGecko] Could not parse API response for {symbol}.")
        return None


async def execute_filled_order(application: Application, pending_order: dict, is_market_order: bool = False):
    """Takes a pending order that has been triggered (or a dummy market order) and executes the paper trade."""
    try:
        logger.info(f"Executing filled order for {pending_order['pair']}")

        # --- Sizing logic (same as before) ---
        balance = float(db.get_setting('balance'))
        leverage = float(db.get_setting('leverage'))
        risk_setting = pending_order['risk_setting']
        entry = pending_order['entry_price']
        sl = pending_order['sl_price']
        is_long = pending_order['is_long']
        trading_pair = pending_order['pair']

        risk_per_trade = 0.0
        if '%' in risk_setting:
            risk_per_trade = (float(risk_setting.strip('%')) / 100) * balance
        else:
            risk_per_trade = float(risk_setting)

        stop_loss_distance = abs(entry - sl)
        position_size_asset = risk_per_trade / stop_loss_distance
        position_size_usd = position_size_asset * entry

        # --- MODIFIED TP Level Calculation ---
        tp_logic = pending_order['tp_logic']
        calculated_tp_levels = []

        # Check if the risk-reward value is less than 1 (our penny coin strategy)
        if tp_logic['type'] == 'rr' and tp_logic['value'] < 1.0:
            # --- Penny Coin Logic: Single TP at 0.2R ---
            rr_value = tp_logic['value']
            profit_distance = stop_loss_distance * rr_value
            tp_price = entry + profit_distance if is_long else entry - profit_distance
            calculated_tp_levels = [{"price": tp_price, "status": "pending"}]
            logger.info(f"Applying single TP strategy for penny coin. TP set at {tp_price}")
        else:
            # --- Standard Logic: 10 Partial TPs ---
            if tp_logic['type'] == 'target':
                tp = tp_logic['value']
                total_profit_range = abs(tp - entry)
                step_size = total_profit_range / 10
            else:  # Default to RR
                FINAL_RR = tp_logic['value']
                total_profit_range = stop_loss_distance * FINAL_RR
                step_size = total_profit_range / 10
            calculated_tp_levels = [
                {"price": entry + (step_size * i) if is_long else entry - (step_size * i), "status": "pending"} for i in
                range(1, 11)]

        # --- Create and Save the Trade ---
        trade = PaperTrade(
            trade_id=str(uuid4()), pair=trading_pair, entry_price=entry, sl_price=sl,
            initial_size=position_size_asset, remaining_size=position_size_asset,
            leverage=leverage, is_long=is_long, tp_levels=calculated_tp_levels
        )
        db.add_trade(trade)

        # Only delete from pending if it was a real limit order
        if not is_market_order:
            db.delete_pending_order(pending_order['order_id'])

        # --- Send Notification ---
        direction = "LONG" if is_long else "SHORT"
        final_tp_price = calculated_tp_levels[-1]['price']
        order_type_text = "Market Order Filled" if is_market_order else "Pending Order Filled"

        await application.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text=f"✅ **{order_type_text} & Opened for {trading_pair}** ({direction})\n\n"
                 f"Position Value: `${position_size_usd:,.2f}`\n"
                 f"Entry: `{entry:.4f}`\nStop-Loss: `{sl}`\n"
                 f"Final TP: `{final_tp_price:.4f}`\n\n"
                 f"New Balance: `${balance:,.2f}`",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"CRITICAL: Failed to execute filled order. Error: {e}", exc_info=True)


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles incoming signals with new REVERSAL logic.
    Closes an existing trade if a new signal for the same pair has the opposite direction.
    """
    message = update.message
    if message.from_user.id != int(AUTHORIZED_USER_ID):
        return

    if not message.photo or not message.caption:
        return

    caption = message.caption
    match = re.search(r'#(\w+)', caption)
    if not match:
        return

    pair_tag = match.group(1).upper()

    # --- NEW: Symbol Alias Correction Logic ---
    lookup_key = pair_tag.lower()  # Look up using a consistent lowercase key
    if lookup_key in SYMBOL_ALIASES:
        original_tag = pair_tag
        pair_tag = SYMBOL_ALIASES[lookup_key].upper()  # Replace with the correct value
        logger.info(f"Symbol alias applied: Corrected signal for #{original_tag} to #{pair_tag}")

    if pair_tag in BLACKLISTED_COINS:
        logger.warning(f"Signal for #{pair_tag} ignored because it is on the blacklist.")
        return

    trading_pair = f"{pair_tag}USDT"

    # --- NEW REVERSAL LOGIC ---

    # 1. Preliminary Price Extraction
    # We must process the image first to determine the new signal's direction.
    photo_file_id = message.photo[-1].file_id
    photo_file = await context.bot.get_file(photo_file_id)
    # Use a temporary, unique filename to avoid conflicts
    image_path = f"temp_{photo_file.file_id}.jpg"
    await photo_file.download_to_drive(image_path)
    extracted_prices = extract_prices_from_image(image_path)
    os.remove(image_path)  # Clean up the temp file immediately

    if not all(k in extracted_prices for k in ['entry', 'stoploss']):
        await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                       text=f"⚠️ Signal for **{trading_pair}** ignored. Could not extract entry/SL for pre-analysis.")
        return

    new_signal_is_long = extracted_prices['stoploss'] < extracted_prices['entry']

    # 2. Find Existing Trade
    # Check if a trade for this pair already exists in the database.
    existing_trade = None
    open_trades = db.get_open_trades()
    for trade in open_trades:
        if trade.pair == trading_pair:
            existing_trade = trade
            break

    # 3. The Core Reversal Decision
    if existing_trade:
        # Scenario A: The directions are DIFFERENT (a true reversal)
        if existing_trade.is_long != new_signal_is_long:
            new_direction_text = "LONG" if new_signal_is_long else "SHORT"
            old_direction_text = "SHORT" if new_signal_is_long else "LONG"

            logger.info(f"Reversal signal for {trading_pair} detected. Closing existing {old_direction_text} position.")
            await context.bot.send_message(
                chat_id=int(AUTHORIZED_USER_ID),
                text=f"⤵️ **Reversal Signal:** Closing existing trade on **{trading_pair}** to open new {new_direction_text} position."
            )
            # Close the existing trade at market price
            await close_trade_by_symbol(pair_tag, context.application)
            # IMPORTANT: We DO NOT return here. We let the function continue to open the new trade.

        # Scenario B: The directions are the SAME
        else:
            logger.warning(f"Signal for {trading_pair} ignored. A trade in the same direction is already open.")
            await context.bot.send_message(
                chat_id=int(AUTHORIZED_USER_ID),
                text=f"⚠️ **Signal Ignored:** A position for **{trading_pair}** in the same direction is already open."
            )
            return  # Exit the function completely.

    # --- END OF NEW LOGIC ---

    # 4. Proceed as Normal
    # The rest of the function continues only if it's a new trade or a reversal.
    try:
        await exchange.load_markets(True)
        market = exchange.market(trading_pair)
        if not market.get('swap'):
            logger.error(f"Signal ignored. Pair '{trading_pair}' exists but is not a SWAP/FUTURES contract.")
            return

    except ccxt.BadSymbol:
        logger.error(f"Signal ignored. Pair '{trading_pair}' is not a valid FUTURES symbol on Binance.")
        return
    except Exception as e:
        logger.error(f"Error validating pair with exchange: {e}", exc_info=True)
        await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                       text=f"An error occurred while validating the trading pair: {e}")
        return

    # --- The "Cleanliness" Check ---
    clean_caption = caption.lower()
    for word in ALL_KEYWORDS:
        clean_caption = clean_caption.replace(word, '')
    clean_caption = re.sub(r'#\w+', '', clean_caption)

    # --- Routing Logic ---
    if not clean_caption.strip():
        logger.info("Clean signal detected. Executing trade automatically.")
        # Note: execute_trade will re-download and re-process the image, which is perfectly fine.
        await create_pending_order(update, context, trading_pair, photo_file_id)
    else:
        # ... (The logic for asking for confirmation on complex signals remains the same)
        request_id = str(uuid4())
        fwd_message = await message.forward(chat_id=int(AUTHORIZED_USER_ID))
        keyboard = [[
            InlineKeyboardButton("✅ Confirm Trade", callback_data=f"confirm_trade|{request_id}"),
            InlineKeyboardButton("❌ Ignore", callback_data=f"ignore_trade|{request_id}"),
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        confirmation_message = await context.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text="This signal contains extra text. Please confirm to proceed:",
            reply_markup=reply_markup,
            reply_to_message_id=fwd_message.message_id
        )
        pending_request = {
            "trading_pair": trading_pair,
            "photo_file_id": photo_file_id,
            "confirmation_message_id": confirmation_message.message_id
        }
        app_state["pending_confirmations"][request_id] = pending_request
        logger.info(f"Saved pending confirmation with ID: {request_id}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all confirmation button presses using persistent state."""
    query = update.callback_query
    await query.answer()

    try:
        action, request_id = query.data.split('|')
        pending_requests = app_state.get("pending_confirmations", {})

        if request_id not in pending_requests:
            await query.edit_message_text(text="⚠️ This trade confirmation has expired or was already processed.")
            return

        # Retrieve the details from our saved state
        request_data = pending_requests[request_id]
        trading_pair = request_data["trading_pair"]
        photo_file_id = request_data["photo_file_id"]

        if action == "confirm_trade":
            await query.edit_message_text(text=f"✅ Confirmation received. Opening trade for {trading_pair}...")
            # The core action is the same
            await create_pending_order(update, context, trading_pair, photo_file_id)

        elif action == "ignore_trade":
            await query.edit_message_text(text="❌ Signal ignored.")

    finally:
        # --- CLEANUP: No matter what, remove the request from the state ---
        # This prevents dangling or double-processed requests.
        action, request_id = query.data.split('|') # Re-split to ensure we have the ID
        if request_id in app_state.get("pending_confirmations", {}):
            del app_state["pending_confirmations"][request_id]
            logger.info(f"Processed and removed pending confirmation ID: {request_id}")



# ==============================================================================
# Main Bot Execution
# ==============================================================================
async def main():
    """Initializes and runs the bot and all background tasks."""
    db.init_db(INITIAL_BALANCE)
    load_symbol_aliases()

    # Load initial state from the database into the in-memory app_state
    app_state["balance"] = float(db.get_setting("balance"))
    app_state["leverage"] = float(db.get_setting("leverage"))
    logger.info(f"State loaded from DB. Balance: ${app_state['balance']:.2f}")

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(10)
        .read_timeout(20)
        .build()
    )

    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("positions", positions_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("tplevels", tplevels_command))
    application.add_handler(CommandHandler("setleverage", set_leverage_command))
    application.add_handler(CommandHandler("setrisk", set_risk_command))
    application.add_handler(CommandHandler("close_by_symbol", close_command_handler))

    # Message and button handlers
    application.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION, message_handler))
    application.add_handler(CallbackQueryHandler(button_handler))

    # --- This is the new, non-blocking way to run the bot ---
    try:
        print("Initializing bot...")
        await application.initialize()  # Prepares the application

        # Create the background task for the market monitor
        # Do this *after* initializing the application
        asyncio.create_task(market_monitor(application))

        print("Starting bot polling...")
        await application.start()  # Starts fetching updates from Telegram
        await application.updater.start_polling()  # Starts the polling loop

        print("Bot is running! Press Ctrl-C to stop.")

        # Keep the script running forever, or until Ctrl-C is pressed
        while True:
            await asyncio.sleep(3600)  # Sleep for a long time

    finally:
        print("Shutting down bot...")
        # Gracefully stop the components in reverse order
        if application.updater and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await exchange.close()
        print("Bot shut down gracefully.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")