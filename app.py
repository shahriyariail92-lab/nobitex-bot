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
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ================= تنظیمات اصلی =================
TEST_MODE = True 
RISK_PER_TRADE_PERCENT = 0.02
MAX_SPREAD_PERCENT = 0.5 
DAILY_DRAWDOWN_LIMIT = -3.0 

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
if GEMINI_API_KEY: 
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '').strip()
if GROQ_API_KEY: 
    groq_client = groq.Groq(api_key=GROQ_API_KEY)

BALE_BOT_TOKEN = os.environ.get('BALE_BOT_TOKEN', '')
BALE_CHAT_ID = os.environ.get('BALE_CHAT_ID', '')
NOBITEX_API_TOKEN = os.environ.get('NOBITEX_API_TOKEN', '')

# ================= تنظیمات دیتابیس =================
db_url_str = os.environ.get('DATABASE_URL', 'sqlite:///local.db').strip()

# اصلاح لینک برای استفاده از psycopg2 در SQLAlchemy
if db_url_str.startswith("postgres://"):
    db_url_str = db_url_str.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url_str.startswith("postgresql://") and not db_url_str.startswith("postgresql+psycopg2://"):
    db_url_str = db_url_str.replace("postgresql://", "postgresql+psycopg2://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url_str
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
    position_size = db.Column(db.Float, default=0.0)
    active_symbol = db.Column(db.String(20), default="BTCUSDT") 
    daily_pnl_percent = db.Column(db.Float, default=0.0) 
    last_reset_date = db.Column(db.String(20), default="") 
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
    except Exception as e:
        print(f"Bale Error: {e}")

def safe_get_request(url, headers=None, retries=3):
    for attempt in range(retries):
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 429:
                time.sleep(5) 
                continue
            return res
        except Exception as e:
            time.sleep(2)
    return None

def get_account_balance():
    if TEST_MODE or not NOBITEX_API_TOKEN: return 1000.0
    url = "https://apiv2.nobitex.ir/users/wallets/list"
    headers = {"Authorization": f"Token {NOBITEX_API_TOKEN}"}
    res = safe_get_request(url, headers=headers)
    if res:
        try:
            data = res.json()
            if data.get('status') == 'ok':
                for wallet in data['wallets']:
                    if wallet['currency'] == 'usdt':
                        return float(wallet['balance'])
        except Exception:
            pass
    return 0.0

def get_ohlc_data(symbol, resolution=60, limit=50):
    end_time = int(time.time())
    start_time = end_time - (limit * resolution * 60)
    url = f"https://apiv2.nobitex.ir/market/udf/history?symbol={symbol}&resolution={resolution}&from={start_time}&to={end_time}"
    res = safe_get_request(url, headers={'User-Agent': 'Mozilla/5.0'})
    if res:
        try:
            data = res.json()
            if data.get('s') == 'ok':
                return {"open": data['o'], "high": data['h'], "low": data['l'], "close": data['c'], "volume": data['v']}
        except Exception:
            pass
    return None

# ================= اندیکاتورها =================
def calculate_ema(prices, period):
    if len(prices) < period: return None
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]: 
        ema = (price - ema) * multiplier + ema
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

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return None
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period

# ================= هسته اسکنر پامپ و دامپ =================
def perform_pump_dump_scan():
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    results = []
    
    for sym in symbols:
        try:
            ohlc = get_ohlc_data(symbol=sym, resolution=15, limit=30)
            if not ohlc or len(ohlc['close']) < 25: continue
            
            closes = ohlc['close'][:-1] 
            volumes = ohlc['volume'][:-1]
            
            current_price = closes[-1]
            current_vol = volumes[-1]
            avg_vol = sum(volumes[-20:]) / 20
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1
            
            if current_vol == 0: continue

            sma20 = sum(closes[-20:]) / 20
            variance = sum((x - sma20) ** 2 for x in closes[-20:]) / 20
            std_dev = variance ** 0.5
            bb_width = ((sma20 + (2 * std_dev)) - (sma20 - (2 * std_dev))) / sma20 * 100
            
            gains, losses = [], []
            for i in range(1, len(closes)):
                change = closes[i] - closes[i-1]
                gains.append(change if change > 0 else 0)
                losses.append(abs(change) if change < 0 else 0)
            
            avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else 0
            avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else 0
            rsi = 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss > 0 else 100
            
            status = "عادی"
            color = "#64748B"
            score = 0
            
            if bb_width < 3.0:
                status = "در حال جمع شدن انرژی (فشردگی)"
                color = "#F59E0B"
                score = int(85 - (bb_width * 10)) 
                if score < 50: score = 50

            if vol_ratio > 2.0: 
                if rsi > 65:
                    status = "هشدار دامپ (فروش نهنگ)"
                    color = "#EF4444"
                    score = min(100, int(70 + (vol_ratio * 5) + (rsi - 65)))
                elif rsi < 35:
                    status = "احتمال پامپ (خرید نهنگ)"
                    color = "#10B981"
                    score = min(100, int(70 + (vol_ratio * 5) + (35 - rsi)))

            results.append({
                "symbol": sym.replace("USDT", ""), "price": current_price, "vol_ratio": round(vol_ratio, 2),
                "bb_width": round(bb_width, 2), "rsi": round(rsi, 2), "status": status, "score": score, "color": color
            })
        except Exception as e:
            print(f"Error scanning {sym}: {e}")
            
    return results

# ================= ترد ۱: ربات معامله‌گر =================
def bot_logic_loop():
    while True:
        with app.app_context():
            state = BotState.query.first()
            
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if state.last_reset_date != today_str:
                state.daily_pnl_percent = 0.0
                state.last_reset_date = today_str
                db.session.commit()
                add_log("Daily PnL Reset to 0%.")
            
            if state.daily_pnl_percent <= DAILY_DRAWDOWN_LIMIT:
                if state.is_active:
                    state.is_active = False
                    db.session.commit()
                    add_log("CIRCUIT BREAKER TRIGGERED!")
                    send_bale_notification("ربات معامله‌گر: ترمز اضطراری فعال شد! ضرر روزانه به حد مجاز رسید.")
            
            if state and state.is_active:
                try:
                    current_symbol = state.active_symbol
                    ohlc = get_ohlc_data(symbol=current_symbol)
                    
                    if ohlc and len(ohlc['close']) > 0:
                        close_prices = ohlc['close']
                        current_price = close_prices[-1]
                        
                        ema9 = calculate_ema(close_prices, 9)
                        ema21 = calculate_ema(close_prices, 21)
                        supertrend = calculate_supertrend(ohlc['high'], ohlc['low'], close_prices)
                        macd = calculate_macd(close_prices)
                        atr = calculate_atr(ohlc['high'], ohlc['low'], close_prices, 14)
                        
                        if ema9 and ema21 and supertrend and macd is not None and atr:
                            if not state.in_position and ema9 > ema21 and supertrend == 1 and macd > 0:
                                balance = get_account_balance()
                                sl_price = current_price - (atr * 1.5)
                                price_diff = abs(current_price - sl_price)
                                
                                raw_size = (balance * RISK_PER_TRADE_PERCENT) / price_diff if price_diff > 0 else 0
                                size = math.floor(raw_size * 1e6) / 1e6 
                                
                                if size > 0:
                                    state.in_position = True
                                    state.position_type = "long"
                                    state.entry_price = current_price
                                    state.stop_loss = sl_price
                                    state.take_profit = current_price + (atr * 3)
                                    state.position_size = size
                                    db.session.commit()
                                    msg = f"ربات معامله‌گر: LONG {current_symbol} باز شد! ورود: {current_price:.2f} SL: {sl_price:.2f} حجم: {size}"
                                    add_log(f"LONG Opened on {current_symbol}.")
                                    send_bale_notification(msg)
                            
                            elif not state.in_position and ema9 < ema21 and supertrend == -1 and macd < 0:
                                balance = get_account_balance()
                                sl_price = current_price + (atr * 1.5)
                                price_diff = abs(sl_price - current_price)
                                
                                raw_size = (balance * RISK_PER_TRADE_PERCENT) / price_diff if price_diff > 0 else 0
                                size = math.floor(raw_size * 1e6) / 1e6
                                
                                if size > 0:
                                    state.in_position = True
                                    state.position_type = "short"
                                    state.entry_price = current_price
                                    state.stop_loss = sl_price
                                    state.take_profit = current_price - (atr * 3)
                                    state.position_size = size
                                    db.session.commit()
                                    msg = f"ربات معامله‌گر: SHORT {current_symbol} باز شد! ورود: {current_price:.2f} SL: {sl_price:.2f} حجم: {size}"
                                    add_log(f"SHORT Opened on {current_symbol}.")
                                    send_bale_notification(msg)

                            elif state.in_position:
                                if (state.position_type == "long" and current_price <= state.stop_loss) or \
                                   (state.position_type == "short" and current_price >= state.stop_loss):
                                    add_log("STOP LOSS HIT!")
                                    send_bale_notification("ربات معامله‌گر: حد ضرر فعال شد.")
                                    state.in_position = False
                                    state.daily_pnl_percent -= 2.0 
                                    db.session.commit()
                                
                                elif (state.position_type == "long" and current_price >= state.take_profit) or \
                                     (state.position_type == "short" and current_price <= state.take_profit):
                                    add_log("TAKE PROFIT HIT!")
                                    send_bale_notification("ربات معامله‌گر: حد سود فعال شد.")
                                    state.in_position = False
                                    state.daily_pnl_percent += 4.0 
                                    db.session.commit()
                            else:
                                add_log(f"Analyzing {current_symbol}... Price: {current_price:.2f}")
                except Exception as e:
                    add_log(f"Loop Error: {e}")
        time.sleep(15)

# ================= ترد ۲: اسکنر خودکار پامپ و دامپ =================
last_alerts = {} 

def pump_dump_auto_loop():
    time.sleep(30) 
    while True:
        try:
            results = perform_pump_dump_scan()
            current_time = time.time()
            
            for item in results:
                if item['score'] >= 85:
                    sym = item['symbol']
                    if sym not in last_alerts or (current_time - last_alerts[sym] > 3600):
                        msg = f"رادار پامپ/دامپ: سیگنال {item['status']} برای {sym}! قیمت: {item['price']:.4f} احتمال: {item['score']}%"
                        send_bale_notification(msg)
                        last_alerts[sym] = current_time 
                        
        except Exception as e:
            print(f"Pump Dump Loop Error: {e}")
            
        time.sleep(300) 

# ================= اجرای تردها =================
threads_started = False
@app.before_request
def start_background_threads():
    global threads_started
    if not threads_started:
        threading.Thread(target=bot_logic_loop, daemon=True).start()
        threading.Thread(target=pump_dump_auto_loop, daemon=True).start()
        threads_started = True

# ================= API Routes =================
@app.route('/status', methods=['GET'])
def get_status():
    state = BotState.query.first()
    return jsonify({
        "is_active": state.is_active, "in_position": state.in_position, 
        "position_type": state.position_type, "entry_price": state.entry_price,
        "stop_loss": state.stop_loss, "take_profit": state.take_profit,
        "position_size": state.position_size, "active_symbol": state.active_symbol,
        "daily_pnl": round(state.daily_pnl_percent, 2),
        "logs": state.logs.split('\n')
    })

@app.route('/toggle', methods=['POST'])
def toggle_bot():
    state = BotState.query.first()
    if state.daily_pnl_percent <= DAILY_DRAWDOWN_LIMIT and not state.is_active:
        return jsonify({"status": "error", "message": "ترمز اضطراری فعال است!"}), 400
    state.is_active = not state.is_active
    db.session.commit()
    add_log(f"User turned the bot {'ON' if state.is_active else 'OFF'}.")
    return jsonify({"status": "success", "is_active": state.is_active})

@app.route('/set_symbol', methods=['POST'])
def set_symbol():
    state = BotState.query.first()
    if state.in_position:
        return jsonify({"status": "error", "message": "شما پوزیشن باز دارید!"}), 400
    data = request.json
    new_symbol = data.get('symbol')
    if new_symbol:
        state.active_symbol = new_symbol
        state.logs = f"Switched to {new_symbol}."
        db.session.commit()
        return jsonify({"status": "success", "symbol": new_symbol})
    return jsonify({"status": "error", "message": "ارز نامعتبر است."}), 400

@app.route('/close_position', methods=['POST'])
def close_position():
    state = BotState.query.first()
    if state.in_position:
        state.in_position = False
        db.session.commit()
        add_log("Panic Close Executed.")
        return jsonify({"status": "success", "message": "Position closed."})
    return jsonify({"status": "error", "message": "No open position."})

@app.route('/scan_pump_dump', methods=['GET'])
def api_scan_pump_dump():
    return jsonify(perform_pump_dump_scan())

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
        
        prompt = "You are a technical analyst. Analyze the chart image and provide a comprehensive analysis in Persian (with new lines for each section): 1. Overall Trend 2. Support & Resistance 3. Indicators 4. Patterns 5. Trading Plan. Keep it concise."
        
        gemini_err = ""
        groq_err = ""

        # استفاده از مدل‌های جدید گوگل
        if GEMINI_API_KEY:
            try:
                response = gemini_client.models.generate_content(
                    model='gemini-2.5-flash', 
                    contents=[prompt, img]
                )
                return jsonify({"analysis": response.text})
            except Exception as e: 
                gemini_err = str(e)
            
        # استفاده از مدل جدید Groq
        if GROQ_API_KEY:
            try:
                response = groq_client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct", 
                    messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64_image}}]}],
                    temperature=0.5, max_tokens=1000
                )
                return jsonify({"analysis": response.choices[
