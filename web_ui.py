import json
import os
from flask import Flask, render_template, redirect, url_for
import ccxt

# --- Configuration ---
DATA_FILE = "trading_data.json"
INITIAL_BALANCE = 1000.00  # Should match your bot's config

# --- Setup ---
app = Flask(__name__)
# Use a synchronous version of ccxt for this simple UI
exchange = ccxt.binanceusdm()
exchange.proxies = {
    'http': 'http://189.219.53.209:10000',
    'https': 'http://189.219.53.209:10000',
}


def calculate_stats(trade_history):
    """Calculates performance metrics from a list of closed trades."""
    if not trade_history:
        return {"total_trades": 0}  # Return default if no history

    total_trades = len(trade_history)
    wins = [t for t in trade_history if t['pnl'] > 0]
    losses = [t for t in trade_history if t['pnl'] <= 0]

    win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
    total_pnl = sum(t['pnl'] for t in trade_history)

    total_profit = sum(t['pnl'] for t in wins)
    total_loss = abs(sum(t['pnl'] for t in losses))

    avg_win = total_profit / len(wins) if wins else 0
    avg_loss = total_loss / len(losses) if losses else 0

    profit_factor = total_profit / total_loss if total_loss > 0 else 0

    return {
        "total_trades": total_trades,
        "win_rate": f"{win_rate:.2f}%",
        "total_pnl": total_pnl,
        "profit_factor": f"{profit_factor:.2f}",
        "avg_win": avg_win,
        "avg_loss": avg_loss
    }


def calculate_pnl(trade, current_price):
    """Calculates the PNL for a single trade."""
    price_diff = current_price - trade['entry_price']
    if not trade['is_long']:
        price_diff = -price_diff

    # Use the 'remaining_size' to calculate floating PNL
    pnl = price_diff * trade['remaining_size']  # <--- THIS IS THE FIX
    return pnl


@app.route('/')
def dashboard():
    """The main dashboard page with more robust price fetching."""
    if not os.path.exists(DATA_FILE):
        return "Trading data file not found. Please run the bot first to generate it.", 404

    with open(DATA_FILE, 'r') as f:
        try:
            state = json.load(f)
        except json.JSONDecodeError:
            return "Error reading trading data file. It might be empty or corrupted.", 500

    balance = state.get("balance", INITIAL_BALANCE)
    leverage = state.get("leverage", 20.0)
    open_trades_data = state.get("open_trades", {})
    trade_history = state.get("trade_history", [])
    stats = calculate_stats(trade_history)

    total_floating_pnl = 0.0
    processed_trades = []

    for trade_id, trade in open_trades_data.items():
        try:
            # --- NEW: Fetch ticker for each trade individually ---
            ticker = exchange.fetch_ticker(trade['pair'])
            current_price = ticker['last']

            pnl = calculate_pnl(trade, current_price)

            trade['current_pnl'] = pnl
            trade['current_price'] = current_price
            total_floating_pnl += pnl

        except Exception as e:
            # If one ticker fails, it won't stop the others from loading.
            print(f"Could not fetch live price for {trade['pair']}: {e}")
            trade['current_pnl'] = "N/A"
            trade['current_price'] = "N/A"

        processed_trades.append(trade)

    equity = balance + total_floating_pnl

    return render_template('index.html',
                           balance=balance,
                           leverage=leverage,
                           trades=processed_trades,
                           floating_pnl=total_floating_pnl,
                           equity=equity,
                           stats=stats,  # <-- Pass stats to template
                           trade_history=reversed(trade_history))  # <-- Pass history, newest first


@app.route('/close_trade/<trade_id>')
def close_trade(trade_id):
    """Endpoint to manually close a trade using the remaining size."""
    if not os.path.exists(DATA_FILE): return "Data file not found.", 404
    with open(DATA_FILE, 'r') as f:
        state = json.load(f)
    open_trades = state.get("open_trades", {})
    if trade_id not in open_trades:
        return "Trade ID not found.", 404
    trade_to_close = open_trades[trade_id]
    try:
        ticker = exchange.fetch_ticker(trade_to_close['pair'])
        exit_price = ticker['last']
        price_diff = exit_price - trade_to_close['entry_price']
        if not trade_to_close['is_long']:
            price_diff = -price_diff

        # --- CRITICAL CHANGE: Use remaining_size for PNL ---
        pnl = price_diff * trade_to_close['remaining_size']

        state['balance'] += pnl
        del state['open_trades'][trade_id]
        with open(DATA_FILE, 'w') as f:
            json.dump(state, f, indent=4)

        # --- Send a notification to the bot's user ---
        # This is an optional but nice feature
        bot_token = "8304193867:AAFtge5snCmFKmElWLcRn4PUs5XRpNWt974"  # You need to put your token here
        user_id = 354055384  # And your ID here
        message = (
            f"🔵🔵🔵 MANUAL CLOSE 🔵🔵🔵\n\n"
            f"Trade Closed: **{trade_to_close['pair']}**\n"
            f"Exit: `{exit_price}`\n"
            f"PNL: `${pnl:,.2f}`\n\n"
            f"**New Balance: `${state['balance']:,.2f}`**"
        )
        # We need a synchronous way to send a message here
        import requests
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": user_id, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload)

    except Exception as e:
        print(f"Error closing trade {trade_id}: {e}")
    return redirect(url_for('dashboard'))

if __name__ == "__main__":
    print("Starting Flask Web UI...")
    print("Open your browser and go to http://127.0.0.1:5000")
    app.run(debug=True, port=5000)