# AI-Powered Cryptocurrency Trading Bot

An intelligent cryptocurrency trading bot that combines computer vision, machine learning, and Telegram integration to automatically execute trades based on signals from Telegram channels and price chart analysis.

## 🚀 Features

### Core Trading Capabilities
- **Automated Trade Execution**: Executes long/short positions on Binance futures
- **Paper Trading Mode**: Safe testing environment with simulated trading
- **Real-time Price Monitoring**: Continuous market price tracking with 3-second intervals
- **Risk Management**: Configurable stop-loss, take-profit, and position sizing
- **Multi-position Support**: Manages up to 10 concurrent long and 10 short positions

### AI-Powered Price Detection
- **Computer Vision Integration**: Uses YOLO (You Only Look Once) for object detection
- **OCR Text Recognition**: TrOCR model for extracting price values from chart images
- **Automatic Price Extraction**: Identifies entry, stop-loss, and target prices from screenshots
- **Custom Trained Model**: Pre-trained YOLO model specifically for trading chart elements

### Telegram Integration
- **Signal Monitoring**: Monitors specified Telegram channels for trading signals
- **Smart Filtering**: AI-powered keyword detection (buy, sell, long, short, etc.)
- **Coin Alias Support**: Maps common coin names to their trading pairs
- **Interactive Bot Interface**: Telegram bot for manual control and monitoring

### Web Dashboard
- **Real-time Monitoring**: Live dashboard showing active trades and performance
- **Performance Analytics**: Win rate, profit/loss, and trading statistics
- **Portfolio Overview**: Current balance, open positions, and trade history
- **Responsive Design**: Modern web interface accessible from any device

## 📋 Prerequisites

- Python 3.8+
- CUDA-capable GPU (recommended for AI models)
- Telegram Bot Token
- Telegram API credentials (for channel monitoring)
- Binance API credentials (for live trading)

## 🛠️ Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd trading-bot
   ```

2. **Create virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Download AI models**
   - Ensure you have the trained YOLO model at `runs/detect/train/weights/best.pt`
   - TrOCR model will be downloaded automatically on first run

5. **Configure environment variables**
   Create a `.env` file with the following variables:
   ```env
   # Telegram Bot Configuration
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   AUTHORIZED_USER_ID=your_telegram_user_id

   # Telegram API (for channel monitoring)
   API_ID=your_api_id
   API_HASH=your_api_hash

   # Binance API (for live trading)
   BINANCE_API_KEY=your_api_key
   BINANCE_SECRET_KEY=your_secret_key

   # Optional: Trading Configuration
   INITIAL_BALANCE=1000.00
   MAX_CONCURRENT_LONGS=10
   MAX_CONCURRENT_SHORTS=10
   ```

## 🎯 Usage

### Paper Trading Mode (Recommended for Testing)

1. **Start the Telegram bot**
   ```bash
   python bot.py
   ```

2. **Start channel monitoring**
   ```bash
   python channel_monitor.py
   ```

3. **Launch web dashboard**
   ```bash
   python web_ui.py
   ```

### Live Trading Mode

1. **Start the testnet version**
   ```bash
   python bot_testnet.py
   python channel_monitor_testnet.py
   python web_ui_testnet.py
   ```

2. **For live trading**, modify the exchange configuration in the bot files to use live endpoints

### Web Dashboard

Access the dashboard at `http://localhost:5000` to:
- View active trades and positions
- Monitor performance metrics
- Check current balance and P&L
- Review trade history

### Telegram Bot Commands

- `/start` - Initialize the bot and check balance
- `/balance` - Display current balance
- `/positions` - Show all open positions
- `/close_all` - Close all open positions
- `/stats` - Display trading statistics

## 🏗️ Project Structure

```
trading-bot/
├── bot.py                    # Main trading bot (paper trading)
├── bot_testnet.py           # Testnet trading bot
├── channel_monitor.py       # Telegram channel monitoring
├── channel_monitor_testnet.py # Testnet channel monitoring
├── database.py              # Database operations and schema
├── extract_price.py         # AI-powered price extraction
├── web_ui.py               # Web dashboard
├── web_ui_testnet.py       # Testnet web dashboard
├── symbol_aliases.json     # Coin name mappings
├── trading.yaml            # YOLO model configuration
├── requirements.txt        # Python dependencies
├── templates/              # HTML templates
│   ├── index.html
│   └── live_dashboard.html
├── static/                 # CSS and static files
│   └── style.css
├── runs/detect/train/      # AI model files
├── price detection.v1i.yolov8/ # YOLO dataset
└── *.db                   # SQLite databases
```

## 🤖 AI Model Details

### YOLO Object Detection
- **Purpose**: Detects price regions (entry, stop-loss, target) in chart screenshots
- **Model**: Custom trained YOLOv8 model
- **Classes**: 3 classes (entry, stoploss, target)
- **Input**: Chart screenshots from Telegram channels

### TrOCR Text Recognition
- **Purpose**: Extracts numerical price values from detected regions
- **Model**: Microsoft's TrOCR-large-printed
- **Capabilities**: Handles various fonts, orientations, and image qualities
- **Output**: Cleaned numerical price values

## 🔧 Configuration

### Trading Parameters
- **Poll Interval**: 3 seconds (configurable)
- **Market Order Tolerance**: 0.25% (configurable)
- **Market Cap Threshold**: $50M for penny coins
- **Max Positions**: 10 longs + 10 shorts (configurable)

### Risk Management
- **Stop Loss**: Automatically set based on chart analysis
- **Take Profit**: Multiple targets supported
- **Position Sizing**: Configurable based on account balance

### Filtering System
- **Buy Keywords**: buy, long, bullish, buying, bought, longed
- **Sell Keywords**: sell, short, bearish, selling, sold, shorted
- **Blacklisted Coins**: ETH, BTC (configurable)

## 📊 Database Schema

The bot uses SQLite with three main tables:

- **settings**: Key-value configuration storage
- **open_trades**: Active position tracking
- **trade_history**: Completed trade records

## 🚨 Safety Features

- **Paper Trading**: Default mode prevents real money loss
- **User Authorization**: Only authorized users can control the bot
- **Error Handling**: Comprehensive error handling and logging
- **Rate Limiting**: Built-in API rate limit management
- **Position Limits**: Maximum concurrent position limits

## 📈 Performance Monitoring

The bot tracks various metrics:
- Win rate percentage
- Total profit/loss
- Profit factor
- Average win/loss
- Number of trades
- Current balance

## 🔍 Troubleshooting

### Common Issues

1. **Models not loading**
   - Ensure CUDA is properly installed for GPU acceleration
   - Check model file paths in `extract_price.py`

2. **Telegram API errors**
   - Verify API credentials in `.env` file
   - Ensure bot token is valid and authorized

3. **Binance connection issues**
   - Check API key permissions
   - Verify network connectivity
   - Ensure API keys have futures trading permissions

### Logs
- All operations are logged with timestamps
- Check console output for detailed error messages
- Database logs available in SQLite files

## ⚠️ Disclaimer

This software is for educational and research purposes only. Cryptocurrency trading involves substantial risk of loss. Never trade with money you cannot afford to lose. The developers are not responsible for any financial losses incurred through the use of this software.

## 📝 License

[Add your license information here]

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📞 Support

For support and questions:
- Create an issue in the repository
- Check the troubleshooting section
- Review the logs for error details

---

**Happy Trading! 🚀📈**
