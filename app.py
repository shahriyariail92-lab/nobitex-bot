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

# ================= تنظیمات هوش مصنوعی =================
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

# ================= توابع =================
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

def get_ohlc_data(symbol="BTCUSDT", resolution=60, limit=20):
    end_time = int(time.time())
    start_time = end_time - (limit * resolution * 60)
    url = f"https://apiv2.nobitex.ir/market/udf/history?symbol={symbol}&resolution={resolution}&from={start_time}&to={end_time}"
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
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
    avg_gain = sum(gains[:period])/period
    avg_loss = sum(losses[:period])/period
    if avg_loss == 0: return 100
    return 100 - (100 / (1 + (avg_gain / avg_loss)))

def bot_logic_loop():
    while True:
        with app.app_context():
            state = BotState.query.first()
            if state and state.is_active:
                try:
                    url = "https://apiv2.nobitex.ir/market/stats?srcCurrency=btc&dstCurrency=usdt"
                    res_price = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10).json()
                    current_price = float(res_price['stats']['btc-usdt']['latest'])

                    close_prices = get_ohlc_data()
                    if close_prices:
                        rsi = calculate_rsi(close_prices)
                        if not state.in_position and rsi < 30:
                            msg = f"🚨 سیگنال خرید (BUY)!\nقیمت: ${current_price:,.0f}\nRSI: {rsi:.2f}"
                            add_log(f"BUY SIGNAL! RSI: {rsi:.2f}")
                            send_bale_notification(msg)
                            state.in_position = True
                            state.entry_price = current_price
                            db.session.commit()
                        elif state.in_position and rsi > 70:
                            msg = f"🔻 سیگنال فروش (SELL)!\nقیمت: ${current_price:,.0f}\nRSI: {rsi:.2f}"
                            add_log(f"SELL SIGNAL! RSI: {rsi:.2f}")
                            send_bale_notification(msg)
                            state.in_position = False
                            state.entry_price = 0.0
                            db.session.commit()
                        else:
                            add_log(f"Analyzing... Price: ${current_price:,.0f}, RSI: {rsi:.2f}")
                except Exception as e:
                    add_log(f"Loop Error: {e}")
        time.sleep(15)

# ================= API =================
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
                # تغییر مدل به 1.5-flash که پایدارترین مدل بینایی گوگل است
                response = gemini_client.models.generate_content(model='gemini-1.5-flash', contents=[prompt, img])
                return jsonify({"analysis": response.text})
            except Exception as e:
                gemini_error_msg = str(e)
                print(f"Gemini failed: {gemini_error_msg}")
        
        # 2. تلاش با Groq (پشتیبان)
        if GROQ_API_KEY:
            try:
                # تغییر مدل به 90b-vision که در Groq پشتیبانی می‌شود
                response = groq_client.chat.completions.create(
                    model="llama-3.2-90b-vision-preview",
                    messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
                    temperature=0.5, max_tokens=1024
                )
                return jsonify({"analysis": response.choices[0].message.content})
            except Exception as e:
                groq_error_msg = str(e)
                print(f"Groq failed: {groq_error_msg}")

        # اگر هر دو خطا دادند، متن دقیق خطاها را برمی‌گردانیم
        return jsonify({"error": f"خطای گوگل: {gemini_error_msg} | خطای Groq: {groq_error_msg}"}), 500
        
    except Exception as e:
        return jsonify({"error": f"خطای سرور: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
