# telegram_bot_v2.py

import asyncio
import json
import logging
import re
import os
from uuid import uuid4
from dataclasses import dataclass, asdict
import json

import ccxt.async_support as ccxt
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from extract_price import extract_prices_from_image
import database as db

db.DATABASE_FILE = "trading_bot_testnet.db"

from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TESTNET_TOKEN')
AUTHORIZED_USER_ID = os.getenv('AUTHORIZED_USER_ID')
INITIAL_BALANCE = 1000.00
POLL_INTERVAL_SECONDS = 3  # Check prices more frequently
BOT_MODE_TAG = "🔵 [TESTNET] 🔵\n\n"

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


watched_positions = {}

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
#  Global State & Exchange Instance
# ==============================================================================
app_state = {
    "balance": 0.0,
    "leverage": 0.0,
    "pending_confirmations": {},
    "pending_sl_tp_placement": {} # <-- ADD THIS to store details
}

# --- START: TESTNET CONFIGURATION ---
# Use a single, shared exchange instance for efficiency, configured for TESTNET
logger.info("✅ Initializing exchange in TESTNET mode.")
exchange = ccxt.binanceusdm({
    'apiKey': os.getenv('TESTNET_API_KEY'),
    'secret': os.getenv('TESTNET_API_SECRET'),
    'options': {
        'defaultType': 'future',
        'warnOnFetchOpenOrdersWithoutSymbol': False,
    },
})
exchange.set_sandbox_mode(True)  # This is the magic line for testnet


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

async def close_trade_by_symbol(symbol: str, application: Application):
    """Finds an open position by its symbol on the exchange and closes it."""
    trading_pair = f"{symbol.upper()}USDT"
    logger.info(f"Received command to close position for {trading_pair}")

    try:
        # Get all open positions from the exchange
        positions = await safe_exchange_call(exchange.fetch_positions)
        target_position = None
        for p in positions:
            if p['symbol'] == trading_pair and float(p['contracts']) != 0:
                target_position = p
                break

        if not target_position:
            await application.bot.send_message(
                chat_id=int(AUTHORIZED_USER_ID),
                text=f"⚠️ No open position found on the exchange for **{trading_pair}**.",
                parse_mode='Markdown'
            )
            return

        # Determine the side and size needed to close the position
        position_size = float(target_position['contracts'])
        side_to_close = 'sell' if target_position['side'] == 'long' else 'buy'

        # IMPORTANT: Cancel all open orders (SL/TP) for this symbol first
        await safe_exchange_call(exchange.cancel_all_orders, trading_pair)
        logger.info(f"Cancelled all open orders for {trading_pair} before closing.")

        # Create a market order to close the position
        await safe_exchange_call(
            exchange.create_order,
            trading_pair, 'market', side_to_close, position_size, params={'reduceOnly': True}
        )

        logger.info(f"Successfully placed closing order for {trading_pair}.")
        # The live_trade_monitor will automatically detect the closure and send the final PNL notification.

    except Exception as e:
        logger.error(f"An unexpected error occurred while closing {trading_pair}: {e}")
        await application.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text=f"🚨 An error occurred trying to close **{trading_pair}**. Error: {e}",
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
    # ... function code ...
    try:
        # Get the risk value as a string from the command arguments
        risk_input = context.args[0]
        # --- FIX: Get the LIVE balance from the exchange ---
        balance_data = await safe_exchange_call(exchange.fetch_balance)
        current_balance = balance_data['USDT']['total']
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
    try:
        balance_data = await safe_exchange_call(exchange.fetch_balance)
        usdt_balance = balance_data['USDT']['total']
        await update.message.reply_text(f"{BOT_MODE_TAG}Testnet Balance: **${usdt_balance:,.2f}**", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Could not fetch testnet balance: {e}")
        await update.message.reply_text("Error fetching testnet balance.")


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        positions = await safe_exchange_call(exchange.fetch_positions)
        # Filter out positions that are zero
        open_positions = [p for p in positions if float(p['contracts']) != 0]

        if not open_positions:
            await update.message.reply_text("No open positions on testnet.")
            return

        message = f"{BOT_MODE_TAG}**Open Testnet Positions:**\n\n"
        for p in open_positions:
            direction = "LONG" if p['side'] == 'long' else "SHORT"
            pnl = float(p.get('unrealizedPnl', 0))
            size = float(p.get('contracts', 0))
            entry = float(p.get('entryPrice', 0))
            message += f"- **{p['symbol']}** ({direction})\n"
            message += f"  Size: `{size}`, Entry: `{entry}`\n"
            message += f"  Unrealized PNL: `${pnl:,.2f}`\n\n"

        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Could not fetch testnet positions: {e}")
        await update.message.reply_text("Error fetching testnet positions.")


async def execute_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, trading_pair: str, photo_file_id: str):
    """
    Step 1 of 2: Places a LIMIT order for entry and stages the SL/TP info.
    """
    try:
        # 1. Image analysis and calculation
        photo_file = await context.bot.get_file(photo_file_id)
        image_path = f"{photo_file.file_id}.jpg"
        await photo_file.download_to_drive(image_path)
        extracted = extract_prices_from_image(image_path)
        os.remove(image_path)

        if not all(k in extracted for k in ['entry', 'stoploss']):
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID), text=f"❌ **Analysis Failed:** Missing 'entry' or 'stoploss'.")
            return

        entry_price = extracted['entry']
        sl_price = extracted['stoploss']
        is_long = sl_price < entry_price
        direction = 'buy' if is_long else 'sell'

        await exchange.load_markets()
        market = exchange.market(trading_pair)

        leverage = int(float(db.get_setting('leverage')))
        await safe_exchange_call(exchange.set_leverage, leverage, trading_pair)

        balance_data = await safe_exchange_call(exchange.fetch_balance)
        balance = balance_data['USDT']['total']
        risk_setting = db.get_setting('risk_per_trade')
        risk_per_trade = 0.0
        if '%' in risk_setting:
            risk_per_trade = (float(risk_setting.strip('%')) / 100) * balance
        else:
            risk_per_trade = float(risk_setting)

        stop_loss_distance = abs(entry_price - sl_price)
        position_size = risk_per_trade / stop_loss_distance
        position_size_str = exchange.amount_to_precision(trading_pair, position_size)

        # 2. Place ONLY the LIMIT order for entry
        logger.info(f"Placing LIMIT {direction} order for {position_size_str} of {trading_pair} at {entry_price}")
        entry_order = await safe_exchange_call(
            exchange.create_order, trading_pair, 'limit', direction, position_size_str, entry_price
        )

        # --- THIS IS THE FIX ---
        # If the order placement failed (e.g., invalid symbol), entry_order will be None.
        if not entry_order:
            logger.error(f"Failed to place limit entry order for {trading_pair}. The symbol may be invalid on the testnet.")
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID), text=f"{BOT_MODE_TAG}❌ **Order Failed for {trading_pair}**\n\nThe exchange rejected the order. This symbol may not be available on the testnet.")
            return # Stop execution here
        # --- END FIX ---

        # 3. Stage the SL/TP details in memory, waiting for the fill
        app_state["pending_sl_tp_placement"][trading_pair] = {
            "sl_price": sl_price,
            "position_size_str": position_size_str,
            "is_long": is_long,
            "stop_loss_distance": stop_loss_distance,
            "entry_price": entry_price
        }
        logger.info(f"Staged SL/TP info for {trading_pair} in memory.")

        # 4. Send a "Pending" Confirmation
        await context.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text=f"{BOT_MODE_TAG}"
                 f"⏳ **Limit Order PLACED for {trading_pair}** ⏳\n\n"
                 f"Direction: **{direction.upper()}**\n"
                 f"Size: `{position_size_str}`\n"
                 f"Entry Price: `{entry_price}`\n\n"
                 f"**The bot is now waiting for this order to be filled.**",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Error in execute_trade (limit order): {e}", exc_info=True)
        await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID), text=f"A critical error occurred while placing the limit order: {e}")


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
    existing_position = None
    all_positions = await safe_exchange_call(exchange.fetch_positions)
    for position in all_positions:
        if position['symbol'] == trading_pair and float(position['contracts']) != 0:
            existing_position = position
            break

    # 3. The Core Reversal Decision
    if existing_position:
        existing_position_is_long = existing_position['side'] == 'long'
        # Scenario A: The directions are DIFFERENT (a true reversal)
        if existing_position_is_long != new_signal_is_long:
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
        await execute_trade(update, context, trading_pair, photo_file_id)
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
            await execute_trade(update, context, trading_pair, photo_file_id)

        elif action == "ignore_trade":
            await query.edit_message_text(text="❌ Signal ignored.")

    finally:
        # --- CLEANUP: No matter what, remove the request from the state ---
        # This prevents dangling or double-processed requests.
        action, request_id = query.data.split('|') # Re-split to ensure we have the ID
        if request_id in app_state.get("pending_confirmations", {}):
            del app_state["pending_confirmations"][request_id]
            logger.info(f"Processed and removed pending confirmation ID: {request_id}")


async def live_trade_monitor(application: Application):
    """
    Monitors live positions with robust, atomic state checking.
    - Detects new fills and places staged SL/TP orders.
    - Detects closed positions and sends notifications.
    """
    logger.info("Live Testnet Trade Monitor started.")
    global watched_positions

    while True:
        try:
            # Get all current data from the exchange
            current_positions_list = await safe_exchange_call(exchange.fetch_positions)
            current_positions = {p['symbol']: p for p in current_positions_list if float(p['contracts']) != 0}
            pending_placements = app_state.get("pending_sl_tp_placement", {})

            # --- SINGLE, ROBUST LOOP TO PROCESS ALL CURRENT POSITIONS ---
            for exchange_symbol, position in current_positions.items():
                bot_symbol = exchange_symbol.replace('/', '').replace(':USDT', '')

                # Priority 1: Does this position need its SL/TP orders placed?
                if bot_symbol in pending_placements:
                    logger.info(f"Detected filled entry for {bot_symbol}. Placing SL/TP orders.")

                    details = pending_placements[bot_symbol]
                    sl_price = details["sl_price"]
                    position_size_str = details["position_size_str"]
                    is_long = details["is_long"]
                    stop_loss_distance = details["stop_loss_distance"]
                    actual_entry_price = float(position['entryPrice'])

                    # Place SL and TP orders (using the full 'exchange_symbol')
                    sl_side = 'sell' if is_long else 'buy'
                    await safe_exchange_call(exchange.create_order, exchange_symbol, 'STOP_MARKET', sl_side,
                                             position_size_str, None, {'stopPrice': sl_price})

                    tp_side = 'sell' if is_long else 'buy'
                    ideal_partial_size = float(position_size_str) / 10
                    partial_size_str = exchange.amount_to_precision(exchange_symbol, ideal_partial_size)

                    if float(partial_size_str) > 0:
                        step_size = (stop_loss_distance * 10.0) / 10
                        for i in range(1, 11):
                            tp_price = actual_entry_price + (step_size * i) if is_long else actual_entry_price - (
                                        step_size * i)
                            tp_price_formatted = exchange.price_to_precision(exchange_symbol, tp_price)
                            await safe_exchange_call(exchange.create_order, exchange_symbol, 'limit', tp_side,
                                                     partial_size_str, tp_price_formatted, {'reduceOnly': True})

                    logger.info(f"Successfully placed SL and TP orders for {bot_symbol}.")

                    # Send notification
                    await application.bot.send_message(
                        chat_id=int(AUTHORIZED_USER_ID),
                        text=f"{BOT_MODE_TAG}✅ **Position FILLED & PROTECTED for {bot_symbol}** ✅\n\n"
                             f"Actual Entry Price: `{actual_entry_price}`\n"
                             f"Stop Loss is set at `{sl_price}`.",
                        parse_mode='Markdown'
                    )

                    # Mark as managed: add to watched_positions and remove from pending
                    watched_positions[exchange_symbol] = position
                    del app_state["pending_sl_tp_placement"][bot_symbol]

                # Priority 2: Is this a position we don't know about? (from a previous session)
                elif exchange_symbol not in watched_positions:
                    logger.info(
                        f"Detected existing position for {bot_symbol} from a previous session. Adding to watch list.")
                    watched_positions[exchange_symbol] = position

            # --- CHECK FOR CLOSED POSITIONS ---
            # Create a copy of keys to safely iterate while deleting
            for exchange_symbol in list(watched_positions.keys()):
                if exchange_symbol not in current_positions:
                    bot_symbol = exchange_symbol.replace('/', '').replace(':USDT', '')
                    logger.info(f"Position for {bot_symbol} has closed. Fetching details.")

                    trade_history = await safe_exchange_call(exchange.fetch_my_trades, exchange_symbol, limit=1)
                    last_trade = trade_history[0] if trade_history else None

                    message = f"{BOT_MODE_TAG}"
                    if last_trade:
                        pnl = float(last_trade['info'].get('realizedPnl', 0))
                        if last_trade['type'] == 'limit':
                            message += f"🎯 **TAKE PROFIT HIT for {bot_symbol}** 🎯\n\n"
                        elif last_trade['type'] == 'stop_market':
                            message += f"❌ **STOP LOSS HIT for {bot_symbol}** ❌\n\n"
                        else:
                            message += f"🔵 **MANUAL CLOSE for {bot_symbol}** 🔵\n\n"
                        message += f"Exit Price: `{last_trade['price']}`\nPNL: `${pnl:,.2f}`"
                    else:
                        message += f"Position for {bot_symbol} was closed, but couldn't fetch trade details."

                    await application.bot.send_message(chat_id=int(AUTHORIZED_USER_ID), text=message,
                                                       parse_mode='Markdown')

                    # Clean up the watch list
                    del watched_positions[exchange_symbol]

            await asyncio.sleep(10)

        except Exception as e:
            logger.error(f"[Live Monitor] An unexpected error occurred: {e}", exc_info=True)
            await asyncio.sleep(30)



async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches and displays all open (pending) orders from the exchange."""
    try:
        open_orders = await safe_exchange_call(exchange.fetch_open_orders)
        if not open_orders:
            await update.message.reply_text(f"{BOT_MODE_TAG}No open orders found on the exchange.")
            return

        message = f"{BOT_MODE_TAG}**Open Orders:**\n\n"
        for order in open_orders:
            order_type = order['type'].replace('_', ' ').upper()
            message += f"- **{order['symbol']}** ({order['side'].upper()})\n"
            message += f"  Type: `{order_type}`\n"
            message += f"  Amount: `{order['amount']}`\n"
            message += f"  Price: `{order['price']}`\n\n"

        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Could not fetch open orders: {e}")
        await update.message.reply_text(f"{BOT_MODE_TAG}Error fetching open orders.")

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

    # Register command handlers
    application.add_handler(CommandHandler("start", placeholder_command))
    application.add_handler(CommandHandler("help", placeholder_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("positions", positions_command))
    application.add_handler(CommandHandler("orders", orders_command))  # <-- REPLACED
    application.add_handler(CommandHandler("setleverage", set_leverage_command))
    application.add_handler(CommandHandler("setrisk", set_risk_command))
    application.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION, message_handler))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(CommandHandler("close_by_symbol", close_command_handler))

    # --- This is the new, non-blocking way to run the bot ---
    try:
        print("Initializing bot...")
        await application.initialize()  # Prepares the application

        # Create the background task for the market monitor
        # Do this *after* initializing the application
        asyncio.create_task(live_trade_monitor(application))

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