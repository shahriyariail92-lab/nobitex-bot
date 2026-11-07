import os
from flask import Flask, jsonify
from flask_cors import CORS
import requests
import time
import threading

app = Flask(__name__)
CORS(app)

bot_state = {
    "is_active": False,
    "price": 0.0,
    "rsi": 0.0,
    "in_position": False,
    "logs": ["[System] Cloud Server Started. Waiting for user to activate bot..."]
}

STOP_LOSS_PERCENT = 0.02 
TAKE_PROFIT_PERCENT = 0.05

def add_log(message):
    timestamp = time.strftime("%H:%M:%S")
    bot_state["logs"].insert(0, f"[{timestamp}] {message}")
    if len(bot_state["logs"]) > 10:
        bot_state["logs"].pop()

def get_ohlc_data(symbol="BTCUSDT", resolution=60, limit=20):
    end_time = int(time.time())
    start_time = end_time - (limit * resolution * 60)
    url = f"https://apiv2.nobitex.ir/market/udf/history?symbol={symbol}&resolution={resolution}&from={start_time}&to={end_time}"
    try:
        res = requests.get(url, timeout=10)
        if res.json().get('s') == 'ok': return res.json()['c']
    except: pass
    return None

def calculate_rsi(prices, period=14):
    if not prices or len(prices) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(change if change > 0 else 0)
        losses.append(abs(change) if change < 0 else 0)
    avg_gain, avg_loss = sum(gains[:period])/period, sum(losses[:period])/period
    if avg_loss == 0: return 100
    return 100 - (100 / (1 + (avg_gain / avg_loss)))

def bot_logic_loop():
    while True:
        if bot_state["is_active"]:
            try:
                res_price = requests.get("https://apiv2.nobitex.ir/market/stats?srcCurrency=btc&dstCurrency=usdt", timeout=10).json()
                current_price = float(res_price['stats']['btc-usdt']['latest'])
                bot_state["price"] = current_price

                close_prices = get_ohlc_data()
                if close_prices:
                    rsi = calculate_rsi(close_prices)
                    bot_state["rsi"] = rsi
                    
                    if not bot_state["in_position"] and rsi < 30:
                        add_log(f"BUY SIGNAL! RSI: {rsi:.2f}. Opening Position...")
                        bot_state["in_position"] = True
                    elif bot_state["in_position"] and rsi > 70:
                        add_log(f"SELL SIGNAL! RSI: {rsi:.2f}. Closing Position...")
                        bot_state["in_position"] = False
                    else:
                        add_log(f"Analyzing... Price: ${current_price:,.0f}, RSI: {rsi:.2f}")
            except Exception as e:
                add_log(f"Error: {e}")
        
        time.sleep(15)

# شروع حلقه ربات در پس‌زمینه
threading.Thread(target=bot_logic_loop, daemon=True).start()

@app.route('/status', methods=['GET'])
def get_status():
    return jsonify(bot_state)

@app.route('/toggle', methods=['POST'])
def toggle_bot():
    bot_state["is_active"] = not bot_state["is_active"]
    status_text = "ON" if bot_state["is_active"] else "OFF"
    add_log(f"User turned the bot {status_text}.")
    return jsonify({"status": "success", "is_active": bot_state["is_active"]})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
