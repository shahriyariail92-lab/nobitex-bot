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

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ================= تنظیمات هوش مصنوعی (دوگانه) =================
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '').strip()
if GROQ_API_KEY:
    groq_client = groq.Groq(api_key=GROQ_API_KEY)

# ================= تنظیمات بله =================
BALE_BOT_TOKEN = os.environ.get('BALE_BOT_TOKEN', '')
BALE_CHAT_ID = os.environ.get('BALE_CHAT_ID', '')

# ================= دیتابیس =================
db_url = os.environ.get('DATABASE_URL', 'sqlite:///local.db')
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class BotState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    is_active = db.Column(db.Boolean, default=False)
    in_position = db.Column(db.Boolean, default=False)
    entry_price = db.Column(db.Float, default=0.0)
    logs = db.Column(db.Text, default="[System] Database initialized. Waiting for user...")

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
        payload = {"chat_id": BALE_CHAT_ID, "text": message}
        requests.post(url, json=payload, timeout=10)
    except: pass

def get_ohlc_data(symbol="BTCUSDT", resolution=60, limit=50, trade_type="margin"):
    end_time = int(time.time())
    start_time = end_time - (limit * resolution * 60)
    url = f"https://apiv2.nobitex.ir/market/udf/history?symbol={symbol}&resolution={resolution}&from={start_time}&to={end_time}"
    headers = {'User-Agent': 'TraderBot/Technicalist-1.0.0'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        if data.get('s') == 'ok':
            return {"open": data['o'], "high": data['h'], "low": data['l'], "close": data['c']}
    except Exception as e:
        print(f"OHLC Error: {e}")
    return None

# ================= اندیکاتورهای فیوچرز =================
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

# ================= حلقه اصلی ربات =================
def bot_logic_loop():
    while True:
        with app.app_context():
            state = BotState.query.first()
            if state and state.is_active:
                try:
                    add_log("Checking Futures Market...")
                    ohlc = get_ohlc_data(symbol="BTCUSDT", resolution=60, limit=50, trade_type="margin")
                    
                    if ohlc and len(ohlc['close']) > 0:
                        close_prices = ohlc['close']
                        high_prices = ohlc['high']
                        low_prices = ohlc['low']
                        current_price = close_prices[-1]
                        
                        ema9 = calculate_ema(close_prices, 9)
                        ema21 = calculate_ema(close_prices, 21)
                        supertrend = calculate_supertrend(high_prices, low_prices, close_prices)
                        macd = calculate_macd(close_prices)
                        
                        if ema9 and ema21 and supertrend and macd is not None:
                            # 🟢 سیگنال خرید (Long)
                            if not state.in_position and ema9 > ema21 and supertrend == 1 and macd > 0:
                                msg = f"🚀 سیگنال خرید فیوچرز (LONG)!\nقیمت: ${current_price:,.0f}"
                                add_log("LONG SIGNAL (Futures): Confirmed")
                                send_bale_notification(msg)
                                state.in_position = True
                                state.entry_price = current_price
                                db.session.commit()
                            
                            # 🔴 سیگنال فروش (Short)
                            elif not state.in_position and ema9 < ema21 and supertrend == -1 and macd < 0:
                                msg = f"🔻 سیگنال فروش فیوچرز (SHORT)!\nقیمت: ${current_price:,.0f}"
                                add_log("SHORT SIGNAL (Futures): Confirmed")
                                send_bale_notification(msg)
                                state.in_position = True
                                state.entry_price = current_price
                                db.session.commit()
                            
                            # بستن پوزیشن
                            elif state.in_position:
                                if (supertrend == -1 and state.entry_price < current_price) or (supertrend == 1 and state.entry_price > current_price):
                                    add_log("CLOSING POSITION: Trend Reversal")
                                    state.in_position = False
                                    state.entry_price = 0.0
                                    db.session.commit()
                            
                            else:
                                add_log(f"Analyzing... Price: ${current_price:,.0f} | EMA9: {ema9:,.0f} | EMA21: {ema21:,.0f}")
                        else:
                            add_log("Not enough data to calculate indicators.")
                    else:
                        add_log("Failed to fetch OHLC data from Nobitex.")
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
    return jsonify({"is_active": state.is_active, "in_position": state.in_position, "entry_price": state.entry_price, "logs": state.logs.split('\n')})

@app.route('/toggle', methods=['POST'])
def toggle_bot():
    state = BotState.query.first()
    state.is_active = not state.is_active
    db.session.commit()
    add_log(f"User turned the bot {'ON' if state.is_active else 'OFF'}.")
    return jsonify({"status": "success", "is_active": state.is_active})

# 🔥 مسیر تحلیل با هوش مصنوعی دوگانه (Gemini + Groq)
@app.route('/analyze', methods=['POST', 'OPTIONS'])
def analyze_chart():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200

    if 'image' not in request.files:
        return jsonify({"error": "عکسی ارسال نشده است."}), 400

    try:
        file = request.files['image']
        img = PIL.Image.open(io.BytesIO(file.read()))
        img.thumbnail((800, 800))
        
        buffered = io.BytesIO()
        img.convert('RGB').save(buffered, format="JPEG")
        base64_image = base64.b64encode(buffered.getvalue()).decode('utf-8')
        
        prompt = """
        شما یک تحلیلگر تکنیکال حرفه‌ای هستید. تصویر چارت را بررسی کنید و تحلیل جامع به فارسی (با خط جدید برای هر بخش) بدهید:
        1. روند کلی
        2. حمایت و مقاومت
        3. اندیکاتورها
        4. الگوها
        5. پیشنهاد معاملاتی (ورود، سود، ضرر)
        """

        gemini_error_msg = "کلید تنظیم نشده"
        groq_error_msg = "کلید تنظیم نشده"

        # 1. تلاش با Gemini
        if GEMINI_API_KEY:
            try:
                response = gemini_client.models.generate_content(model='gemini-3.8-flash', contents=[prompt, img])
                return jsonify({"analysis": response.text})
            except Exception as e:
                gemini_error_msg = str(e)
                print(f"Gemini failed: {gemini_error_msg}")
        
        # 2. تلاش با Groq (مدل 11B Vision)
        if GROQ_API_KEY:
            try:
                response = groq_client.chat.completions.create(
                    model="llama-3.2-11b-vision-preview",
                    messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
                    temperature=0.5, max_tokens=1024
                )
                return jsonify({"analysis": response.choices[0].message.content})
            except Exception as e:
                groq_error_msg = str(e)
                print(f"Groq failed: {groq_error_msg}")

        # اگر هر دو خطا دادند
        return jsonify({"error": f"خطای گوگل: {gemini_error_msg} | خطای Groq: {groq_error_msg}"}), 500
        
    except Exception as e:
        return jsonify({"error": f"خطای سرور: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
