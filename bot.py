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
from telegram.request import Request
import database as db

from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AUTHORIZED_USER_ID = os.getenv('AUTHORIZED_USER_ID')
INITIAL_BALANCE = 1000.00
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
    """The 'control tower' that now checks for SL, manual closures, and partial TPs."""
    # ... (The first part of the function reloading state from the file is the same) ...
    logger.info("Market monitor started.")
    while True:
        try:
            open_trades = db.get_open_trades()
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

                # --- 1. Check for STOP LOSS hit ---
                # The trade might have been fully closed by the last TP, so we check if it still exists
                if (trade.is_long and current_price <= trade.sl_price) or \
                        (not trade.is_long and current_price >= trade.sl_price):
                    await process_trade_closure(application, trade, "SL_HIT", trade.sl_price)
                    continue  # Move to the next trade, as this one is now closed

                # --- 2. Check for PARTIAL TAKE PROFIT hits ---
                if trade.tp_levels:
                    for i, level in enumerate(trade.tp_levels):
                        if level['status'] == 'pending':
                            if (trade.is_long and current_price >= level['price']) or \
                                    (not trade.is_long and current_price <= level['price']):

                                # First, process the partial closure as always
                                await process_partial_tp_closure(application, trade, level, i)

                                # --- NEW HYBRID STOP-LOSS LOGIC ---
                                new_sl_price = None
                                notification_reason = ""

                                # The TRIGGER: If TP2 (index 1) is hit, move SL to Break-Even
                                if i == 1 and not trade.sl_moved_to_be:
                                    new_sl_price = trade.entry_price
                                    trade.sl_moved_to_be = True  # Set the flag so this only runs once
                                    notification_reason = "TP2 hit. Trade is now risk-free."

                                # The TRAIL: If TP3 or higher is hit, trail the SL to the TP level from two steps ago
                                elif i > 1:
                                    # e.g., When TP3 (i=2) is hit, move SL to TP1 (i-2=0)
                                    # e.g., When TP4 (i=3) is hit, move SL to TP2 (i-2=1)
                                    new_sl_price = trade.tp_levels[i - 2]['price']
                                    notification_reason = f"TP{i + 1} hit. Trailing stop-loss updated."

                                # If we determined a new SL is needed, update and notify
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
                                        chat_id=AUTHORIZED_USER_ID, text=message, parse_mode='Markdown'
                                    )
                                    logger.info(
                                        f"Moved SL for trade {trade.trade_id} to {trade.sl_price}. Reason: {notification_reason}")

                            break  # Only check the next pending level in each loop

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
    level['status'] = 'hit'

    is_fully_closed = (level_index == 9) or trade.remaining_size < 1e-8
    if is_fully_closed:
        db.close_trade(trade.trade_id, f"TP{level_index + 1}_FULL_CLOSE", level['price'], pnl)
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
    """Handles the logic for a FULL trade closure and records it to history."""
    price_diff = exit_price - trade.entry_price
    if not trade.is_long:
        price_diff = -price_diff

    pnl = price_diff * trade.remaining_size
    current_balance = float(db.get_setting("balance"))
    new_balance = current_balance + pnl
    db.update_setting("balance", new_balance)

    db.close_trade(trade.trade_id, status, exit_price, pnl)

    result_text = "❌ STOP LOSS ❌\n\n" if "SL_HIT" in status else "🔵 MANUAL CLOSE 🔵\n\n"
    message = (
        f"{result_text}"
        f"Trade Closed: **{trade.pair}**\n"
        f"Exit: `{exit_price}`\n"
        f"PNL: `${pnl:,.2f}`\n\n"
        f"**New Balance: `${new_balance:,.2f}`**"
    )
    await application.bot.send_message(
        chat_id=int(AUTHORIZED_USER_ID), text=message, parse_mode='Markdown'
    )
    logger.info(f"Trade {trade.trade_id} closed. PNL: {pnl:,.2f}. Recorded to history.")


async def close_trade_by_symbol(symbol: str, application: Application):
    """Finds an open trade by its symbol and closes it at market price."""
    trade_to_close = None

    # Find the trade in our app_state
    open_trades = db.get_open_trades()
    for trade in open_trades:
        if trade.pair.startswith(symbol + '/'):
            trade_to_close = trade
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
    """Handles the /setrisk command to dynamically update RISK_PER_TRADE."""
    if update.message.from_user.id != int(AUTHORIZED_USER_ID):
        return

    try:
        # Get the new risk value from the command arguments
        new_risk = float(context.args[0])
        current_balance = float(db.get_setting("balance"))

        if new_risk <= 0:
            await update.message.reply_text("⚠️ **Invalid Value:** Risk must be a positive number.")
            return
        if new_risk > current_balance:
            await update.message.reply_text(f"⚠️ **Warning:** New risk `${new_risk:,.2f}` is higher than your current balance of `${current_balance:,.2f}`.")

        # This is the key: we need a way to store and retrieve this value.
        # Let's use our database for this.
        db.update_setting("risk_per_trade", new_risk)

        logger.info(f"Risk per trade updated to ${new_risk:,.2f} by user command.")
        await update.message.reply_text(
            f"✅ **Risk Updated** ✅\n\n"
            f"New risk per trade is now **${new_risk:,.2f}**.",
            parse_mode='Markdown'
        )

    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/setrisk <dollar_amount>` (e.g., `/setrisk 50`)", parse_mode='Markdown')
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


async def execute_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, trading_pair: str, photo_file_id: str):
    """Downloads an image from a file_id, extracts prices, and opens a trade."""
    try:
        # 1. Get the image file and analyze it

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
                                           text=f"❌ **Analysis Failed:** Missing 'entry' or 'stoploss'. OCR could not read the image clearly. Data: `{extracted}`")
            return

        entry = extracted['entry']
        sl = extracted['stoploss']
        is_long = sl < entry # Determine direction based on prices

        if (is_long and entry <= sl) or (not is_long and entry >= sl):
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                           text=f"❌ **Logic Error:** Stop-Loss (`{sl}`) must be below entry (`{entry}`) for a LONG, or above for a SHORT.")
            return

        if 'target' in extracted:
            tp = extracted['target']
            if (is_long and tp <= entry) or (not is_long and tp >= entry):
                await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                               text=f"⚠️ **Warning:** Target price (`{tp}`) is on the wrong side of entry. Ignoring target.")
                tp = None  # Invalidate the target

        if entry == sl:
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                           text="Analysis failed. Entry and Stop-Loss prices cannot be the same.")
            return

        # 3. Calculate position size and create the trade object
        leverage = float(db.get_setting('leverage'))
        balance = float(db.get_setting('balance'))

        # --- START OF MODIFICATION ---
        # Fetch the risk amount dynamically from the database
        risk_per_trade = float(db.get_setting('risk_per_trade'))
        if risk_per_trade is None:
            # Fallback in case it's not set yet in the DB
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                               text="⚠️ **CRITICAL:** Risk per trade is not set. Please use `/setrisk <amount>`.")
            return

        if risk_per_trade > balance:
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                               text=f"Insufficient balance. Risk: ${risk_per_trade:.2f}, Available: ${balance:.2f}")
            return
            # --- END OF MODIFICATION ---

        stop_loss_distance = abs(entry - sl)
        if stop_loss_distance == 0:
            await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                           text="Analysis failed due to zero stop-loss distance.")
            return

        # Use the dynamic variable here instead of the constant
        position_size_asset = risk_per_trade / stop_loss_distance
        position_size_usd = position_size_asset * entry
        trade_id = str(uuid4())

        # Determine direction from the entry/sl prices, not the caption
        is_long = sl < entry

        # --- START: NEW TP CALCULATION LOGIC ---
        calculated_tp_levels = None

        # First, we always need the risk distance (the "R" in RR)
        stop_loss_distance = abs(entry - sl)

        # Case 1: A target price was successfully extracted from the image
        if tp and ((is_long and tp > entry) or (not is_long and tp < entry)):
            total_profit_range = abs(tp - entry)
            step_size = total_profit_range / 10
            calculated_tp_levels = [
                {"price": entry + (step_size * i) if is_long else entry - (step_size * i), "status": "pending"} for
                i in range(1, 11)]
            logger.info(f"Using image-based target for {trading_pair}. Final TP: {tp}")

        # Case 2: No target was found, so we calculate TPs based on a 10R target
        else:
            if tp:  # This handles the case where the OCR found a target, but it was invalid (on the wrong side of entry)
                await context.bot.send_message(chat_id=int(AUTHORIZED_USER_ID),
                                               text=f"⚠️ Warning: Target price (`{tp}`) is invalid. Defaulting to 10R calculation.")
                tp = None  # Nullify the invalid target

            logger.info(f"No valid target found for {trading_pair}. Calculating 10R-based TPs.")

            FINAL_RR = 10.0
            total_profit_range = stop_loss_distance * FINAL_RR
            step_size = total_profit_range / 10  # This conveniently simplifies to stop_loss_distance

            calculated_tp_levels = [
                {"price": entry + (step_size * i) if is_long else entry - (step_size * i), "status": "pending"} for
                i in range(1, 11)]
        # --- END: NEW TP CALCULATION LOGIC ---

        trade = PaperTrade(
            trade_id=trade_id, pair=trading_pair, entry_price=entry, sl_price=sl,
            initial_size=position_size_asset, remaining_size=position_size_asset,
            leverage=leverage, is_long=is_long, tp_levels=calculated_tp_levels,
            sl_moved_to_be=False
        )

        db.add_trade(trade)

        # 4. Send the final confirmation message
        direction = "LONG" if is_long else "SHORT"
        sl_percent_display = (stop_loss_distance / entry) * 100
        tp_message = "Not Set"
        if calculated_tp_levels:
            final_tp_price = calculated_tp_levels[-1]['price']
            # The number of decimals can be important for crypto, let's format it properly
            price_format = f".{8 - len(str(int(final_tp_price)))}f"  # Dynamic precision formatting

            if tp:
                # If the original 'tp' variable exists, it was an image-based target
                tp_message = f"**10 Partial TPs** up to `{final_tp_price:{price_format}}` (from image)"
            else:
                # Otherwise, it was calculated via RR
                tp_message = f"**10 Partial TPs** calculated up to `{final_tp_price:{price_format}}` (10R)"
        # --- END: NEW CONFIRMATION MESSAGE LOGIC ---

        await context.bot.send_message(
            chat_id=int(AUTHORIZED_USER_ID),
            text=f"✅ **Trade Opened for {trading_pair}** ({direction})\n\n"
                 f"Leverage: **{leverage}x**\nRisk Amount: `${risk_per_trade:,.2f}`\n"  # <-- Use the variable
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



# ==============================================================================
# Main Bot Execution
# ==============================================================================
async def main():
    """Initializes and runs the bot and all background tasks."""
    db.init_db(INITIAL_BALANCE)

    # Load initial state from the database into the in-memory app_state
    app_state["balance"] = float(db.get_setting("balance"))
    app_state["leverage"] = float(db.get_setting("leverage"))
    logger.info(f"State loaded from DB. Balance: ${app_state['balance']:.2f}")

    request = Request(connect_timeout=10, read_timeout=20)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", placeholder_command))
    application.add_handler(CommandHandler("help", placeholder_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("positions", positions_command))
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