# telegram_bot_v2.py

import asyncio
import json
import logging
import re
import os
from uuid import uuid4
from dataclasses import dataclass, asdict

import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from extract_price import extract_prices_from_image

from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AUTHORIZED_USER_ID = os.getenv('AUTHORIZED_USER_ID')
INITIAL_BALANCE = 1000.00
RISK_PER_TRADE = 50.00  # This is the dollar amount to risk, not the position size
DATA_FILE = "trading_data.json"
POLL_INTERVAL_SECONDS = 3  # Check prices more frequently

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


# ==============================================================================
#  Global State & Exchange Instance
# ==============================================================================
app_state = {
    "balance": INITIAL_BALANCE,
    "leverage": 20.0,
    "open_trades": {},  # We will store PaperTrade objects here
    "trade_history": [],
    "pending_confirmations": {}
}
# Use a single, shared exchange instance for efficiency
exchange = ccxt.binanceusdm()


# ==============================================================================
#  State Management (No changes needed here)
# ==============================================================================
def save_state():
    """Saves the full current state (balance, leverage, and open trades) to the data file."""
    with open(DATA_FILE, 'w') as f:
        trades_to_save = {trade_id: asdict(trade) for trade_id, trade in app_state["open_trades"].items()}

        state_to_save = {
            "balance": app_state["balance"],
            "leverage": app_state["leverage"],
            "open_trades": trades_to_save,
            "trade_history": app_state["trade_history"],
            # --- NEW LINE ---
            "pending_confirmations": app_state.get("pending_confirmations", {})
        }
        json.dump(state_to_save, f, indent=4)


def load_state():
    """Loads the full state from the data file if it exists."""
    global app_state
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            try:
                data = json.load(f)
                app_state["balance"] = data.get("balance", INITIAL_BALANCE)
                app_state["leverage"] = data.get("leverage", 20.0)

                # Recreate the PaperTrade objects from the loaded data
                loaded_trades = data.get("open_trades", {})
                app_state["open_trades"] = {
                    trade_id: PaperTrade(**trade_data)
                    for trade_id, trade_data in loaded_trades.items()
                }

                app_state["trade_history"] = data.get("trade_history", [])

                app_state["pending_confirmations"] = data.get("pending_confirmations", {})

                trade_count = len(app_state["open_trades"])
                pending_count = len(app_state["pending_confirmations"])
                logger.info(
                    f"State loaded. Balance: ${app_state['balance']:.2f}, "
                    f"Open Trades: {trade_count}, Pending Confirmations: {pending_count}"
                )
            except (json.JSONDecodeError, TypeError) as e:
                logger.error(
                    f"Could not load state from {DATA_FILE}. It might be corrupted. Starting fresh. Error: {e}")


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
    """The 'control tower' that now checks for SL, manual closures, and partial TPs."""
    # ... (The first part of the function reloading state from the file is the same) ...
    logger.info("Market monitor started.")
    while True:
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r') as f:
                    try:
                        data = json.load(f)
                        loaded_trades = data.get("open_trades", {})
                        app_state["open_trades"] = {
                            trade_id: PaperTrade(**trade_data)
                            for trade_id, trade_data in loaded_trades.items()
                        }
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(f"Could not parse {DATA_FILE}, state may be out of sync.")
            open_trades = list(app_state["open_trades"].values())
            if not open_trades:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            pairs_to_watch = list(set(trade.pair for trade in open_trades))
            if not pairs_to_watch:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            tickers = await safe_exchange_call(exchange.fetch_tickers, pairs_to_watch)

            # If tickers is None, it means all retries failed.
            if not tickers:
                logger.critical("[Monitor] Could not fetch market data from exchange. It may be down. Pausing for 60s.")
                # Optional: Send an alert to the user that the monitor is struggling
                await application.bot.send_message(
                    chat_id=AUTHORIZED_USER_ID,
                    text="🚨 **CRITICAL: Market Monitor** 🚨\n\nCould not connect to Binance to check SL/TP for open trades. The exchange may be down for maintenance. Will keep retrying."
                )
                await asyncio.sleep(60)  # Wait a longer time before the next full loop
                continue  # Skip the rest of this loop iteration


            for trade in open_trades:
                if trade.pair not in tickers: continue
                current_price = tickers[trade.pair]['last']
                exit_price = None
                status = None

                # --- 1. Check for PARTIAL TAKE PROFIT hits ---
                if trade.tp_levels:
                    for i, level in enumerate(trade.tp_levels):
                        if level['status'] == 'pending':
                            # Check if the next pending TP level is hit
                            if (trade.is_long and current_price >= level['price']) or \
                               (not trade.is_long and current_price <= level['price']):
                                await process_partial_tp_closure(application, trade, level, i)
                            break # IMPORTANT: Only check the very next pending level in each loop

                if not trade.sl_moved_to_be and trade.tp_levels:
                    # Check if the 5th TP level (index 4) has been hit
                    # We also check length to prevent an IndexError
                    if len(trade.tp_levels) > 4 and trade.tp_levels[4]['status'] == 'hit':
                        original_sl = trade.sl_price
                        # Move SL to the entry price
                        trade.sl_price = trade.entry_price
                        # Set the flag to True so this doesn't run again for this trade
                        trade.sl_moved_to_be = True

                        # Save the state immediately to make the new SL persistent
                        save_state()

                        # Send a notification to the user
                        message = (
                            f"✅ **Stop-Loss Updated for {trade.pair}** ✅\n\n"
                            f"TP5 was hit. The trade is now risk-free.\n\n"
                            f"Original SL: `{original_sl}`\n"
                            f"**New SL: `{trade.sl_price}`** (Break-Even)"
                        )
                        await application.bot.send_message(
                            chat_id=AUTHORIZED_USER_ID, text=message, parse_mode='Markdown'
                        )
                        logger.info(f"Moved SL for trade {trade.trade_id} to break-even at {trade.sl_price}.")

                # --- 2. Check for STOP LOSS hit ---
                # The trade might have been fully closed by the last TP, so we check if it still exists
                if trade.trade_id in app_state["open_trades"]:
                    if (trade.is_long and current_price <= trade.sl_price) or \
                       (not trade.is_long and current_price >= trade.sl_price):
                        status = "SL_HIT"
                        exit_price = trade.sl_price
                    if status:
                        await process_trade_closure(application, trade, status, exit_price)
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
    app_state["balance"] += pnl
    trade.remaining_size -= size_to_close
    level['status'] = 'hit'  # Mark this level as completed

    # If this was the last TP, the trade is fully closed
    is_fully_closed = (level_index == 9)  # 9 because index is 0-based
    if is_fully_closed or trade.remaining_size < 1e-8:  # Use a small threshold for float comparison
        if trade.trade_id in app_state["open_trades"]:
            del app_state["open_trades"][trade.trade_id]

    save_state()

    # Prepare notification message
    result_text = f"🎯🎯🎯 PARTIAL TAKE PROFIT {level_index + 1}/10 🎯🎯🎯\n\n"
    message = (
        f"{result_text}"
        f"Trade: **{trade.pair}**\n"
        f"Closed **10%** of position at `{exit_price}`\n"
        f"Portion PNL: `${pnl:,.2f}`\n\n"
        f"**New Balance: `${app_state['balance']:,.2f}`**\n"
        f"Remaining Size: `{trade.remaining_size:.4f}`"
    )

    if is_fully_closed:
        message += "\n\n**Position fully closed.**"

    await application.bot.send_message(
        chat_id=AUTHORIZED_USER_ID, text=message, parse_mode='Markdown'
    )
    logger.info(f"Partial TP {level_index + 1}/10 hit for trade {trade.trade_id}. PNL: {pnl:,.2f}")


async def process_trade_closure(application: Application, trade: PaperTrade, status: str, exit_price: float):
    """Handles the logic for a FULL trade closure and records it to history."""
    price_diff = exit_price - trade.entry_price
    if not trade.is_long:
        price_diff = -price_diff

    pnl = price_diff * trade.remaining_size
    app_state["balance"] += pnl

    # --- NEW: Create a history record before deleting the trade ---
    history_record = {
        "pair": trade.pair,
        "pnl": pnl,
        "direction": "LONG" if trade.is_long else "SHORT",
        "entry_price": trade.entry_price,
        "exit_price": exit_price,
        "status": status  # e.g., "SL_HIT", "MANUAL_CLOSE"
    }
    app_state["trade_history"].append(history_record)
    # --- END NEW ---

    if trade.trade_id in app_state["open_trades"]:
        del app_state["open_trades"][trade.trade_id]

    save_state()  # This now saves the history too

    result_text = "❌ STOP LOSS ❌\n\n" if "SL_HIT" in status else "🔵 MANUAL CLOSE 🔵\n\n"
    message = (
        f"{result_text}"
        f"Trade Closed: **{trade.pair}**\n"
        f"Exit: `{exit_price}`\n"
        f"PNL: `${pnl:,.2f}`\n\n"
        f"**New Balance: `${app_state['balance']:,.2f}`**"
    )
    await application.bot.send_message(
        chat_id=int(AUTHORIZED_USER_ID), text=message, parse_mode='Markdown'
    )
    logger.info(f"Trade {trade.trade_id} closed. PNL: {pnl:,.2f}. Recorded to history.")


async def close_trade_by_symbol(symbol: str, application: Application):
    """Finds an open trade by its symbol and closes it at market price."""
    trade_to_close = None
    trade_id_to_close = None

    # Find the trade in our app_state
    for trade_id, trade in app_state["open_trades"].items():
        if trade.pair.startswith(symbol + '/'):
            trade_to_close = trade
            trade_id_to_close = trade_id
            break

    if not trade_to_close:
        await application.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text=f"⚠️ Received close command for **{symbol}**, but no open trade was found.",
            parse_mode='Markdown'
        )
        return

    # --- START OF CORRECTED LOGIC ---
    try:
        # Step 1: Safely fetch the ticker with retries
        ticker = await safe_exchange_call(exchange.fetch_ticker, trade_to_close.pair)

        # Step 2: Handle the failure case
        if not ticker:
            logger.error(f"Failed to close trade for {symbol}: Could not fetch price from exchange.")
            await application.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"🚨 **CLOSE FAILED for {symbol}** 🚨\n\nCould not get the current price from Binance after multiple retries. The trade remains open. Please check manually.",
                parse_mode='Markdown'
            )
            return

        # Step 3: THIS WAS THE MISSING PART - Use the successful result to close the trade
        exit_price = ticker['last']
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
    open_trades = app_state['open_trades'].values()
    if not open_trades:
        await update.message.reply_text("No open positions.")
        return

    message = "**Open Positions:**\n\n"
    for trade in open_trades:
        direction = "LONG" if trade.is_long else "SHORT"
        message += f"- **{trade.pair}** ({direction})\n"
        message += f"  Entry: `{trade.entry_price}`, SL: `{trade.sl_price}`, TP: `{trade.tp_price}`\n"
        message += f"  Size: `{trade.position_size:.4f}`\n\n"

    await update.message.reply_text(message, parse_mode='Markdown')


async def execute_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, trading_pair: str, photo_file_id: str):
    """Downloads an image from a file_id, extracts prices, and opens a trade."""
    try:
        # 1. Get the image file and analyze it
        load_state()
        photo_file = await context.bot.get_file(photo_file_id)
        image_path = f"{photo_file.file_id}.jpg"
        await photo_file.download_to_drive(image_path)
        await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                       text=f"Analyzing chart for **{trading_pair}**...", parse_mode='Markdown')
        extracted = extract_prices_from_image(image_path)
        os.remove(image_path)

        # 2. Validate the extracted data
        if not all(k in extracted for k in ['entry', 'stoploss']):
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                           text=f"Analysis failed. Missing 'entry' or 'stoploss'. Data: `{extracted}`")
            return

        entry = extracted['entry']
        sl = extracted['stoploss']
        tp = extracted.get('target', None)

        if entry == sl:
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                           text="Analysis failed. Entry and Stop-Loss prices cannot be the same.")
            return

        # 3. Calculate position size and create the trade object
        leverage = app_state['leverage']
        balance = app_state['balance']
        if RISK_PER_TRADE > balance:
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                           text=f"Insufficient balance. Risk: ${RISK_PER_TRADE:.2f}, Available: ${balance:.2f}")
            return

        stop_loss_distance = abs(entry - sl)
        if stop_loss_distance == 0:
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                           text="Analysis failed due to zero stop-loss distance.")
            return

        position_size_asset = RISK_PER_TRADE / stop_loss_distance
        position_size_usd = position_size_asset * entry
        trade_id = str(uuid4())

        # Determine direction from the entry/sl prices, not the caption
        is_long = sl < entry

        calculated_tp_levels = None
        if tp:
            if (is_long and tp > entry) or (not is_long and tp < entry):
                total_profit_range = abs(tp - entry)
                step_size = total_profit_range / 10
                calculated_tp_levels = [
                    {"price": entry + (step_size * i) if is_long else entry - (step_size * i), "status": "pending"} for
                    i in range(1, 11)]
            else:
                await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                               text="Warning: Target price is on the wrong side of entry. Ignoring target.")
                tp = None

        trade = PaperTrade(
            trade_id=trade_id, pair=trading_pair, entry_price=entry, sl_price=sl,
            initial_size=position_size_asset, remaining_size=position_size_asset,
            leverage=leverage, is_long=is_long, tp_levels=calculated_tp_levels,
            sl_moved_to_be=False
        )

        app_state["open_trades"][trade_id] = trade
        save_state()

        # 4. Send the final confirmation message
        direction = "LONG" if is_long else "SHORT"
        sl_percent_display = (stop_loss_distance / entry) * 100
        tp_message = f"**10 Partial TPs** up to `{tp}`" if tp else "Not Set"

        await context.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text=f"✅ **Trade Opened for {trading_pair}** ({direction})\n\n"
                 f"Leverage: **{leverage}x**\nRisk Amount: `${RISK_PER_TRADE:,.2f}`\n"
                 f"Position Value (USD): `${position_size_usd:,.2f}`\n\n"
                 f"Entry: `{entry}`\nStop-Loss: `{sl}` ({sl_percent_display:.2f}% move)\n"
                 f"Take-Profit: {tp_message}\n\n"
                 f"Current Balance: `${balance:,.2f}`",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Error in execute_trade: {e}", exc_info=True)
        await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                       text=f"A critical error occurred while trying to execute the trade: {e}")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming signals, decides to auto-trade or ask for confirmation."""
    message = update.message

    # --- NEW, SIMPLIFIED AUTHORIZATION ---
    # This single check works for all cases (direct, manual forward, monitor forward)
    # because the message always comes from your user account.
    if message.from_user.id != int(AUTHORIZED_USER_ID):
        return

    if not message.photo or not message.caption:
        return

    caption = message.caption
    match = re.search(r'#(\w+)', caption)
    if not match:
        return  # Silently ignore if no hashtag

    pair_tag = match.group(1).upper()

    if pair_tag in BLACKLISTED_COINS:
        logger.warning(f"Signal for #{pair_tag} ignored because it is on the blacklist.")
        return

    trading_pair = f"{pair_tag}USDT"

    try:
        # 1. Force a reload of the markets to ensure we have the latest data.
        await exchange.load_markets(True)

        # 2. Use the canonical ccxt method to check for the symbol.
        # This will raise a specific `ccxt.BadSymbol` error if it's not found.
        market = exchange.market(trading_pair)

        # 3. As an extra safeguard, ensure it's a futures/swap contract.
        # The `market` object has a `swap` flag which should be True.
        if not market.get('swap'):
            logger.error(f"Signal ignored. Pair '{trading_pair}' exists but is not a SWAP/FUTURES contract.")
            return

    except ccxt.BadSymbol:
        # This is the expected error when a symbol doesn't exist.
        logger.error(f"Signal ignored. Pair '{trading_pair}' is not a valid FUTURES symbol on Binance.")
        return  # Stop processing this signal
    except Exception as e:
        # This catches other errors like network issues during validation.
        logger.error(f"Error validating pair with exchange: {e}", exc_info=True)
        await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                       text=f"An error occurred while validating the trading pair: {e}")
        return

    # --- The "Cleanliness" Check ---
    clean_caption = caption.lower()
    for word in ALL_KEYWORDS:
        clean_caption = clean_caption.replace(word, '')
    clean_caption = re.sub(r'#\w+', '', clean_caption)

    photo_file_id = message.photo[-1].file_id

    # --- Routing Logic ---
    if not clean_caption.strip():
        logger.info("Clean signal detected. Executing trade automatically.")
        await execute_trade(update, context, trading_pair, photo_file_id)
    else:
        # --- THIS IS THE ENTIRE REPLACEMENT 'else' BLOCK ---
        logger.info("Complex signal detected. Creating persistent confirmation request.")

        # 1. Generate a unique ID for this request
        request_id = str(uuid4())

        # 2. Forward the original message to provide context
        fwd_message = await message.forward(chat_id=int(AUTHORIZED_USER_ID))

        # 3. Create the keyboard with the NEW callback_data format
        keyboard = [[
            InlineKeyboardButton("✅ Confirm Trade", callback_data=f"confirm_trade|{request_id}"),
            InlineKeyboardButton("❌ Ignore", callback_data=f"ignore_trade|{request_id}"),
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # 4. Send the confirmation buttons as a reply
        confirmation_message = await context.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text="This signal contains extra text. Please confirm to proceed:",
            reply_markup=reply_markup,
            reply_to_message_id=fwd_message.message_id
        )

        # 5. Create the data packet to save
        pending_request = {
            "trading_pair": trading_pair,
            "photo_file_id": photo_file_id,
            "confirmation_message_id": confirmation_message.message_id
        }

        # 6. Save it to the state and immediately to the file
        app_state["pending_confirmations"][request_id] = pending_request
        save_state()
        logger.info(f"Saved pending confirmation with ID: {request_id}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all confirmation button presses using persistent state."""
    query = update.callback_query
    await query.answer()

    # --- CRITICAL: Reload state to ensure we have the latest info after a restart ---
    load_state()

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
            await execute_trade(update, context, trading_pair, photo_file_id)

        elif action == "ignore_trade":
            await query.edit_message_text(text="❌ Signal ignored.")

    finally:
        # --- CLEANUP: No matter what, remove the request from the state ---
        # This prevents dangling or double-processed requests.
        action, request_id = query.data.split('|') # Re-split to ensure we have the ID
        if request_id in app_state.get("pending_confirmations", {}):
            del app_state["pending_confirmations"][request_id]
            save_state()
            logger.info(f"Processed and removed pending confirmation ID: {request_id}")



# ==============================================================================
# Main Bot Execution
# ==============================================================================
async def main():
    """Initializes and runs the bot and all background tasks."""
    load_state()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", placeholder_command))
    application.add_handler(CommandHandler("help", placeholder_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("positions", positions_command))
    application.add_handler(CommandHandler("setleverage", placeholder_command))
    application.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION, message_handler))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(CommandHandler("close_by_symbol", close_command_handler))

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