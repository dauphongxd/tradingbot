import json
import os
from flask import Flask, render_template, redirect, url_for, request
import ccxt
import time
import database as db
from dataclasses import asdict

db.DATABASE_FILE = "trading_bot_testnet.db"

from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
INITIAL_BALANCE = 1000.00  # Should match your bot's config

# --- Setup ---
app = Flask(__name__)
# Use a synchronous version of ccxt for this simple UI
print("✅ Initializing Web UI exchange in TESTNET mode.")
exchange = ccxt.binanceusdm({
    'apiKey': os.getenv('TESTNET_API_KEY'),
    'secret': os.getenv('TESTNET_API_SECRET'),
    'options': {
            'defaultType': 'future',
            'warnOnFetchOpenOrdersWithoutSymbol': False, # <-- ADD THIS LINE
        },
})
exchange.set_sandbox_mode(True) # This is the magic line for testnet


def safe_sync_exchange_call(func, *args, **kwargs):
    """A synchronous wrapper to safely call a ccxt function with retries."""
    max_retries = 3
    retry_delay_seconds = 3
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as e:
            print(f"[Web UI] Exchange call failed (Attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt + 1 == max_retries:
                print(f"[Web UI] All {max_retries} attempts failed. Giving up.")
                return None
            time.sleep(retry_delay_seconds)
        except Exception as e:
            print(f"[Web UI] An unexpected error occurred: {e}")
            return None
    return None


@app.route('/')
def dashboard():
    """The live dashboard, now showing positions, open orders, and history."""
    try:
        # --- Fetch All Live Data ---
        balance_data = safe_sync_exchange_call(exchange.fetch_balance) or {}
        positions_data = safe_sync_exchange_call(exchange.fetch_positions) or []

        # --- START OF FIX ---
        # We cannot fetch all open orders at once.
        # Instead, we get symbols from open positions and fetch orders for each one.
        open_positions = [p for p in positions_data if float(p.get('contracts', 0)) != 0]

        all_open_orders = []
        if open_positions:
            # Get a unique list of symbols that have an open position
            symbols_with_positions = list(set([p['symbol'] for p in open_positions]))

            # Loop through each symbol to get its open orders
            for symbol in symbols_with_positions:
                orders_for_symbol = safe_sync_exchange_call(exchange.fetch_open_orders, symbol)
                if orders_for_symbol:
                    all_open_orders.extend(orders_for_symbol)
        # --- END OF FIX ---

        # --- Process Data (using the corrected data) ---
        balance = balance_data.get('USDT', {}).get('total', INITIAL_BALANCE)
        risk = db.get_setting("risk_per_trade")
        leverage = db.get_setting("leverage")

        total_floating_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in open_positions)
        equity = balance + total_floating_pnl

        # --- Group Open Orders by Symbol (using all_open_orders) ---
        grouped_orders = {}
        for order in all_open_orders:  # <-- Use the new aggregated list
            symbol = order['symbol']
            if symbol not in grouped_orders:
                grouped_orders[symbol] = {
                    'stop_loss': None,
                    'take_profits': []
                }

            if order['type'] == 'stop_market':
                grouped_orders[symbol]['stop_loss'] = order
            elif order['type'] in ['limit', 'take_profit_market']:
                grouped_orders[symbol]['take_profits'].append(order)

        # Sort TPs by price for logical display
        for symbol, orders in grouped_orders.items():
            # Find the corresponding position to determine if it's long or short
            position_for_symbol = next((p for p in open_positions if p['symbol'] == symbol), None)
            if position_for_symbol:
                is_short = position_for_symbol['side'] == 'short'
                orders['take_profits'].sort(key=lambda x: x['price'], reverse=is_short)

        # --- Pass to Template ---
        return render_template('live_dashboard.html',
                               balance=balance,
                               risk=risk,
                               equity=equity,
                               floating_pnl=total_floating_pnl,
                               open_positions=open_positions,
                               grouped_orders=grouped_orders,
                               trade_history=[],
                               leverage=leverage)

    except Exception as e:
        # This will now print the true error if something else goes wrong
        print(f"Error loading dashboard: {e}")
        import traceback
        traceback.print_exc()  # Print the full error traceback to the console
        return f"<h1>Error connecting to Binance Testnet</h1><p>{e}</p>"


@app.route('/close_trade/<symbol>')  # <-- We now use the symbol, not a database ID
def close_trade(symbol):
    """Closes a live position via the web UI."""
    trading_pair = symbol
    print(f"Received UI request to close {trading_pair}")
    try:
        # --- START OF FIX ---
        # Instead of fetching a single position, fetch ALL positions and then filter.
        # This is more robust and avoids the 'list' object TypeError.

        all_positions = safe_sync_exchange_call(exchange.fetch_positions) or []
        target_position = None
        for position in all_positions:
            # Find the position that matches the symbol AND has a non-zero size
            if position['symbol'] == trading_pair and float(position.get('contracts', 0)) != 0:
                target_position = position
                break # Found it, so we can stop looping

        # --- END OF FIX ---

        if not target_position:
            print(f"Could not find an open position for {trading_pair} to close.")
            return redirect(url_for('dashboard'))

        # Determine side and size from the filtered position
        position_size = float(target_position['contracts'])
        side_to_close = 'sell' if target_position['side'] == 'long' else 'buy'

        # Cancel open orders first
        safe_sync_exchange_call(exchange.cancel_all_orders, trading_pair)

        # Place the closing market order
        safe_sync_exchange_call(
            exchange.create_order,
            trading_pair, 'market', side_to_close, position_size, params={'reduceOnly': True}
        )
        print(f"Placed UI closing order for {trading_pair}")

    except Exception as e:
        print(f"Error closing trade {trading_pair} from UI: {e}")

    return redirect(url_for('dashboard'))

if __name__ == "__main__":
    print("Starting Flask Web UI...")
    print("Open your browser and go to http://127.0.0.1:5000")
    app.run(debug=True, port=3000)