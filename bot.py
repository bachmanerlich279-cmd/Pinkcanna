import telebot
from telebot import types
import os
import sqlite3
import re
import requests
from io import BytesIO
from datetime import datetime, timedelta
from openai import OpenAI
import time
import barcode
from barcode.writer import ImageWriter

# --- НАЛАШТУВАННЯ ---
TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID") 
WEB_APP_URL = "https://mamutpetr.github.io/Pinkcanna/"

# --- POSTER API ---
POSTER_TOKEN = os.getenv("POSTER_TOKEN")
POSTER_API_URL = "https://joinposter.com/api"

# ID твого закладу в Poster
SPOT_ID = 1 

if not TOKEN:
    raise Exception("❌ BOT_TOKEN не заданий")
if not POSTER_TOKEN:
    raise Exception("❌ POSTER_TOKEN не заданий")

bot = telebot.TeleBot(TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)

# --- FSM СХОВИЩЕ (КЕШ) ---
user_data_cache = {}

# --- UTILS ---
def normalize_phone(phone):
    clean = re.sub(r'\D', '', phone)
    if clean.startswith("380"):
        return clean
    elif clean.startswith("0"):
        return "380" + clean[1:]
    return clean

# --- POSTER REQUEST ---
def poster_request(endpoint, method="GET", data=None):
    url = f"{POSTER_API_URL}/{endpoint}"
    if not data:
        data = {}

    params = {"token": POSTER_TOKEN}

    try:
        if method == "GET":
            merged_params = {**params, **data}
            res = requests.get(url, params=merged_params, timeout=10)
        else:
            res = requests.post(url, params=params, json=data, timeout=10)

        return res.json()
    except Exception as e:
        print(f"❌ POSTER EXCEPTION [{endpoint}]:", e)
        return None

# --- РОБОТА З POSTER API ---
def get_poster_client(phone_number):
    if not POSTER_TOKEN: return None
    phone = normalize_phone(phone_number)
    res = poster_request("clients.getClients", "GET", {"phone": phone})
    if res and res.get("response"):
        return res["response"][0]
    res_plus = poster_request("clients.getClients", "GET", {"phone": f"+{phone}"})
    if res and res_plus.get("response"):
        return res_plus["response"][0]
    return None

def add_poster_bonus(client_id, amount_uah):
    if not POSTER_TOKEN: return
    payload = {
        "client_id": client_id,
        "count": int(amount_uah * 100)
    }
    res = poster_request("clients.changeClientBonus", "POST", payload)
    return res

def reward_referrer(referrer_id):
    referrer_data = db_manage_user(referrer_id)
    if not referrer_data or not referrer_data[0]:
        return
        
    referrer_phone = referrer_data[0]
    client_poster = get_poster_client(referrer_phone)
    if client_poster:
        add_poster_bonus(client_poster['client_id'], 50)
        try:
            bot.send_message(referrer_id, "🎁 Ваш друг щойно завершив реєстрацію! Вам нараховано **50 бонусів** на рахунок у Poster!", parse_mode="Markdown")
        except: pass

def create_poster_client_full(user_id):
    if not POSTER_TOKEN: return None
    data = user_data_cache.get(user_id, {})
    phone = normalize_phone(data.get('phone', ''))
    
    payload = {
        "client_name": data.get('name', 'Клієнт Telegram'),
        "phone": phone,
        "card_number": phone, 
        "client_sex": data.get('sex', 0),
        "birthday": data.get('birthday', ''),
        "email": data.get('email', ''),
        "client_groups_id_client": 1,
        "bonus": 0
    }

    res = poster_request("clients.createClient", "POST", payload)

    if res and "error" not in res:
        user_db = db_manage_user(user_id)
        if user_db[2]: 
            reward_referrer(user_db[2])
            
        local_discount = user_db[1]
        if local_discount > 0:
            new_client = get_poster_client(phone)
            if new_client:
                add_poster_bonus(new_client['client_id'], local_discount)
                db_manage_user(user_id, discount=0)
                try:
                    bot.send_message(user_id, f"💸 Твої натапані **{local_discount} грн** успішно перенесені на бонусну карту Poster!", parse_mode="Markdown")
                except: pass
            
    return res

# --- БАЗА ДАНИХ ---
def init_db():
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS carts_v2 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_key TEXT, expires_at DATETIME)''')
        c.execute('''CREATE TABLE IF NOT EXISTS users 
                     (user_id INTEGER PRIMARY KEY, phone TEXT, discount INTEGER DEFAULT 0, balance REAL DEFAULT 0, referred_by INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS ai_history 
                     (user_id INTEGER, role TEXT, content TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS inventory 
                     (product_key TEXT PRIMARY KEY, total_qty INTEGER DEFAULT 0)''')
        
        try: c.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        except: pass

        for key in PRODUCTS.keys():
            c.execute("INSERT OR IGNORE INTO inventory (product_key, total_qty) VALUES (?, 20)", (key,))
        conn.commit()

def db_cleanup_expired():
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("DELETE FROM carts_v2 WHERE expires_at < ?", (now_str,))
        conn.commit()

def db_get_stock(product_key):
    db_cleanup_expired()
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("SELECT total_qty FROM inventory WHERE product_key = ?", (product_key,))
        res = c.fetchone()
        total = res[0] if res else 0
        c.execute("SELECT COUNT(*) FROM carts_v2 WHERE product_key = ?", (product_key,))
        reserved = c.fetchone()[0]
        return max(0, total - reserved)

def db_set_stock(product_key, qty):
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("UPDATE inventory SET total_qty = ? WHERE product_key = ?", (qty, product_key))
        conn.commit()

def db_add_to_cart_with_reserve(user_id, product_key):
    if db_get_stock(product_key) > 0:
        with sqlite3.connect("pinkcanna.db") as conn:
            c = conn.cursor()
            expires = datetime.now() + timedelta(minutes=15)
            c.execute("INSERT INTO carts_v2 (user_id, product_key, expires_at) VALUES (?, ?, ?)", 
                      (user_id, product_key, expires.strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
        return True
    return False

def db_get_cart_with_expiry(user_id):
    db_cleanup_expired()
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("SELECT product_key, expires_at FROM carts_v2 WHERE user_id = ?", (user_id,))
        return c.fetchall()

def db_remove_one_from_cart(user_id, product_key):
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("DELETE FROM carts_v2 WHERE id = (SELECT id FROM carts_v2 WHERE user_id = ? AND product_key = ? LIMIT 1)", (user_id, product_key))
        conn.commit()

def db_clear_cart(user_id):
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("DELETE FROM carts_v2 WHERE user_id = ?", (user_id,))
        conn.commit()

def db_confirm_purchase(user_id):
    items = [row[0] for row in db_get_cart_with_expiry(user_id)]
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        for key in items:
            c.execute("UPDATE inventory SET total_qty = total_qty - 1 WHERE product_key = ?", (key,))
        c.execute("DELETE FROM carts_v2 WHERE user_id = ?", (user_id,))
        c.execute("UPDATE users SET discount = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
    return items

def db_manage_user(user_id, discount=None, phone=None):
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        if discount is not None:
            c.execute("UPDATE users SET discount = ? WHERE user_id = ?", (discount, user_id))
        if phone is not None:
            c.execute("UPDATE users SET phone = ? WHERE user_id = ?", (phone, user_id))
        conn.commit()
        c.execute("SELECT phone, discount, referred_by FROM users WHERE user_id = ?", (user_id,))
        return c.fetchone()

def db_manage_history(user_id, role=None, content=None):
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        if role and content:
            c.execute("INSERT INTO ai_history VALUES (?, ?, ?)", (user_id, role, content))
            c.execute("DELETE FROM ai_history WHERE rowid NOT IN (SELECT rowid FROM ai_history WHERE user_id = ? ORDER BY rowid DESC LIMIT 10)", (user_id,))
            conn.commit()
        c.execute("SELECT role, content FROM ai_history WHERE user_id = ? ORDER BY rowid ASC", (user_id,))
        return [{"role": row[0], "content": row[1]} for row in c.fetchall()]

# --- ТОВАРИ ---
CATEGORIES = {
    "kanna": "🌿 Екстракти Канни", 
    "cbd": "💧 Олії та Релакс", 
    "wellness": "🧠 Сон та Енергія", 
    "topical": "🧴 Вейпи та Догляд",
    "coffee": "☕️ Кава",
    "desserts": "🍰 Десерти",
    "cocktails": "🍸 Коктейлі"
}

PRODUCTS = {
    "espresso": {"poster_id": 10, "name": "Еспресо", "price": 65, "image": "espresso.jpg", "category": "coffee", "short": "Класична бадьорість.", "info": "☕️ **Еспресо:** Міцна, насичена кава зі 100% арабіки для ідеального початку дня."},
    "cappuccino": {"poster_id": 9, "name": "Капучино", "price": 85, "image": "cappuccino.jpg", "category": "coffee", "short": "Ніжна молочна пінка.", "info": "☕️ **Капучино:** Ідеальний баланс еспресо та збитого в ніжну пінку молока."},
    "latte": {"poster_id": 8, "name": "Лате", "price": 90, "image": "latte.jpg", "category": "coffee", "short": "Більше молока, м'який смак.", "info": "☕️ **Лате:** Легкий кавовий напій для тих, хто полюбляє м'який молочний смак."},
    "flat_white": {"poster_id": 13, "name": "Флет-вайт", "price": 100, "image": "flatwhite.jpg", "category": "coffee", "short": "Подвійний заряд кави.", "info": "☕️ **Флет-вайт:** Подвійний еспресо з невеликою кількістю ідеально текстурованого молока."},
    "tart_cherry": {"poster_id": 53, "name": "Тарта «Вишня-Ваніль»", "price": 150, "image": "tart.jpg", "category": "desserts", "short": "Хрумке тісто і кислинка.", "info": "🍰 **Тарта «Вишня-Ваніль»:** Ніжний заварний ванільний крем та соковита вишня на пісочній основі."},
    "cake_chocolate": {"poster_id": 55, "name": "Чізкейк «Три шоколади»", "price": 250, "image": "cheesecake.jpg", "category": "desserts", "short": "Шоколадний вибух.", "info": "🍰 **Чізкейк «Три шоколади»:** Преміальний десерт з трьох видів бельгійського шоколаду."},
    "aperol": {"poster_id": 81, "name": "Aperol spritz", "price": 260, "image": "aperol.jpg", "category": "cocktails", "short": "Хіт літнього сезону.", "info": "🍸 **Aperol spritz:** Легкий, ігристий та освіжаючий італійський аперитив."},
    "clover_club": {"poster_id": 7, "name": "Clover Club", "price": 260, "image": "clover.jpg", "category": "cocktails", "short": "Малинова класика.", "info": "🍸 **Clover Club:** Вишуканий коктейль на основі джину з яскравими малиновими нотками."},
    "vape": {"poster_id": 237, "name": "Вейп CBD", "price": 3000, "image": "blackvape.jpg", "category": "topical", "short": "Миттєвий релакс.", "info": "💨 **Vape:** Найшвидша доставка CBD в організм."},
    "kanna10x": {"poster_id": 305, "name": "Канна 10х", "price": 2500, "image": "kanna10x.jpg", "category": "kanna", "short": "Екстракт для настрою.", "info": "🌿 **Канна 10х:** Потужний SRI-ефект для ейфорії та зняття тривоги."},
    "crystal": {"poster_id": 304, "name": "Канна Crystal", "price": 3000, "image": "kannacrystal.jpg", "category": "kanna", "short": "Чистий ізолят.", "info": "💎 **Crystal:** 98% чистих алкалоїдів для ідеального фокусу."},
    "strong": {"poster_id": 304, "name": "Канна Strong", "price": 3000, "image": "kannastrong.jpg", "category": "kanna", "short": "Максимальна сила.", "info": "🔥 **Strong:** Найшвидша дія для досвідчених користувачів."},
    "jelly": {"poster_id": 197, "name": "СБД Желе", "price": 1900, "image": "Cbdgele.jpg", "category": "cbd", "short": "Смачний релакс.", "info": "🍬 **CBD Jelly:** Зручний формат для підтримки спокою протягом дня."},
    "cbd_5_10": {"poster_id": 0, "name": "Олія CBD 5% (10мл)", "price": 800, "image": "cbd_5_10.jpg", "category": "cbd", "short": "35мг в піпетці", "info": "💧 **Олія CBD 5%:** Ідеально для легкого стресу та профілактики."},
    "cbd_10_10": {"poster_id": 0, "name": "Олія CBD 10% (10мл)", "price": 1300, "image": "cbd_10_10.jpg", "category": "cbd", "short": "70мг в піпетці", "info": "💧 **Олія CBD 10%:** Універсальна концентрація для сну та спокою."},
    "cbd_15_10": {"poster_id": 0, "name": "Олія CBD 15% (10мл)", "price": 1800, "image": "cbd_15_10.jpg", "category": "cbd", "short": "105мг в піпетці", "info": "💧 **Олія CBD 15%:** Для хронічного болю та підвищеної тривожності."},
    "cbd_20_10": {"poster_id": 0, "name": "Олія CBD 20% (10мл)", "price": 2100, "image": "cbd_20_10.jpg", "category": "cbd", "short": "140мг в піпетці", "info": "💧 **Олія CBD 20%:** Сильна дія для серйозних симптомів."},
    "cbd_30_10": {"poster_id": 0, "name": "Олія CBD 30% (10мл)", "price": 3400, "image": "cbd_30_10.jpg", "category": "cbd", "short": "210мг в піпетці", "info": "💧 **Олія CBD 30%:** Максимальна концентрація."},
    "cbd_5_30": {"poster_id": 0, "name": "Олія CBD 5% (30мл)", "price": 2000, "image": "cbd_5_30.jpg", "category": "cbd", "short": "Економ формат", "info": "💧 **Олія CBD 5% (30мл):** Вигідний формат."},
    "cbd_10_30": {"poster_id": 0, "name": "Олія CBD 10% (30мл)", "price": 3400, "image": "cbd_10_30.jpg", "category": "cbd", "short": "Економ формат", "info": "💧 **Олія CBD 10% (30мл):** Вигідний формат."},
    "cbd_15_30": {"poster_id": 0, "name": "Олія CBD 15% (30мл)", "price": 4500, "image": "cbd_15_30.jpg", "category": "cbd", "short": "Економ формат", "info": "💧 **Олія CBD 15% (30мл):** Вигідний формат."},
    "cbd_20_30": {"poster_id": 0, "name": "Олія CBD 20% (30мл)", "price": 5200, "image": "cbd_20_30.jpg", "category": "cbd", "short": "Економ формат", "info": "💧 **Олія CBD 20% (30мл):** Вигідний формат."},
    "cbd_30_30": {"poster_id": 0, "name": "Олія CBD 30% (30мл)", "price": 8200, "image": "cbd_30_30.jpg", "category": "cbd", "short": "Економ формат", "info": "💧 **Олія CBD 30% (30мл):** Вигідний формат."},
    "sleep": {"poster_id": 234, "name": "Happy caps sleep", "price": 2000, "image": "sleep.jpg", "category": "wellness", "short": "Для засинання.", "info": "💤 **Sleep:** Глибокий сон та швидке відновлення."},
    "gaba": {"poster_id": 118, "name": "Габа #9", "price": 400, "image": "gaba9.jpg", "category": "wellness", "short": "Спокій мозку.", "info": "🧠 **GABA:** Природне гальмо для зайвих думок та стресу."},
    "energy": {"poster_id": 234, "name": "Happy caps energy", "price": 2000, "image": "energy.jpg", "category": "wellness", "short": "Бадьорість.", "info": "⚡ **Energy:** Енергія без кави та тремору."},
    "cream": {"poster_id": 139, "name": "СБД Крем", "price": 1600, "image": "cream.jpg", "category": "topical", "short": "Для м'язів.", "info": "🧴 **Cream:** Локальне зняття болю та запалень."}
}

DOSAGE_DATA = {
    "ptsd_insomnia": {"name": "ПТСР / Безсоння / Артрит", "doses": {50: 78, 60: 85, 70: 93, 80: 100, 90: 108, 100: 115, 110: 123, 120: 130}},
    "pain": {"name": "Хронічний біль", "doses": {50: 91, 60: 99, 70: 106, 80: 113, 90: 120, 100: 128, 110: 135, 120: 142}},
    "stress": {"name": "Стрес / Фобії", "doses": {50: 64, 60: 68, 70: 73, 80: 77, 90: 82, 100: 87, 110: 91, 120: 95}},
    "depression": {"name": "Депресія", "doses": {50: 76, 60: 88, 70: 99, 80: 111, 90: 122, 100: 133, 110: 145, 120: 156}},
    "migraine": {"name": "Мігрень", "doses": {50: 85, 60: 87, 70: 90, 80: 93, 90: 96, 100: 99, 110: 102, 120: 105}},
    "epilepsy": {"name": "Епілепсія", "doses": {50: 174, 60: 210, 70: 245, 80: 280, 90: 315, 100: 350, 110: 385, 120: 420}}
}
CONC_DATA = {5: {"10ml": 35, "30ml": 50}, 10: {"10ml": 70, "30ml": 100}, 15: {"10ml": 105, "30ml": 150}, 20: {"10ml": 140, "30ml": 200}, 30: {"10ml": 210, "30ml": 300}}

init_db()

# --- UTILS ---
def send_product_card(chat_id, key):
    item = PRODUCTS[key]
    stock = db_get_stock(key)
    markup = types.InlineKeyboardMarkup(row_width=1)
    if stock > 0:
        stock_text = f"🟢 В наявності: {stock} шт"
        markup.add(
            types.InlineKeyboardButton(f"🛒 Додати в кошик ({item['price']} грн)", callback_data=f"buy_{key}"),
            types.InlineKeyboardButton("🔍 Дізнатись більше", callback_data=f"info_{key}")
        )
    else:
        stock_text = "🔴 Немає в наявності"
        markup.add(types.InlineKeyboardButton("🔍 Дізнатись більше", callback_data=f"info_{key}"))

    caption = f"🏷 **{item['name']}**\n\n📝 {item['short']}\n📦 {stock_text}\n💰 **Ціна: {item['price']} грн**"
    try:
        if os.path.exists(item['image']):
            with open(item['image'], 'rb') as photo: bot.send_photo(chat_id, photo, caption=caption, reply_markup=markup, parse_mode="Markdown")
        else: bot.send_message(chat_id, caption, reply_markup=markup, parse_mode="Markdown")
    except: bot.send_message(chat_id, caption, reply_markup=markup, parse_mode="Markdown")

def generate_customer_barcode(phone_number):
    code128 = barcode.get_barcode_class('code128')
    bar = code128(phone_number, writer=ImageWriter())
    bio = BytesIO()
    bar.write(bio, options={"write_text": True, "module_width": 0.3})
    bio.seek(0)
    return bio

# --- МЕНЮ ---
def main_menu():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("📂 Каталог", "🛒 Кошик")
    m.row("🧮 Підбір дози CBD", "👤 Профіль")
    m.row(types.KeyboardButton("🍀 Натапати знижку", web_app=types.WebAppInfo(url=WEB_APP_URL)))
    m.row("📞 Консультант", "📰 Новини")
    return m

def contact_menu():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    m.add(types.KeyboardButton("📱 Надіслати свій номер телефону", request_contact=True))
    m.add("⬅️ Назад до меню")
    return m

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.chat.id
    user_data = db_manage_user(user_id)
    
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id != user_id and user_data[2] is None: 
            with sqlite3.connect("pinkcanna.db") as conn:
                c = conn.cursor()
                c.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, user_id))
                conn.commit()

    bot.send_message(user_id, "🌿 Вітаємо у Pink Canna! Оберіть пункт меню:", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "⬅️ Назад до меню")
def back_to_menu(message):
    if message.chat.id in user_data_cache:
        del user_data_cache[message.chat.id]
    bot.send_message(message.chat.id, "Ви в головному меню:", reply_markup=main_menu())

# --- ПРОФІЛЬ ---
@bot.message_handler(commands=['me', 'profile'])
@bot.message_handler(func=lambda m: m.text == "👤 Профіль")
def profile_cmd(message):
    user_id = message.chat.id
    user_data = db_manage_user(user_id)
    phone = user_data[0] 
    
    if phone:
        display_profile(message, phone, user_data[1]) 
    else:
        user_data_cache[user_id] = {'step': 'register_phone'}
        bot.send_message(user_id, "👤 **Оформлення карти клієнта**\n\nЩоб отримувати кешбек та знижки, поділіться своїм номером телефону.", reply_markup=contact_menu(), parse_mode="Markdown")

@bot.message_handler(content_types=['contact'])
def handle_contact(message):
    user_id = message.chat.id
    phone = message.contact.phone_number
    if not phone.startswith('+'): phone = '+' + phone
    
    if user_id in user_data_cache and user_data_cache[user_id].get('step') == 'register_phone':
        user_data_cache[user_id]['phone'] = phone
        user_data_cache[user_id]['step'] = 'register_name'
        
        client_poster = get_poster_client(phone)
        if client_poster:
            db_manage_user(user_id, phone=phone)
            bot.send_message(user_id, "✅ Ваш профіль знайдено в базі Poster!", reply_markup=main_menu())
            
            user_db = db_manage_user(user_id)
            if user_db[1] > 0:
                add_poster_bonus(client_poster['client_id'], user_db[1])
                db_manage_user(user_id, discount=0)
                bot.send_message(user_id, f"💸 Твої натапані **{user_db[1]} грн** автоматично перенесені на бонусну карту Poster!", parse_mode="Markdown")
            
            display_profile(message, phone, db_manage_user(user_id)[1])
            del user_data_cache[user_id]
        else:
            bot.send_message(user_id, "Введіть ваше ПІБ (Прізвище та Ім'я):", reply_markup=types.ReplyKeyboardRemove())
    else:
        db_manage_user(user_id, phone=phone)
        bot.send_message(user_id, "Номер збережено.", reply_markup=main_menu())

def display_profile(message, phone, game_discount):
    user_id = message.chat.id
    bot_name = bot.get_me().username
    ref_link = f"https://t.me/{bot_name}?start={user_id}"
    
    client_poster = get_poster_client(phone)
    poster_bonus = float(client_poster.get('bonus', 0)) / 100 if client_poster else 0.0
    group_name = client_poster.get('group_name', 'Постійний клієнт') if client_poster else 'Новий клієнт'
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🪪 Моя карта (Штрих-код на касу)", callback_data="show_qr"))

    text = (f"👤 **Твій кабінет Pink Canna**\n\n"
            f"🏷 Статус: *{group_name}*\n"
            f"📱 Телефон: `{phone}`\n\n"
            f"💰 Баланс Poster: **{int(poster_bonus)} грн**\n"
            f"*(всі бонуси за друзів та ігри накопичуються тут)*\n\n"
            f"🔗 **Реферальна програма:**\n"
            f"Запрошуй друзів та отримуй **50 грн** на рахунок у Poster!\n")
    
    if game_discount > 0:
        text += f"\n⚠️ У вас є **{game_discount} грн** знижки, яка чекає на перенесення в Poster."
    
    bot.send_message(user_id, text, reply_markup=markup, parse_mode="Markdown")
    bot.send_message(user_id, f"`{ref_link}`", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "show_qr")
def show_qr_callback(call):
    user_id = call.message.chat.id
    user_data = db_manage_user(user_id)
    phone = user_data[0]
    
    if not phone: return bot.answer_callback_query(call.id, "Спочатку надайте номер телефону!", show_alert=True)
    
    client_poster = get_poster_client(phone)
    poster_bonus = float(client_poster.get('bonus', 0)) / 100 if client_poster else 0.0

    img_barcode = generate_customer_barcode(phone) 
    caption = f"🪪 **Цифрова карта Poster**\n💰 Баланс: **{int(poster_bonus)} грн**\n\nПокажіть цей штрих-код касиру."
    bot.send_photo(user_id, img_barcode, caption=caption, parse_mode="Markdown")

# --- КАЛЬКУЛЯТОР ДОЗИ ---
@bot.message_handler(func=lambda m: m.text == "🧮 Підбір дози CBD")
def calc_start(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, data in DOSAGE_DATA.items(): markup.add(types.InlineKeyboardButton(data["name"], callback_data=f"calc_diag_{key}"))
    bot.send_message(message.chat.id, "🩺 **Крок 1/3:** Оберіть ваш симптом:", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("calc_diag_"))
def calc_weight(call):
    bot.answer_callback_query(call.id)
    diag_key = call.data.replace("calc_diag_", "")
    markup = types.InlineKeyboardMarkup(row_width=4)
    markup.add(*[types.InlineKeyboardButton(f"{w} кг", callback_data=f"calc_weight_{diag_key}_{w}") for w in range(50, 130, 10)])
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="calc_back"))
    bot.edit_message_text("⚖️ **Крок 2/3:** Оберіть вашу вагу тіла:", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("calc_weight_"))
def calc_conc(call):
    bot.answer_callback_query(call.id)
    parts = call.data.split("_")
    diag_key, weight = parts[2], int(parts[3])
    dose = DOSAGE_DATA[diag_key]["doses"][weight]
    markup = types.InlineKeyboardMarkup(row_width=5)
    markup.add(*[types.InlineKeyboardButton(f"{c}%", callback_data=f"calc_res_{diag_key}_{weight}_{c}") for c in [5, 10, 15, 20, 30]])
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"calc_diag_{diag_key}"))
    text = f"🎯 Ваша орієнтовна норма: **{dose} мг** CBD на добу.\n\n🧪 **Крок 3/3:** Оберіть концентрацію олії CBD:"
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("calc_res_"))
def calc_result(call):
    bot.answer_callback_query(call.id)
    parts = call.data.split("_")
    diag_key, weight, conc = parts[2], int(parts[3]), int(parts[4])
    dose = DOSAGE_DATA[diag_key]["doses"][weight]
    text = (f"📊 **Ваш розрахунок:**\n🩺 Симптом: **{DOSAGE_DATA[diag_key]['name']}**\n⚖️ Вага: **{weight} кг**\n🎯 Добова норма: **{dose} мг** CBD\n\n"
            f"💧 **Як приймати ({conc}%):**\n• Флакон 10 мл: `~ {round(dose / CONC_DATA[conc]['10ml'], 1)} піпетки`\n"
            f"• Флакон 30 мл: `~ {round(dose / CONC_DATA[conc]['30ml'], 1)} піпетки`\n\n💡 *Порада: розділіть дозу на ранок та вечір.*")
    markup = types.InlineKeyboardMarkup(row_width=1)
    if db_get_stock(f"cbd_{conc}_10") > 0: markup.add(types.InlineKeyboardButton(f"🛒 Додати {conc}% (10мл)", callback_data=f"buy_cbd_{conc}_10"))
    if db_get_stock(f"cbd_{conc}_30") > 0: markup.add(types.InlineKeyboardButton(f"🛒 Додати {conc}% (30мл)", callback_data=f"buy_cbd_{conc}_30"))
    markup.add(types.InlineKeyboardButton("🔄 Розрахувати заново", callback_data="calc_back"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "calc_back")
def calc_back(call):
    bot.answer_callback_query(call.id); calc_start(call.message)

# --- КАТАЛОГ ---
@bot.message_handler(func=lambda m: m.text == "📂 Каталог")
def show_cats(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for cat_id, cat_name in CATEGORIES.items(): markup.add(types.InlineKeyboardButton(cat_name, callback_data=f"cat_{cat_id}"))
    bot.send_message(message.chat.id, "Оберіть категорію:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_"))
def show_items(call):
    bot.answer_callback_query(call.id)
    cat_id = call.data.split("_")[1]
    for key, item in PRODUCTS.items():
        if item["category"] == cat_id: send_product_card(call.message.chat.id, key)

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_") or call.data.startswith("info_"))
def item_actions(call):
    action, key = call.data.split("_", 1)
    if action == "buy":
        if db_add_to_cart_with_reserve(call.message.chat.id, key):
            bot.answer_callback_query(call.id, f"✅ {PRODUCTS[key]['name']} заброньовано на 15 хв!")
        else:
            bot.answer_callback_query(call.id, "❌ Недостатньо товару!", show_alert=True)
    elif action == "info":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, PRODUCTS[key]['info'], parse_mode="Markdown")
        send_product_card(call.message.chat.id, key)

# --- ТАПАЛКА ---
@bot.message_handler(content_types=['web_app_data'])
def get_discount(message):
    try:
        match = re.search(r'\d+', message.web_app_data.data)
        if match:
            disc = int(match.group())
            user_id = message.chat.id
            user_data = db_manage_user(user_id)
            phone = user_data[0] if user_data else None
            
            if phone:
                client_poster = get_poster_client(phone)
                if client_poster:
                    add_poster_bonus(client_poster['client_id'], disc)
                    db_manage_user(user_id, discount=0)
                    bot.send_message(user_id, f"🍀 Супер! **{disc} грн** успішно зараховано на твій бонусний рахунок у Poster!", parse_mode="Markdown")
                    return
            
            db_manage_user(user_id, discount=disc)
            bot.send_message(user_id, f"🍀 Супер! Знижка **{disc} грн** збережена.\n\n⚠️ Обов'язково зареєструй **👤 Профіль**, щоб ці гроші перейшли в Poster!", parse_mode="Markdown")
    except Exception as e:
        print("Помилка тапалки:", e)

# --- КОШИК ТА ОФОРМЛЕННЯ (САМОВИВІЗ 3 ГОДИНИ) ---
@bot.message_handler(func=lambda m: m.text == "🛒 Кошик")
def cart_cmd(message): render_cart(message.chat.id)

def render_cart(chat_id, message_id=None):
    raw_items = db_get_cart_with_expiry(chat_id)
    if not raw_items:
        text = "🛒 Ваш кошик порожній."
        if message_id: bot.edit_message_text(text, chat_id, message_id)
        else: bot.send_message(chat_id, text)
        return
        
    items = [row[0] for row in raw_items]
    total = sum(PRODUCTS[k]['price'] for k in items)
    
    user_data = db_manage_user(chat_id)
    phone = user_data[0]
    local_discount = user_data[1] 
    
    poster_bonus = 0.0
    if phone:
        client_poster = get_poster_client(phone)
        if client_poster: poster_bonus = float(client_poster.get('bonus', 0)) / 100

    total_benefit = local_discount + poster_bonus
    min_expiry_str = min([row[1] for row in raw_items])
    mins_left = max(1, int((datetime.strptime(min_expiry_str, "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds() / 60))

    markup = types.InlineKeyboardMarkup(row_width=3)
    item_counts = {k: items.count(k) for k in set(items)}
    summary = ""
    for k, count in item_counts.items():
        summary += f"• {PRODUCTS[k]['name']} x{count} = {PRODUCTS[k]['price'] * count} грн\n"
        markup.row(types.InlineKeyboardButton("➖", callback_data=f"crem_{k}"), types.InlineKeyboardButton(f"{count} шт", callback_data="ignore"), types.InlineKeyboardButton("➕", callback_data=f"cadd_{k}"))
        
    markup.row(types.InlineKeyboardButton("✅ Забронювати (Самовивіз)", callback_data="start_checkout"))
    markup.row(types.InlineKeyboardButton("🗑 Очистити кошик", callback_data="clear_cart"))
    
    final_total = total - total_benefit if total_benefit < total else 1
    text = f"**Ваш кошик:**\n\n{summary}\n"
    if total_benefit > 0: text += f"🎁 Можлива знижка (бонуси): -{int(total_benefit)} грн\n"
    text += f"💰 **Сума замовлення: {int(final_total)} грн**\n\n⏳ *Бронь товарів у кошику: {mins_left} хв!*"
    if message_id: bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="Markdown")
    else: bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("cadd_") or call.data.startswith("crem_"))
def mod_cart(call):
    key = call.data.split("_", 1)[1]
    if call.data.startswith("cadd_"):
        if not db_add_to_cart_with_reserve(call.message.chat.id, key):
            bot.answer_callback_query(call.id, "❌ Немає в наявності!", show_alert=True); return
    elif call.data.startswith("crem_"): db_remove_one_from_cart(call.message.chat.id, key)
    bot.answer_callback_query(call.id); render_cart(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "clear_cart")
def clr_cart(call):
    bot.answer_callback_query(call.id); db_clear_cart(call.message.chat.id)
    bot.edit_message_text("🗑 Кошик очищено.", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "start_checkout")
def start_checkout(call):
    user_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    
    items = db_get_cart_with_expiry(user_id)
    if not items:
        bot.send_message(user_id, "Ваш кошик порожній.")
        return

    purchased_items = [row[0] for row in items]
    total_price = sum(PRODUCTS[k]['price'] for k in purchased_items)

    user_data = db_manage_user(user_id)
    phone = user_data[0] if user_data else None
    
    if not phone:
        bot.send_message(user_id, "⚠️ Для бронювання необхідно надати номер телефону в розділі **👤 Профіль**.")
        return

    # Логіка Poster
    products_list = []
    for k in set(purchased_items):
        count = purchased_items.count(k)
        poster_id = PRODUCTS[k].get("poster_id", 0)
        products_list.append({"product_id": poster_id, "count": count})

    order_data_poster = {
        "spot_id": SPOT_ID,
        "phone": phone,
        "products": products_list,
        "comment": "🏃‍♂️ САМОВИВІЗ (Бронь на 3 години через Telegram). Оплата на касі."
    }

    client_poster = get_poster_client(phone)
    if client_poster: order_data_poster["client_id"] = client_poster["client_id"]

    res_order = poster_request("incomingOrders.createIncomingOrder", "POST", order_data_poster)

    if res_order and "error" in res_order:
        bot.send_message(user_id, "⚠️ Помилка Poster API. Зв'яжіться з адміном.")
        return
    
    db_confirm_purchase(user_id)
    
    bot.send_message(user_id, "✅ **Успішно заброньовано на 3 години!**\n\nМи чекаємо на вас. Оплата та списання бонусів — на касі. Просто покажіть касиру ваш штрих-код у профілі.", parse_mode="Markdown")

    if ADMIN_ID:
        try:
            summary = ", ".join([f"{PRODUCTS[k]['name']} (x{purchased_items.count(k)})" for k in set(purchased_items)])
            bot.send_message(ADMIN_ID, f"🔔 **НОВА БРОНЬ (3 год)!**\n👤 Телефон: {phone}\n📦 Товари: {summary}\n💰 Сума: {total_price} грн")
        except: pass

# --- АДМІНКА ---
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if str(message.chat.id) != str(ADMIN_ID): return
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("📦 Склад", callback_data="admin_stock"), types.InlineKeyboardButton("📢 Розсилка", callback_data="admin_broadcast"))
    bot.send_message(message.chat.id, "👨‍💻 **Адмін-панель**", reply_markup=m, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "admin_stock")
def admin_stock_cats(call):
    m = types.InlineKeyboardMarkup(row_width=1)
    for cat_id, cat_name in CATEGORIES.items(): m.add(types.InlineKeyboardButton(cat_name, callback_data=f"astockcat_{cat_id}"))
    bot.edit_message_text("📦 Категорія для складу:", call.message.chat.id, call.message.message_id, reply_markup=m)

@bot.callback_query_handler(func=lambda call: call.data.startswith("astockcat_"))
def admin_stock_items(call):
    cat_id = call.data.split("_")[1]
    m = types.InlineKeyboardMarkup(row_width=1)
    for key, item in PRODUCTS.items():
        if item["category"] == cat_id: m.add(types.InlineKeyboardButton(f"{item['name']} ({db_get_stock(key)} шт)", callback_data=f"astockedit_{key}"))
    m.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_stock"))
    bot.edit_message_text("📦 Зміна кількості:", call.message.chat.id, call.message.message_id, reply_markup=m)

@bot.callback_query_handler(func=lambda call: call.data.startswith("astockedit_"))
def admin_stock_edit(call):
    key = call.data.split("_")[1]
    msg = bot.send_message(call.message.chat.id, f"Введіть кількість для **{PRODUCTS[key]['name']}**:")
    bot.register_next_step_handler(msg, process_stock_update, key)

def process_stock_update(message, key):
    try:
        qty = int(message.text); db_set_stock(key, qty)
        bot.send_message(message.chat.id, f"✅ Оновлено: {qty} шт.")
    except: bot.send_message(message.chat.id, "⚠️ Тільки цифри!")

# --- ОБРОБНИК ТЕКСТУ ТА РЕЄСТРАЦІЯ ---
@bot.message_handler(func=lambda m: True)
def handle_all_text(message):
    user_id = message.chat.id
    text = message.text

    if user_id in user_data_cache and 'step' in user_data_cache[user_id]:
        state = user_data_cache[user_id]
        if state['step'] == 'register_name':
            state['name'] = text
            state['step'] = 'register_sex'
            m = types.ReplyKeyboardMarkup(resize_keyboard=True).add("👨 Чоловіча", "👩 Жіноча")
            bot.send_message(user_id, "Оберіть стать:", reply_markup=m)
            return
        elif state['step'] == 'register_sex':
            state['sex'] = 1 if text == "👨 Чоловіча" else 2 if text == "👩 Жіноча" else 0
            state['step'] = 'register_birthday'
            bot.send_message(user_id, "Дата народження (ДД.ММ.РРРР):", reply_markup=types.ReplyKeyboardRemove())
            return
        elif state['step'] == 'register_birthday':
            if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
                state['birthday'] = text
                state['step'] = 'register_email'
                bot.send_message(user_id, "E-mail (або 'Пропустити'):", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("Пропустити ➡️"))
            return
        elif state['step'] == 'register_email':
            state['email'] = text if text != "Пропустити ➡️" else ""
            db_manage_user(user_id, phone=state['phone'])
            create_poster_client_full(user_id)
            bot.send_message(user_id, "🎉 Карта клієнта створена!", reply_markup=main_menu())
            del user_data_cache[user_id]
            return

    if text in ["📂 Каталог", "🛒 Кошик", "📞 Консультант", "🍀 Натапати знижку", "📰 Новини", "🧮 Підбір дози CBD", "👤 Профіль"]: 
        if text == "📰 Новини": return bot.send_message(user_id, "🌿 СБД легальний згідно з Постановою КМУ №324.")
        if text == "📞 Консультант":
            bot.send_message(user_id, "👨‍💻 Менеджер зв'яжеться з вами.")
            return
        return
    
    # AI Logic
    bot.send_chat_action(user_id, 'typing')
    history = db_manage_history(user_id)
    db_manage_history(user_id, "user", text)
    avail = [f"{k}: {p['name']}" for k, p in PRODUCTS.items() if db_get_stock(k) > 0]
    system_prompt = f"Ти Pink Canna AI. В наявності: {', '.join(avail)}. Використовуй коди [код]."
    
    try:
        response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": text}])
        ai_text = response.choices[0].message.content
        db_manage_history(user_id, "assistant", ai_text)
        keys = re.findall(r'\[([a-zA-Z0-9_]+)\]', ai_text)
        bot.send_message(user_id, re.sub(r'\[[a-zA-Z0-9_]+\]', '', ai_text).strip())
        for k in keys:
            if k in PRODUCTS and db_get_stock(k) > 0: send_product_card(user_id, k)
    except: bot.send_message(user_id, "⚠️ AI тимчасово недоступний.")

if __name__ == "__main__":
    bot.infinity_polling()

