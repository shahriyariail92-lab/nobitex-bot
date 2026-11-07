import os
import time
import threading
import requests
from flask import Flask, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
CORS(app)

# ================= تنظیمات دیتابیس =================
# دریافت آدرس دیتابیس از تنظیمات Render
db_url = os.environ.get('DATABASE_URL', 'sqlite:///local.db')

# رفع یک باگ رایج: Render آدرس را با postgres:// می‌دهد اما SQLAlchemy به postgresql:// نیاز دارد
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ================= مدل دیتابیس =================
class BotState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    is_active = db.Column(db.Boolean, default=False)
    in_position = db.Column(db.Boolean, default=False)
    entry_price = db.Column(db.Float, default=0.0)
    logs = db.Column(db.Text, default="[System] Database initialized. Waiting for user...")

# ساخت جداول در دیتابیس (فقط بار اول اجرا می‌شود)
with app.app_context():
    db.create_all()
    if not BotState.query.first():
        db.session.add(BotState())
        db.session.commit()

# ================= توابع کمکی =================
def add_log(message):
    state = BotState.query.first()
    timestamp = time.strftime("%H:%M:%S")
    new_log = f"[{timestamp}] {message}"
    
    # نگه داشتن فقط ۱۰ لاگ آخر
    current_logs = state.logs.split('\n') if state.logs else []
    current_logs.insert(0, new_log)
    state.logs = '\n'.join(current_logs[:10])
    db.session.commit()
    print(new_log) # چاپ در لاگ‌های Render

def get_ohlc_data(symbol="BTCUSDT", resolution=60, limit=20):
    end_time = int(time.time())
    start_time = end_time - (limit * resolution * 60)
    url = f"https://apiv2.nobitex.ir/market/udf/history?symbol={symbol}&resolution={resolution}&from={start_time}&to={end_time}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.json().get('s') == 'ok': 
            return res.json()['c']
    except Exception as e:
        print(f"OHLC Error: {e}")
    return None

def calculate_rsi(prices, period=14):
    if not prices or len(prices) < period + 1: 
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(change if change > 0 else 0)
        losses.append(abs(change) if change < 0 else 0)
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0: 
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ================= حلقه اصلی ربات =================
def bot_logic_loop():
    print(">>> BACKGROUND THREAD STARTED <<<")
    while True:
        with app.app_context():
            state = BotState.query.first()
            
            if state and state.is_active:
                try:
                    # دریافت قیمت لحظه‌ای
                    url = "https://apiv2.nobitex.ir/market/stats?srcCurrency=btc&dstCurrency=usdt"
                    headers = {'User-Agent': 'Mozilla/5.0'}
                    res_price = requests.get(url, headers=headers, timeout=10).json()
                    current_price = float(res_price['stats']['btc-usdt']['latest'])

                    # دریافت کندل‌ها و محاسبه RSI
                    close_prices = get_ohlc_data()
                    if close_prices:
                        rsi = calculate_rsi(close_prices)
                        
                        # منطق خرید
                        if not state.in_position and rsi < 30:
                            add_log(f"🚨 BUY SIGNAL! RSI: {rsi:.2f}. (Paper Trading)")
                            state.in_position = True
                            state.entry_price = current_price
                            db.session.commit()
                            
                        # منطق فروش
                        elif state.in_position and rsi > 70:
                            add_log(f"🔻 SELL SIGNAL! RSI: {rsi:.2f}. (Paper Trading)")
                            state.in_position = False
                            state.entry_price = 0.0
                            db.session.commit()
                            
                        # وضعیت انتظار
                        else:
                            add_log(f"Analyzing... Price: ${current_price:,.0f}, RSI: {rsi:.2f}")
                            
                except Exception as e:
                    add_log(f"Loop Error: {e}")
        
        # هر ۱۵ ثانیه چک میکند
        time.sleep(15)

# شروع ترد پس‌زمینه دقیقاً بعد از اولین درخواست
thread_started = False
@app.before_request
def start_background_thread():
    global thread_started
    if not thread_started:
        threading.Thread(target=bot_logic_loop, daemon=True).start()
        thread_started = True

# ================= API Routes =================
@app.route('/status', methods=['GET'])
def get_status():
    state = BotState.query.first()
    return jsonify({
        "is_active": state.is_active,
        "in_position": state.in_position,
        "entry_price": state.entry_price,
        "logs": state.logs.split('\n')
    })

@app.route('/toggle', methods=['POST'])
def toggle_bot():
    state = BotState.query.first()
    state.is_active = not state.is_active
    db.session.commit()
    add_log(f"User turned the bot {'ON' if state.is_active else 'OFF'}.")
    return jsonify({"status": "success", "is_active": state.is_active})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
