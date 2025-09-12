# channel_monitor.py

import re
import logging
from telethon.sync import TelegramClient, events

from dotenv import load_dotenv
import os
import asyncio

load_dotenv()

# --- CONFIGURATION ---
# 1. Get these from my.telegram.org
API_ID = os.getenv("API_ID")  # Replace with your API ID
API_HASH = os.getenv("API_HASH")  # Replace with your API Hash

# 2. The name of the session file to be created
SESSION_NAME = 'trading_monitor_session'

# 3. The ID of the channel you want to monitor
#    - You can get this by forwarding a message from the channel to a bot like @userinfobot
#    - It will be a negative number, e.g., -100123456789
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID"))  # <--- IMPORTANT: SET THIS

# 4. The username or ID of your trading bot
#    - If your bot has a username like @MyTradingBot, use that.
DESTINATION_BOT_ID = '@DauTradingTestBot'  # <--- IMPORTANT: SET THIS

# 5. Define the keywords that trigger a signal
BUY_WORDS = {'buy', 'long', 'bullish', 'buying', 'bought', 'longed'}
SELL_WORDS = {'sell', 'short', 'bearish', 'selling', 'sold', 'shorted'}
CLOSE_WORDS = {'close', 'closing', 'closed'}
ALL_KEYWORDS = BUY_WORDS.union(SELL_WORDS).union(CLOSE_WORDS)

# --- SETUP LOGGING ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- THE TELETHON CLIENT ---

# We use "with" to ensure the client is properly started and stopped.
with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
    @client.on(events.NewMessage(chats=int(SOURCE_CHANNEL_ID)))
    async def handler(event):
        """
        This function now validates a signal and then waits 60 seconds
        before acting, checking if the message was deleted during the wait.
        """
        message = event.message

        # --- NEW: Enhanced Logging ---
        logger.info(f"--- New message detected (ID: {message.id}) ---")
        caption = message.text.lower() if message.text else ""

        if not caption:
            logger.info("SKIPPED: Message has no text caption.")
            return

        logger.info(f"Message content: \"{caption}\". Checking for signals...")
        # --- END: Enhanced Logging ---

        # --- Initial Filtering (with more logging) ---
        match = re.search(r'#(\w+)', caption)
        if not match:
            logger.info("SKIPPED: No coin symbol (#...) found in caption.")
            return

        coin_symbol = match.group(1).upper()
        logger.info(f"Found Coin Symbol: #{coin_symbol}")

        is_close_signal = any(word in caption for word in CLOSE_WORDS)
        is_open_signal = any(word in caption for word in BUY_WORDS.union(SELL_WORDS))

        # If it's not a recognized signal type, ignore it immediately
        if not is_open_signal and not is_close_signal:
            logger.info(f"SKIPPED: No trade keywords ({', '.join(ALL_KEYWORDS)}) found.")
            return

        logger.info(f"Signal type identified -> Is Open Signal: {is_open_signal}, Is Close Signal: {is_close_signal}")

        # --- NEW: DELAY AND DELETION CHECK ---
        message_id = message.id
        logger.info(f"VALID SIGNAL: #{coin_symbol} detected. Waiting 60 seconds for confirmation...")

        # Wait for 60 seconds
        await asyncio.sleep(60)

        # After waiting, check if the message still exists
        try:
            refetched_message = await client.get_messages(int(SOURCE_CHANNEL_ID), ids=message_id)
            if not refetched_message:
                logger.warning(f"CANCELLED: Signal for #{coin_symbol} (ID: {message_id}) was DELETED.")
                return
        except Exception as e:
            logger.error(f"Could not re-fetch message {message_id}. Assuming it was deleted. Error: {e}")
            return

        logger.info(f"CONFIRMED: Signal for #{coin_symbol} still exists. Proceeding to forward...")
        # --- END NEW LOGIC ---

        # --- ACTION LOGIC (moved from the top) ---
        # 1. Check for a CLOSE signal FIRST.
        if is_close_signal:
            clean_caption = caption
            for word in CLOSE_WORDS:
                clean_caption = clean_caption.replace(word, '')
            clean_caption = re.sub(r'#\w+', '', clean_caption)

            if not clean_caption.strip():
                logger.info(f"Action: Sending /close_by_symbol command to bot.")
                await client.send_message(DESTINATION_BOT_ID, f"/close_by_symbol {coin_symbol}")
            else:
                logger.info(f"Action: Forwarding close signal with extra text to bot.")
                await message.forward_to(DESTINATION_BOT_ID)
            return

        # 2. Only if it's NOT a close signal, check if it's an OPEN signal.
        elif is_open_signal:
            if message.photo:
                logger.info(f"Action: Forwarding open signal (with photo) to bot.")
                await message.forward_to(DESTINATION_BOT_ID)


    # --- START THE MONITOR ---
    logger.info("Channel monitor started. Listening for new messages...")
    client.run_until_disconnected()