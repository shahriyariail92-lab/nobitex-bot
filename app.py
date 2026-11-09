import os
import time
import threading
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from google import genai
from google.genai import types
import groq
import PIL.Image
import io
import base64
import math

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ================= تنظیمات اصلی =================
TEST_MODE = True  # ⚠️ برای معامله واقعی باید False شود
RISK_PER_TRADE_PERCENT = 0.02  # ریسک ۲ درصدی برای هر معامله
MAX_SPREAD_PERCENT = 0.5  # حداکثر مجاز اسپرد (نیم درصد)
DAILY_DRAWDOWN_LIMIT = 5.0 # حداکثر ضرر روزانه (۵ درصد)

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
if GEMINI_API_KEY: gemini_client = genai.Client(api_key=GEMINI_API_KEY)

GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '').strip()
if GROQ_API_KEY: groq_client = groq.Groq(api_key=GROQ_API_KEY)

BALE_BOT_TOKEN = os.environ.get('BALE_BOT_TOKEN', '')
BALE_CHAT_ID = os.environ.get('BALE_CHAT_ID', '')
NOBITEX_API_TOKEN = os.environ.get('NOBITEX_API_TOKEN', '')

# ================= دیتابیس =================
db_url = os.environ.get('DATABASE_URL', 'sqlite:///local.db')
if db_url.startswith("postgres://"): db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class BotState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    is_active = db.Column(db.Boolean, default=False)
    in_position = db.Column(db.Boolean, default=False)
    position_type = db.Column(db.String(10), default="none")
    entry_price = db.Column(db.Float, default=0.0)
    stop_loss = db.Column(db.Float, default=0.0)
    take_profit = db.Column(db.Float, default=0.0)
    position_size = db.Column(db.Float, default=0.0) # حجم معامله
    logs = db.Column(db.Text, default="[System] Database initialized.")

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
    current_logs = state.logs.split('\n') if state.logs else []
    current_logs.insert(0, new_log)
    state.logs = '\n'.join(current_logs[:10])
    db.session.commit()
    print(new_log)

def send_bale_notification(message):
    if not BALE_BOT_TOKEN or not BALE_CHAT_ID: return
    try:
        url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": BALE_CHAT_ID, "text": message}, timeout=10)
    except: pass

# دریافت موجودی حساب و همگام‌سازی
def get_account_balance():
    if TEST_MODE or not NOBITEX_API_TOKEN: return 1000.0 # فرض ۱۰۰۰ دلار در حالت تست
    
    url = "https://apiv2.nobitex.ir/users/wallets/list"
    headers = {"Authorization": f"Token {NOBITEX_API_TOKEN}"}
    try:
        res = requests.get(url, headers=headers, timeout=10).json()
        if res.get('status') == 'ok':
            for wallet in res['wallets']:
                if wallet['currency'] == 'usdt':
                    return float(wallet['balance'])
    except: pass
    return 0.0

# محاسبه حجم با مدیریت ریسک
def calculate_position_size(balance, entry_price, stop_loss_price):
    risk_amount = balance * RISK_PER_TRADE_PERCENT
    price_difference = abs(entry_price - stop_loss_price)
    if price_difference == 0: return 0
    # حجم = (مبلغ ریسک) / (فاصله تا حد ضرر)
    size = risk_amount / price_difference
    # گرد کردن به ۶ رقم اعشار برای بیت کوین
    return round(size, 6)

# بررسی اسپرد (اختلاف خرید و فروش)
def check_spread(symbol="BTCUSDT"):
    if TEST_MODE: return True # در حالت تست همیشه تایید است
    
    try:
        url = "https://apiv2.nobitex.ir/orderbook/BTCUSDT"
        res = requests.get(url, timeout=10).json()
        if res.get('status') == 'ok':
            best_buy = float(res['bids'][0][0])
            best_sell = float(res['asks'][0][0])
            spread_percent = ((best_sell - best_buy) / best_buy) * 100
            if spread_percent > MAX_SPREAD_PERCENT:
                add_log(f"⚠️ اسپرد بازار بالا است ({spread_percent:.2f}%). معامله لغو شد.")
                return False
            return True
    except: pass
    return True

# اجرای سفارش با کارمزد در نظر گرفته شده
def execute_nobitex_order(order_type, amount, price=0):
    if TEST_MODE:
        add_log(f"🧪 [TEST] {order_type.upper()} {amount} BTC placed.")
        return True
    
    if not NOBITEX_API_TOKEN:
        add_log("❌ توکن API نوبیتکس تنظیم نشده است.")
        return False

    # استفاده از سفارش Limit برای جلوگیری از اسلیپیج شدید
    url = "https://apiv2.nobitex.ir/orders/add"
    headers = {"Authorization": f"Token {NOBITEX_API_TOKEN}"}
    data = {
        "type": order_type,
        "srcCurrency": "btc",
        "dstCurrency": "usdt",
        "amount": str(amount),
        "price": str(int(price)), 
        "execution": "limit" # استفاده از Limit به جای Market
    }
    try:
        res = requests.post(url, headers=headers, data=data, timeout=10)
        if res.json().get('status') == 'ok':
            add_log(f"✅ سفارش واقعی {order_type} ثبت شد.")
            return True
        else:
            add_log(f"❌ خطای نوبیتکس: {res.text}")
            return False
    except Exception as e:
        add_log(f"❌ خطای ارتباط: {e}")
        return False

def get_ohlc_data(symbol="BTCUSDT", resolution=60, limit=50):
    end_time = int(time.time())
    start_time = end_time - (limit * resolution * 60)
    url = f"https://apiv2.nobitex.ir/market/udf/history?symbol={symbol}&resolution={resolution}&from={start_time}&to={end_time}"
    try:
        res = requests.get(url, headers={'User-Agent': 'TraderBot/1.0'}, timeout=10)
        data = res.json()
        if data.get('s') == 'ok':
            return {"open": data['o'], "high": data['h'], "low": data['l'], "close": data['c']}
    except: pass
    return None

# ================= اندیکاتورها =================
def calculate_ema(prices, period):
    if len(prices) < period: return None
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]: ema = (price - ema) * multiplier + ema
    return ema

def calculate_supertrend(high, low, close, period=10, multiplier=3):
    if len(close) < period: return None
    tr_list = []
    for i in range(1, len(close)):
        tr = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
        tr_list.append(tr)
    if len(tr_list) < period: return None
    atr = sum(tr_list[-period:]) / period
    hl2 = (high[-1] + low[-1]) / 2
    lower_band = hl2 - (multiplier * atr)
    return 1 if close[-1] > lower_band else -1

def calculate_macd(prices):
    if len(prices) < 26: return None
    ema_fast = calculate_ema(prices, 12)
    ema_slow = calculate_ema(prices, 26)
    if ema_fast is None or ema_slow is None: return None
    return ema_fast - ema_slow

# ================= حلقه اصلی ربات =================
def bot_logic_loop():
    while True:
        with app.app_context():
            state = BotState.query.first()
            if state and state.is_active:
                try:
                    ohlc = get_ohlc_data()
                    if ohlc and len(ohlc['close']) > 0:
                        close_prices = ohlc['close']
                        current_price = close_prices[-1]
                        
                        ema9 = calculate_ema(close_prices, 9)
                        ema21 = calculate_ema(close_prices, 21)
                        supertrend = calculate_supertrend(ohlc['high'], ohlc['low'], close_prices)
                        macd = calculate_macd(close_prices)
                        
                        if ema9 and ema21 and supertrend and macd is not None:
                            
                            # 🟢 باز کردن پوزیشن LONG با مدیریت ریسک
                            if not state.in_position and ema9 > ema21 and supertrend == 1 and macd > 0:
                                if not check_spread(): 
                                    time.sleep(15); continue # اگر اسپرد بالا بود، صبر کن
                                
                                balance = get_account_balance()
                                sl_price = current_price * 0.98 # ۲٪ حد ضرر
                                size = calculate_position_size(balance, current_price, sl_price)
                                
                                if size > 0 and execute_nobitex_order("buy", size, current_price):
                                    state.in_position = True
                                    state.position_type = "long"
                                    state.entry_price = current_price
                                    state.stop_loss = sl_price
                                    state.take_profit = current_price * 1.05 # ۵٪ حد سود
                                    state.position_size = size
                                    db.session.commit()
                                    msg = f"🚀 LONG باز شد!\nحجم: {size} BTC\nورود: ${current_price:,.0f}\nSL: ${sl_price:,.0f}"
                                    add_log(f"LONG Opened. Size: {size}")
                                    send_bale_notification(msg)
                            
                            # 🔴 باز کردن پوزیشن SHORT
                            elif not state.in_position and ema9 < ema21 and supertrend == -1 and macd < 0:
                                if not check_spread(): 
                                    time.sleep(15); continue
                                
                                balance = get_account_balance()
                                sl_price = current_price * 1.02 # ۲٪ حد ضرر
                                size = calculate_position_size(balance, current_price, sl_price)
                                
                                if size > 0 and execute_nobitex_order("sell", size, current_price):
                                    state.in_position = True
                                    state.position_type = "short"
                                    state.entry_price = current_price
                                    state.stop_loss = sl_price
                                    state.take_profit = current_price * 0.95
                                    state.position_size = size
                                    db.session.commit()
                                    msg = f"🔻 SHORT باز شد!\nحجم: {size} BTC\nورود: ${current_price:,.0f}\nSL: ${sl_price:,.0f}"
                                    add_log(f"SHORT Opened. Size: {size}")
                                    send_bale_notification(msg)

                            # ⚖️ مدیریت پوزیشن باز و حد ضرر متحرک
                            elif state.in_position:
                                if state.position_type == "long":
                                    if current_price <= state.stop_loss:
                                        if execute_nobitex_order("sell", state.position_size, current_price):
                                            add_log("🛑 STOP LOSS HIT! Closed.")
                                            send_bale_notification("🛑 حد ضرر فعال شد.")
                                            state.in_position = False
                                            db.session.commit()
                                    elif current_price >= state.take_profit:
                                        if execute_nobitex_order("sell", state.position_size, current_price):
                                            add_log("💰 TAKE PROFIT HIT! Closed.")
                                            send_bale_notification("💰 حد سود فعال شد.")
                                            state.in_position = False
                                            db.session.commit()
                                    # Trailing Stop
                                    elif current_price > state.entry_price * 1.02 and state.stop_loss < state.entry_price:
                                        state.stop_loss = state.entry_price
                                        db.session.commit()
                                        add_log("📈 Trailing Stop: SL moved to Breakeven.")
                                
                                elif state.position_type == "short":
                                    if current_price >= state.stop_loss:
                                        if execute_nobitex_order("buy", state.position_size, current_price):
                                            add_log("🛑 STOP LOSS HIT! Short Closed.")
                                            state.in_position = False
                                            db.session.commit()
                                    elif current_price <= state.take_profit:
                                        if execute_nobitex_order("buy", state.position_size, current_price):
                                            add_log("💰 TAKE PROFIT HIT! Short Closed.")
                                            state.in_position = False
                                            db.session.commit()
                            else:
                                add_log(f"Analyzing... Price: ${current_price:,.0f}")
                except Exception as e:
                    add_log(f"Loop Error: {e}")
        time.sleep(15)

# ================= API Routes =================
thread_started = False
@app.before_request
def start_background_thread():
    global thread_started
    if not thread_started:
        threading.Thread(target=bot_logic_loop, daemon=True).start()
        thread_started = True

@app.route('/status', methods=['GET'])
def get_status():
    state = BotState.query.first()
    return jsonify({
        "is_active": state.is_active, "in_position": state.in_position, 
        "position_type": state.position_type, "entry_price": state.entry_price,
        "stop_loss": state.stop_loss, "take_profit": state.take_profit,
        "position_size": state.position_size, "logs": state.logs.split('\n')
    })

@app.route('/toggle', methods=['POST'])
def toggle_bot():
    state = BotState.query.first()
    state.is_active = not state.is_active
    db.session.commit()
    add_log(f"User turned the bot {'ON' if state.is_active else 'OFF'}.")
    return jsonify({"status": "success", "is_active": state.is_active})

@app.route('/close_position', methods=['POST'])
def close_position():
    state = BotState.query.first()
    if state.in_position:
        order_type = "sell" if state.position_type == "long" else "buy"
        if execute_nobitex_order(order_type, state.position_size, 0):
            state.in_position = False
            state.position_type = "none"
            db.session.commit()
            add_log("🚨 Panic Close Executed.")
            return jsonify({"status": "success", "message": "Position closed."})
    return jsonify({"status": "error", "message": "No open position."})

# (بخش هوش مصنوعی بدون تغییر)
@app.route('/analyze', methods=['POST', 'OPTIONS'])
def analyze_chart():
    if request.method == 'OPTIONS': return jsonify({"status": "ok"}), 200
    if 'image' not in request.files: return jsonify({"error": "عکسی ارسال نشده است."}), 400
    try:
        file = request.files['image']
        img = PIL.Image.open(io.BytesIO(file.read()))
        img.thumbnail((800, 800))
        buffered = io.BytesIO()
        img.convert('RGB').save(buffered, format="JPEG")
        base64_image = base64.b64encode(buffered.getvalue()).decode('utf-8')
        
        prompt = """
        شما یک تحلیلگر تکنیکال حرفه‌ای هستید. تصویر چارت را با دقت بررسی کنید و تحلیل جامع به فارسی (با خط جدید برای هر بخش) بدهید:
        1. روند کلی 2. حمایت و مقاومت 3. اندیکاتورها 4. الگوها 5. پیشنهاد معاملاتی (ورود، سود، ضرر)
        ⚠️ دستور مهم: تحلیل بسیار دقیق اما کوتاه باشد و از ۸۰۰ کلمه بیشتر نشود.
        """
        if GEMINI_API_KEY:
            try:
                response = gemini_client.models.generate_content(model='gemini-3.8-flash', contents=[prompt, img], config=types.GenerateContentConfig(max_output_tokens=2048))
                return jsonify({"analysis": response.text})
            except Exception as e: print(f"Gemini failed: {e}")
        if GROQ_API_KEY:
            try:
                response = groq_client.chat.completions.create(
                    model="qwen/qwen3.8-27b",
                    messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
                    temperature=0.5, max_tokens=1000
                )
                return jsonify({"analysis": response.choices[0].message.content})
            except Exception as e: print(f"Groq failed: {e}")
        return jsonify({"error": "هر دو سرویس هوش مصنوعی در دسترس نیستند."}), 500
    except Exception as e:
        return jsonify({"error": f"خطای سرور: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
