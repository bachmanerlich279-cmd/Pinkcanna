import telebot
from telebot import types
import os
import sqlite3
import re
import math
import struct
import zlib
import requests
from io import BytesIO
from datetime import datetime, timedelta
from openai import OpenAI
import time
import barcode
from barcode.writer import ImageWriter
from urllib.parse import quote

# --- НАЛАШТУВАННЯ ---
TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID") 
WEB_APP_URL = "https://mamutpetr.github.io/Pinkcanna/"
PRODUCT_IMAGE_PLACEHOLDER = "placeholder.png"

# --- POSTER API ---
POSTER_TOKEN = os.getenv("POSTER_TOKEN")
POSTER_API_URL = "https://joinposter.com/api"
SPOT_ID = 1 

if not TOKEN: raise Exception("❌ BOT_TOKEN не заданий")
if not POSTER_TOKEN: raise Exception("❌ POSTER_TOKEN не заданий")

bot = telebot.TeleBot(TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)
user_data_cache = {}

# --- UTILS ---
def ensure_placeholder_image():
    """Create a dependency-free local placeholder when it is not deployed."""
    if os.path.exists(PRODUCT_IMAGE_PLACEHOLDER):
        return

    def png_chunk(chunk_type, data):
        body = chunk_type + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)

    try:
        width, height = 640, 360
        # A neutral Pink Canna-colored card; solid rows compress to a tiny file.
        scanline = b"\x00" + bytes((244, 194, 213)) * width
        raw_pixels = scanline * height
        png = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(raw_pixels, 9))
            + png_chunk(b"IEND", b"")
        )
        with open(PRODUCT_IMAGE_PLACEHOLDER, "wb") as placeholder_file:
            placeholder_file.write(png)
    except OSError as e:
        # Product cards already have a safe text-only fallback if the working
        # directory is read-only.
        print("⚠️ PLACEHOLDER IMAGE:", e)

def normalize_phone(phone):
    clean = re.sub(r'\D', '', phone)
    if clean.startswith("380"): return clean
    elif clean.startswith("0"): return "380" + clean[1:]
    return clean

# --- POSTER REQUEST ---
def poster_request(endpoint, method="GET", data=None):
    url = f"{POSTER_API_URL}/{endpoint}"
    params = {"token": POSTER_TOKEN}
    try:
        if method == "GET":
            merged_params = {**params, **(data or {})}
            res = requests.get(url, params=merged_params, timeout=10)
        else:
            res = requests.post(url, params=params, json=(data or {}), timeout=10)
        return res.json()
    except Exception as e:
        print(f"❌ POSTER EXCEPTION [{endpoint}]:", e)
        return None

# --- РОБОТА З POSTER API ---
def get_poster_client(phone_number):
    if not POSTER_TOKEN: return None
    phone = normalize_phone(phone_number)
    res = poster_request("clients.getClients", "GET", {"phone": phone})
    if res and res.get("response"): return res["response"][0]
    res_plus = poster_request("clients.getClients", "GET", {"phone": f"+{phone}"})
    if res and res_plus.get("response"): return res_plus["response"][0]
    return None

def add_poster_bonus(client_id, amount_uah):
    if not POSTER_TOKEN: return
    # Poster приймає бонуси в копійках. Тому множимо на 100.
    payload = {
        "client_id": client_id,
        "count": int(amount_uah * 100)
    }
    return poster_request("clients.changeClientBonus", "POST", payload)

def reward_referrer_registration(referrer_id):
    referrer_data = db_manage_user(referrer_id)
    if not referrer_data or not referrer_data[0]: return
    
    referrer_phone = referrer_data[0]
    client_poster = get_poster_client(referrer_phone)
    if client_poster:
        add_poster_bonus(client_poster['client_id'], 50)
        try:
            bot.send_message(referrer_id, "🎁 Ваш друг успішно зареєструвався!\nНа ваш рахунок зараховано **50 грн** у Poster.", parse_mode="Markdown")
        except: pass

def process_referral_cashback(user_id, total_price):
    user_data = db_manage_user(user_id)
    if not user_data or not user_data[2]: return # Немає рефовода
    
    referrer_id = user_data[2]
    cashback_uah = total_price * 0.01 # 1% від покупки
    
    if cashback_uah >= 0.01:
        referrer_data = db_manage_user(referrer_id)
        if referrer_data and referrer_data[0]:
            client_poster = get_poster_client(referrer_data[0])
            if client_poster:
                add_poster_bonus(client_poster['client_id'], cashback_uah)
                try:
                    bot.send_message(referrer_id, f"🎁 Ваш друг щойно зробив замовлення!\nВам нараховано кешбек **{cashback_uah:.2f} грн** (1%) у Poster.", parse_mode="Markdown")
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
        if user_db[2]: # Якщо прийшов по рефці
            reward_referrer_registration(user_db[2])
            
        local_discount = user_db[1]
        if local_discount >= 0.01:
            new_client = get_poster_client(phone)
            if new_client:
                transferable_uah = round(local_discount, 2)
                add_poster_bonus(new_client['client_id'], transferable_uah)
                remainder = local_discount - transferable_uah
                db_manage_user(user_id, discount=remainder)
                try:
                    bot.send_message(user_id, f"✅ Ваша знижка **{transferable_uah} грн** успішно перенесена на бонусну карту Poster!", parse_mode="Markdown")
                except: pass
    return res

# --- БАЗА ДАНИХ ---
def init_db():
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS carts_v2 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_key TEXT, expires_at DATETIME)''')
        c.execute('''CREATE TABLE IF NOT EXISTS users 
                     (user_id INTEGER PRIMARY KEY, phone TEXT, discount REAL DEFAULT 0, balance REAL DEFAULT 0, referred_by INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS ai_history 
                     (user_id INTEGER, role TEXT, content TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS inventory 
                     (product_key TEXT PRIMARY KEY, total_qty INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS orders 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, items TEXT, total REAL, poster_order_id INTEGER, status TEXT DEFAULT 'active', product_keys TEXT, created_at DATETIME)''')
        c.execute('''CREATE TABLE IF NOT EXISTS categories (
                     cat_id TEXT PRIMARY KEY,
                     name TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS products (
                     product_key TEXT PRIMARY KEY,
                     poster_id INTEGER DEFAULT 0,
                     name TEXT NOT NULL,
                     price REAL NOT NULL,
                     image TEXT,
                     category TEXT,
                     short TEXT,
                     info TEXT,
                     FOREIGN KEY(category) REFERENCES categories(cat_id))''')
        
        try: c.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        except: pass
        
        try: c.execute("ALTER TABLE orders ADD COLUMN poster_order_id INTEGER")
        except: pass
        try: c.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'active'")
        except: pass
        try: c.execute("ALTER TABLE orders ADD COLUMN product_keys TEXT")
        except: pass

        # One-time migration from the catalog that used to live only in Python.
        # Existing database rows always win, so upgrades never overwrite admin data.
        c.execute("SELECT COUNT(*) FROM categories")
        if c.fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO categories (cat_id, name) VALUES (?, ?)",
                DEFAULT_CATEGORIES.items()
            )

        c.execute("SELECT COUNT(*) FROM products")
        if c.fetchone()[0] == 0:
            # Ensure all seed categories exist even if a partially initialized
            # database already had one or more custom categories.
            c.executemany(
                "INSERT OR IGNORE INTO categories (cat_id, name) VALUES (?, ?)",
                DEFAULT_CATEGORIES.items()
            )
            c.executemany(
                '''INSERT INTO products
                   (product_key, poster_id, name, price, image, category, short, info)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                [
                    (
                        key,
                        item.get("poster_id", 0),
                        item["name"],
                        item["price"],
                        item.get("image"),
                        item.get("category"),
                        item.get("short", ""),
                        item.get("info", "")
                    )
                    for key, item in DEFAULT_PRODUCTS.items()
                ]
            )

        c.execute("SELECT product_key FROM products")
        for (key,) in c.fetchall():
            c.execute(
                "INSERT OR IGNORE INTO inventory (product_key, total_qty) VALUES (?, 20)",
                (key,)
            )
        conn.commit()

def load_products_from_db():
    """Reload the database catalog into the legacy in-memory dictionaries."""
    global CATEGORIES, PRODUCTS
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("SELECT cat_id, name FROM categories ORDER BY rowid")
        categories = dict(c.fetchall())
        c.execute('''SELECT product_key, poster_id, name, price, image,
                            category, short, info
                     FROM products ORDER BY rowid''')
        products = {
            row[0]: {
                "poster_id": row[1] or 0,
                "name": row[2],
                "price": row[3],
                "image": row[4] or "",
                "category": row[5],
                "short": row[6] or "",
                "info": row[7] or ""
            }
            for row in c.fetchall()
        }

    # Replace both objects together only after the reads succeed. Handlers never
    # see a half-loaded catalog, even if SQLite raises an exception.
    CATEGORIES = categories
    PRODUCTS = products

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
        c.execute(
            '''INSERT INTO inventory (product_key, total_qty) VALUES (?, ?)
               ON CONFLICT(product_key) DO UPDATE SET total_qty = excluded.total_qty''',
            (product_key, qty)
        )
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

def db_confirm_purchase(user_id, summary_text, total_price, poster_order_id=None):
    items = [row[0] for row in db_get_cart_with_expiry(user_id)]
    keys_str = ",".join(items)
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        for key in items:
            c.execute("UPDATE inventory SET total_qty = total_qty - 1 WHERE product_key = ?", (key,))
        c.execute("DELETE FROM carts_v2 WHERE user_id = ?", (user_id,))
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        c.execute("INSERT INTO orders (user_id, items, total, poster_order_id, status, product_keys, created_at) VALUES (?, ?, ?, ?, 'active', ?, ?)", 
                  (user_id, summary_text, total_price, poster_order_id, keys_str, now_str))
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
DEFAULT_CATEGORIES = {
    "kanna": "🌿 Екстракти Канни", 
    "cbd": "💧 Олії та Релакс", 
    "wellness": "🧠 Сон та Енергія", 
    "topical": "🧴 Вейпи та Догляд",
    "coffee": "☕️ Кава",
    "desserts": "🍰 Десерти",
    "cocktails": "🍸 Коктейлі"
}

DEFAULT_PRODUCTS = {
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

# Runtime catalog. init_db() seeds/migrates the defaults above and then these
# dictionaries are populated exclusively from SQLite.
CATEGORIES = {}
PRODUCTS = {}

DOSAGE_DATA = {
    "ptsd_insomnia": {"name": "ПТСР / Безсоння / Артрит", "doses": {50: 78, 60: 85, 70: 93, 80: 100, 90: 108, 100: 115, 110: 123, 120: 130}},
    "pain": {"name": "Хронічний біль", "doses": {50: 91, 60: 99, 70: 106, 80: 113, 90: 120, 100: 128, 110: 135, 120: 142}},
    "stress": {"name": "Стрес / Фобії", "doses": {50: 64, 60: 68, 70: 73, 80: 77, 90: 82, 100: 87, 110: 91, 120: 95}},
    "depression": {"name": "Депресія", "doses": {50: 76, 60: 88, 70: 99, 80: 111, 90: 122, 100: 133, 110: 145, 120: 156}},
    "migraine": {"name": "Мігрень", "doses": {50: 85, 60: 87, 70: 90, 80: 93, 90: 96, 100: 99, 110: 102, 120: 105}},
    "epilepsy": {"name": "Епілепсія", "doses": {50: 174, 60: 210, 70: 245, 80: 280, 90: 315, 100: 350, 110: 385, 120: 420}}
}
CONC_DATA = {5: {"10ml": 35, "30ml": 50}, 10: {"10ml": 70, "30ml": 100}, 15: {"10ml": 105, "30ml": 150}, 20: {"10ml": 140, "30ml": 200}, 30: {"10ml": 210, "30ml": 300}}

ensure_placeholder_image()
init_db()
load_products_from_db()

# --- ВІДПРАВКА КАРТКИ ТОВАРУ ---
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
    m.row(types.KeyboardButton("🎁 Отримати знижку (Гра)", web_app=types.WebAppInfo(url=WEB_APP_URL)))
    m.row("📞 Консультант")
    return m

def contact_menu():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    m.add(types.KeyboardButton("📱 Надіслати номер телефону", request_contact=True))
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

    bot.send_message(user_id, "Вітаємо у Pink Canna! 🌿\nБудь ласка, оберіть пункт меню нижче:", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "⬅️ Назад до меню")
def back_to_menu(message):
    if message.chat.id in user_data_cache:
        del user_data_cache[message.chat.id]
    bot.send_message(message.chat.id, "Ви повернулися до головного меню:", reply_markup=main_menu())

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
        bot.send_message(user_id, "👤 **Оформлення карти клієнта**\n\nДля нарахування кешбеку та знижок, будь ласка, поділіться своїм номером телефону.", reply_markup=contact_menu(), parse_mode="Markdown")

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
            bot.send_message(user_id, "✅ Ваш профіль успішно знайдено в базі!", reply_markup=main_menu())
            
            user_db = db_manage_user(user_id)
            if user_db[1] >= 0.01:
                transferable_uah = round(user_db[1], 2)
                add_poster_bonus(client_poster['client_id'], transferable_uah)
                db_manage_user(user_id, discount=(user_db[1] - transferable_uah))
                bot.send_message(user_id, f"💸 Ваша накопичена знижка **{transferable_uah} грн** автоматично перенесена на карту Poster!", parse_mode="Markdown")
            
            display_profile(message, phone, db_manage_user(user_id)[1])
            del user_data_cache[user_id]
        else:
            bot.send_message(user_id, "Будь ласка, введіть Ваше ПІБ (Прізвище та Ім'я):", reply_markup=types.ReplyKeyboardRemove())
    else:
        db_manage_user(user_id, phone=phone)
        bot.send_message(user_id, "Номер успішно збережено.", reply_markup=main_menu())

def display_profile(message, phone, game_discount):
    user_id = message.chat.id
    bot_name = bot.get_me().username
    ref_link = f"https://t.me/{bot_name}?start={user_id}"
    
    client_poster = get_poster_client(phone)
    poster_bonus = float(client_poster.get('bonus', 0)) / 100 if client_poster else 0.0
    group_name = client_poster.get('group_name', 'Постійний клієнт') if client_poster else 'Новий клієнт'
    
    # Кнопка Share
    share_text = quote("Приєднуйся до Pink Canna, грай та отримуй бонуси на покупки! 🌿")
    share_url = f"https://t.me/share/url?url={ref_link}&text={share_text}"
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🪪 Моя карта (Штрих-код)", callback_data="show_qr"),
        types.InlineKeyboardButton("📜 Історія замовлень", callback_data="order_history"),
        types.InlineKeyboardButton("📤 Надіслати другу", url=share_url)
    )

    text = (f"👤 **Ваш кабінет Pink Canna**\n\n"
            f"🏷 Статус: *{group_name}*\n"
            f"📱 Телефон: `{phone}`\n\n"
            f"💰 Бонуси: **{int(poster_bonus)} грн**\n"
            f"🔗 **Реферальна програма:** Запрошуйте друзів! Отримуйте **50 грн** за їх реєстрацію та **1%** від суми всіх їхніх покупок.\n")
    
    if game_discount > 0:
        text += f"\n⚠️ У вас накопичено: **{game_discount:.16f} грн** (чекають перенесення в Poster при досягненні 0.01 грн)."
    
    bot.send_message(user_id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "order_history")
def show_order_history(call):
    bot.answer_callback_query(call.id)
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("SELECT id, items, total, created_at, status FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 5", (call.message.chat.id,))
        orders = c.fetchall()
        
    if not orders:
        bot.send_message(call.message.chat.id, "🛒 У вас ще немає замовлень.")
        return
        
    bot.send_message(call.message.chat.id, "📜 **Ваші останні замовлення:**", parse_mode="Markdown")
    
    for o in orders:
        order_id, items, total, created_at, status = o
        text = f"📅 **{created_at}**\n📦 {items}\n💰 Сума: **{total} грн**\n"
        
        m = types.InlineKeyboardMarkup(row_width=1)
        if status == 'active':
            text += "🟢 Статус: **Активне** (Бронь)"
            m.add(types.InlineKeyboardButton("❌ Скасувати замовлення", callback_data=f"cancel_order_{order_id}"))
        else:
            text += "🔴 Статус: **Скасоване**"
            
        bot.send_message(call.message.chat.id, text, reply_markup=m if status == 'active' else None, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_order_"))
def cancel_order_handler(call):
    order_id = call.data.split("_")[2]
    user_id = call.message.chat.id
    
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("SELECT poster_order_id, status, product_keys FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id))
        order = c.fetchone()
        
    if not order:
        return bot.answer_callback_query(call.id, "❌ Замовлення не знайдено!", show_alert=True)
        
    poster_order_id, status, product_keys = order
    
    if status == 'cancelled':
        return bot.answer_callback_query(call.id, "ℹ️ Це замовлення вже скасовано.", show_alert=True)
        
    # СКАСУВАННЯ ЛОКАЛЬНО БЕЗ POSTER API
    with sqlite3.connect("pinkcanna.db") as conn:
        c = conn.cursor()
        c.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
        if product_keys:
            for key in product_keys.split(","):
                c.execute("UPDATE inventory SET total_qty = total_qty + 1 WHERE product_key = ?", (key,))
        conn.commit()
        
    bot.answer_callback_query(call.id, "✅ Замовлення успішно скасоване!")
    
    new_text = call.message.text.replace("🟢 Статус: **Активне** (Бронь)", "🔴 Статус: **Скасоване**").replace("🟢 Статус: Активне", "🔴 Статус: Скасоване")
    try: bot.edit_message_text(new_text, chat_id=user_id, message_id=call.message.message_id, parse_mode="Markdown")
    except: pass

    if ADMIN_ID:
        user_data = db_manage_user(user_id)
        phone = user_data[0] if user_data and user_data[0] else "Не вказано"
        try: 
            bot.send_message(
                ADMIN_ID, 
                f"⚠️ **СКАСУВАННЯ ЗАМОВЛЕННЯ!**\n\n"
                f"Клієнт ({phone}) самостійно скасував замовлення #{order_id} у боті.\n"
                f"❗️ **Poster ID:** `{poster_order_id}`\n\n"
                f"Товари повернуто на віртуальний склад. Скасуйте замовлення на касі Poster вручну.", 
                parse_mode="Markdown"
            )
        except: pass

@bot.callback_query_handler(func=lambda call: call.data == "show_qr")
def show_qr_callback(call):
    user_id = call.message.chat.id
    user_data = db_manage_user(user_id)
    phone = user_data[0]
    
    if not phone: return bot.answer_callback_query(call.id, "Будь ласка, спочатку надайте номер телефону в профілі!", show_alert=True)
    
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
    bot.send_message(message.chat.id, "Будь ласка, оберіть категорію:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_"))
def show_items(call):
    bot.answer_callback_query(call.id)
    cat_id = call.data[len("cat_"):]
    for key, item in PRODUCTS.items():
        if item["category"] == cat_id: send_product_card(call.message.chat.id, key)

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_") or call.data.startswith("info_"))
def item_actions(call):
    action, key = call.data.split("_", 1)
    if action == "buy":
        if db_add_to_cart_with_reserve(call.message.chat.id, key):
            bot.answer_callback_query(call.id, f"✅ {PRODUCTS[key]['name']} успішно додано в кошик!")
        else:
            bot.answer_callback_query(call.id, "❌ На жаль, товару немає в наявності!", show_alert=True)
    elif action == "info":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, PRODUCTS[key]['info'], parse_mode="Markdown")
        send_product_card(call.message.chat.id, key)

# --- ТАПАЛКА (ГРА) ---
@bot.message_handler(content_types=['web_app_data'])
def get_discount(message):
    try:
        match = re.search(r'\d+', message.web_app_data.data)
        if match:
            taps = int(match.group())
            disc = taps * 0.0000000000000001
            
            user_id = message.chat.id
            user_data = db_manage_user(user_id)
            current_discount = user_data[1] if user_data else 0.0
            new_total = current_discount + disc
            
            phone = user_data[0] if user_data else None
            
            # Якщо накопичилась мінімум 1 копійка і є телефон, відправляємо цілі копійки в Poster
            if phone and new_total >= 0.01:
                client_poster = get_poster_client(phone)
                if client_poster:
                    transferable_uah = round(new_total, 2)
                    add_poster_bonus(client_poster['client_id'], transferable_uah)
                    remainder = new_total - transferable_uah
                    db_manage_user(user_id, discount=remainder)
                    bot.send_message(user_id, f"✅ Відмінно! Ваші накопичені **{transferable_uah} грн** переведено на карту Poster.", parse_mode="Markdown")
                    return
            
            db_manage_user(user_id, discount=new_total)
            bot.send_message(user_id, f"✅ На ваш внутрішній баланс додано **{disc:.16f} грн**.\nЗагальна сума: **{new_total:.16f} грн**.\n\n⚠️ Знижка перейде в Poster, щойно досягне 0.01 грн, якщо заповнено **👤 Профіль**.", parse_mode="Markdown")
    except Exception as e:
        print("Помилка обробки результатів гри:", e)

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
        
    markup.add(types.InlineKeyboardButton("✅ Забронювати (Самовивіз)", callback_data="start_checkout"))
    markup.add(types.InlineKeyboardButton("🗑 Очистити кошик", callback_data="clear_cart"))
    
    final_total = total - total_benefit if total_benefit < total else 1
    text = f"**Ваш кошик:**\n\n{summary}\n"
    if total_benefit >= 0.01: text += f"🎁 Можлива знижка (бонуси): -{int(total_benefit)} грн\n"
    text += f"💰 **Сума замовлення: {int(final_total)} грн**\n\n⏳ *Бронь товарів у кошику: {mins_left} хв!*"
    if message_id: bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="Markdown")
    else: bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("cadd_") or call.data.startswith("crem_"))
def mod_cart(call):
    key = call.data.split("_", 1)[1]
    if call.data.startswith("cadd_"):
        if not db_add_to_cart_with_reserve(call.message.chat.id, key):
            bot.answer_callback_query(call.id, "❌ На жаль, товару немає в наявності!", show_alert=True); return
    elif call.data.startswith("crem_"): db_remove_one_from_cart(call.message.chat.id, key)
    bot.answer_callback_query(call.id); render_cart(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "clear_cart")
def clr_cart(call):
    bot.answer_callback_query(call.id); db_clear_cart(call.message.chat.id)
    bot.edit_message_text("🗑 Кошик успішно очищено.", call.message.chat.id, call.message.message_id)

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
        bot.send_message(user_id, "⚠️ Для бронювання необхідно надати номер телефону. Будь ласка, перейдіть до розділу **👤 Профіль**.", parse_mode="Markdown")
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
        bot.send_message(user_id, "⚠️ Виникла помилка під час обробки замовлення. Будь ласка, зверніться до підтримки.")
        return
        
    poster_order_id = None
    if res_order and "response" in res_order:
        resp = res_order["response"]
        if isinstance(resp, dict): poster_order_id = resp.get("incoming_order_id")
        elif isinstance(resp, int): poster_order_id = resp
    
    summary = ", ".join([f"{PRODUCTS[k]['name']} (x{purchased_items.count(k)})" for k in set(purchased_items)])
    
    # Підтвердження покупки та виплата 1% кешбеку рефоводу
    db_confirm_purchase(user_id, summary, total_price, poster_order_id)
    process_referral_cashback(user_id, total_price)
    
    bot.send_message(user_id, "✅ **Замовлення успішно заброньовано на 3 години!**\n\nМи з нетерпінням чекаємо на Вас у закладі. Оплата та списання бонусів відбудуться на касі. Для цього просто покажіть касиру Ваш штрих-код у профілі.\n\nДякуємо, що обрали Pink Canna! 🌿", parse_mode="Markdown")

    if ADMIN_ID:
        try: bot.send_message(ADMIN_ID, f"🔔 **НОВЕ ЗАМОВЛЕННЯ (Самовивіз)!**\n👤 Телефон: `{phone}`\n📦 Товари: {summary}\n💰 Сума: {total_price} грн", parse_mode="Markdown")
        except: pass

# --- АДМІНКА ---
def is_admin(user_id):
    return ADMIN_ID is not None and str(user_id) == str(ADMIN_ID)

def admin_menu_markup():
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(
        types.InlineKeyboardButton("📦 Склад", callback_data="admin_stock"),
        types.InlineKeyboardButton("➕ Додати товар", callback_data="admin_add_product"),
        types.InlineKeyboardButton("📢 Розсилка", callback_data="admin_broadcast")
    )
    return m

def reject_non_admin_callback(call):
    if is_admin(call.from_user.id):
        return False
    try:
        bot.answer_callback_query(call.id, "⛔ Немає доступу", show_alert=True)
    except Exception:
        pass
    return True

def normalize_product_key(value):
    value = re.sub(r"\s+", "_", (value or "").strip().lower())
    value = re.sub(r"_+", "_", value).strip("_")
    if not value or len(value) > 40 or not re.fullmatch(r"[a-z0-9_]+", value):
        return None
    return value

def generate_category_id(name):
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    if not base:
        base = f"category_{int(time.time())}"
    base = base[:32].rstrip("_") or "category"
    candidate = base
    suffix = 2
    while candidate in CATEGORIES:
        tail = f"_{suffix}"
        candidate = f"{base[:32 - len(tail)]}{tail}"
        suffix += 1
    return candidate

def category_picker_markup():
    m = types.InlineKeyboardMarkup(row_width=1)
    for cat_id, cat_name in CATEGORIES.items():
        m.add(types.InlineKeyboardButton(cat_name, callback_data=f"admin_add_cat:{cat_id}"))
    m.add(types.InlineKeyboardButton("🆕 Створити нову категорію", callback_data="admin_add_category_new"))
    return m

def image_skip_markup():
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(types.InlineKeyboardButton("⏭ Використати placeholder", callback_data="admin_add_image_skip"))
    return m

def broadcast_preview_markup():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("✅ Надіслати", callback_data="admin_broadcast_send"),
        types.InlineKeyboardButton("❌ Скасувати", callback_data="admin_broadcast_cancel")
    )
    return m

def prompt_admin_short(chat_id):
    bot.send_message(chat_id, "Введіть короткий опис товару:")

def prompt_admin_image(chat_id):
    bot.send_message(
        chat_id,
        "Надішліть фото товару. Буде збережено фото найвищої доступної якості.",
        reply_markup=image_skip_markup()
    )

def finish_admin_product(user_id, image_path=PRODUCT_IMAGE_PLACEHOLDER):
    state = user_data_cache.get(user_id, {})
    required = ("product_key", "name", "price", "category", "short", "info")
    if state.get("step") != "admin_add_image" or any(field not in state for field in required):
        bot.send_message(user_id, "⚠️ Дані товару неповні. Запустіть додавання ще раз через /admin.")
        user_data_cache.pop(user_id, None)
        return

    try:
        with sqlite3.connect("pinkcanna.db") as conn:
            c = conn.cursor()
            c.execute(
                '''INSERT INTO products
                   (product_key, poster_id, name, price, image, category, short, info)
                   VALUES (?, 0, ?, ?, ?, ?, ?, ?)''',
                (
                    state["product_key"], state["name"], state["price"], image_path,
                    state["category"], state["short"], state["info"]
                )
            )
            c.execute(
                '''INSERT INTO inventory (product_key, total_qty) VALUES (?, 20)
                   ON CONFLICT(product_key) DO UPDATE SET total_qty = 20''',
                (state["product_key"],)
            )
            conn.commit()
        product_name = state["name"]
        product_key = state["product_key"]
        user_data_cache.pop(user_id, None)
        load_products_from_db()
        bot.send_message(
            user_id,
            f"✅ Товар «{product_name}» додано.\nКлюч: `{product_key}`\nПочатковий залишок: 20 шт.",
            reply_markup=admin_menu_markup(),
            parse_mode="Markdown"
        )
    except sqlite3.IntegrityError:
        user_data_cache.pop(user_id, None)
        bot.send_message(user_id, "⚠️ Товар із таким ключем уже існує.", reply_markup=admin_menu_markup())
    except Exception as e:
        print("❌ ADMIN ADD PRODUCT:", e)
        bot.send_message(user_id, "⚠️ Не вдалося зберегти товар. Спробуйте ще раз або перевірте журнал помилок.")

def show_broadcast_preview(user_id, draft):
    state = user_data_cache.setdefault(user_id, {})
    state["step"] = "admin_broadcast_preview"
    state["broadcast"] = draft
    bot.send_message(user_id, "👀 Попередній перегляд розсилки:")
    if draft["type"] == "photo":
        bot.send_photo(
            user_id,
            draft["file_id"],
            caption=draft.get("caption"),
            reply_markup=broadcast_preview_markup()
        )
    else:
        bot.send_message(user_id, draft["text"], reply_markup=broadcast_preview_markup())

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if not is_admin(message.chat.id):
        return
    user_data_cache.pop(message.chat.id, None)
    bot.send_message(
        message.chat.id,
        "👨‍💻 **Адміністративна панель**",
        reply_markup=admin_menu_markup(),
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "admin_stock")
def admin_stock_cats(call):
    if reject_non_admin_callback(call):
        return
    user_data_cache.pop(call.from_user.id, None)
    load_products_from_db()
    m = types.InlineKeyboardMarkup(row_width=1)
    for cat_id, cat_name in CATEGORIES.items():
        m.add(types.InlineKeyboardButton(cat_name, callback_data=f"astockcat_{cat_id}"))
    m.add(types.InlineKeyboardButton("⬅️ Адмін-панель", callback_data="admin_home"))
    bot.answer_callback_query(call.id)
    bot.edit_message_text("📦 Категорія для складу:", call.message.chat.id, call.message.message_id, reply_markup=m)

@bot.callback_query_handler(func=lambda call: call.data.startswith("astockcat_"))
def admin_stock_items(call):
    if reject_non_admin_callback(call):
        return
    cat_id = call.data[len("astockcat_"):]
    m = types.InlineKeyboardMarkup(row_width=1)
    for key, item in PRODUCTS.items():
        if item["category"] == cat_id:
            m.add(types.InlineKeyboardButton(
                f"{item['name']} ({db_get_stock(key)} шт)",
                callback_data=f"astockedit_{key}"
            ))
    m.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_stock"))
    bot.answer_callback_query(call.id)
    bot.edit_message_text("📦 Зміна кількості:", call.message.chat.id, call.message.message_id, reply_markup=m)

@bot.callback_query_handler(func=lambda call: call.data.startswith("astockedit_"))
def admin_stock_edit(call):
    if reject_non_admin_callback(call):
        return
    key = call.data[len("astockedit_"):]
    if key not in PRODUCTS:
        bot.answer_callback_query(call.id, "Товар не знайдено", show_alert=True)
        return
    user_data_cache[call.from_user.id] = {"step": "admin_stock_qty", "product_key": key}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"Введіть кількість для **{PRODUCTS[key]['name']}**:", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "admin_home")
def admin_home(call):
    if reject_non_admin_callback(call):
        return
    user_data_cache.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        "👨‍💻 **Адміністративна панель**",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=admin_menu_markup(),
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_product")
def admin_add_product(call):
    if reject_non_admin_callback(call):
        return
    load_products_from_db()
    user_data_cache[call.from_user.id] = {"step": "admin_add_key"}
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "Введіть унікальний англійський ключ товару (наприклад, `matcha_latte`).\n"
        "Пробіли автоматично стануть символами підкреслення:",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_add_cat:"))
def admin_add_category_selected(call):
    if reject_non_admin_callback(call):
        return
    state = user_data_cache.get(call.from_user.id, {})
    if state.get("step") != "admin_add_category":
        bot.answer_callback_query(call.id, "Сесія додавання застаріла", show_alert=True)
        return
    cat_id = call.data.split(":", 1)[1]
    if cat_id not in CATEGORIES:
        bot.answer_callback_query(call.id, "Категорію не знайдено", show_alert=True)
        return
    state["category"] = cat_id
    state["step"] = "admin_add_short"
    bot.answer_callback_query(call.id)
    prompt_admin_short(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_category_new")
def admin_add_category_new(call):
    if reject_non_admin_callback(call):
        return
    state = user_data_cache.get(call.from_user.id, {})
    if state.get("step") != "admin_add_category":
        bot.answer_callback_query(call.id, "Сесія додавання застаріла", show_alert=True)
        return
    state["step"] = "admin_add_category_name"
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Введіть публічну назву нової категорії:")

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_image_skip")
def admin_add_image_skip(call):
    if reject_non_admin_callback(call):
        return
    if user_data_cache.get(call.from_user.id, {}).get("step") != "admin_add_image":
        bot.answer_callback_query(call.id, "Сесія додавання застаріла", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    finish_admin_product(call.from_user.id, PRODUCT_IMAGE_PLACEHOLDER)

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def admin_broadcast_start(call):
    if reject_non_admin_callback(call):
        return
    user_data_cache[call.from_user.id] = {"step": "admin_broadcast_content"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📢 Надішліть текст або фото з підписом для розсилки:")

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_cancel")
def admin_broadcast_cancel(call):
    if reject_non_admin_callback(call):
        return
    user_data_cache.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id, "Розсилку скасовано")
    bot.send_message(call.message.chat.id, "❌ Розсилку скасовано.", reply_markup=admin_menu_markup())

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_send")
def admin_broadcast_send(call):
    if reject_non_admin_callback(call):
        return
    state = user_data_cache.get(call.from_user.id, {})
    draft = state.get("broadcast")
    if state.get("step") != "admin_broadcast_preview" or not draft:
        bot.answer_callback_query(call.id, "Чернетку не знайдено", show_alert=True)
        return

    # Prevent a double tap from launching the same broadcast twice.
    state["step"] = "admin_broadcast_sending"
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "⏳ Розсилку розпочато…")
    try:
        with sqlite3.connect("pinkcanna.db") as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM users")
            user_ids = [row[0] for row in c.fetchall()]
    except sqlite3.Error as e:
        print("❌ BROADCAST DATABASE:", e)
        user_data_cache.pop(call.from_user.id, None)
        bot.send_message(call.message.chat.id, "⚠️ Не вдалося отримати список користувачів.", reply_markup=admin_menu_markup())
        return

    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            if draft["type"] == "photo":
                bot.send_photo(user_id, draft["file_id"], caption=draft.get("caption"))
            else:
                bot.send_message(user_id, draft["text"])
            sent += 1
        except Exception as e:
            failed += 1
            print(f"⚠️ BROADCAST FAILED [{user_id}]:", e)
        finally:
            time.sleep(0.05)

    user_data_cache.pop(call.from_user.id, None)
    bot.send_message(
        call.message.chat.id,
        f"📊 Розсилку завершено.\nSuccessfully sent: {sent}. Blocked/Failed: {failed}.",
        reply_markup=admin_menu_markup()
    )

@bot.message_handler(
    content_types=['photo'],
    func=lambda message: is_admin(message.chat.id) and
    user_data_cache.get(message.chat.id, {}).get("step") in
    ("admin_add_image", "admin_broadcast_content")
)
def admin_photo_input(message):
    state = user_data_cache.get(message.chat.id, {})
    if state.get("step") == "admin_broadcast_content":
        show_broadcast_preview(message.chat.id, {
            "type": "photo",
            "file_id": message.photo[-1].file_id,
            "caption": message.caption or ""
        })
        return

    key = state.get("product_key")
    if not key:
        user_data_cache.pop(message.chat.id, None)
        bot.send_message(message.chat.id, "⚠️ Сесія додавання пошкоджена. Почніть ще раз через /admin.")
        return
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        image_path = f"{key}.jpg"
        with open(image_path, "wb") as image_file:
            image_file.write(downloaded_file)
        finish_admin_product(message.chat.id, image_path)
    except Exception as e:
        print("❌ ADMIN IMAGE DOWNLOAD:", e)
        bot.send_message(
            message.chat.id,
            "⚠️ Не вдалося завантажити фото. Надішліть його ще раз або пропустіть цей крок.",
            reply_markup=image_skip_markup()
        )

def handle_admin_text_input(message):
    user_id = message.chat.id
    state = user_data_cache.get(user_id, {})
    step = state.get("step", "")
    if not step.startswith("admin_"):
        return False
    if not is_admin(user_id):
        user_data_cache.pop(user_id, None)
        return False

    text = (message.text or "").strip()

    if step == "admin_add_key":
        key = normalize_product_key(text)
        if not key:
            bot.send_message(
                user_id,
                "⚠️ Використовуйте лише англійські літери, цифри та пробіли/підкреслення (до 40 символів). Спробуйте ще раз:"
            )
            return True
        if key in PRODUCTS:
            bot.send_message(user_id, "⚠️ Такий ключ уже існує. Введіть інший:")
            return True
        state["product_key"] = key
        state["step"] = "admin_add_name"
        bot.send_message(user_id, f"Ключ буде збережено як `{key}`.\nВведіть публічну назву товару:", parse_mode="Markdown")
        return True

    if step == "admin_add_name":
        if not text:
            bot.send_message(user_id, "⚠️ Назва не може бути порожньою. Введіть назву товару:")
            return True
        state["name"] = text
        state["step"] = "admin_add_price"
        bot.send_message(user_id, "Введіть ціну товару в гривнях:")
        return True

    if step == "admin_add_price":
        try:
            price = float(text.replace(",", "."))
            if not math.isfinite(price) or price <= 0:
                raise ValueError
        except (TypeError, ValueError):
            bot.send_message(user_id, "⚠️ Ціна має бути додатним числом. Спробуйте ще раз:")
            return True
        state["price"] = price
        state["step"] = "admin_add_category"
        bot.send_message(user_id, "Оберіть категорію товару:", reply_markup=category_picker_markup())
        return True

    if step == "admin_add_category":
        bot.send_message(user_id, "Оберіть категорію кнопкою під попереднім повідомленням.")
        return True

    if step == "admin_add_category_name":
        if not text:
            bot.send_message(user_id, "⚠️ Назва категорії не може бути порожньою. Спробуйте ще раз:")
            return True
        cat_id = generate_category_id(text)
        try:
            with sqlite3.connect("pinkcanna.db") as conn:
                c = conn.cursor()
                c.execute("INSERT INTO categories (cat_id, name) VALUES (?, ?)", (cat_id, text))
                conn.commit()
            load_products_from_db()
        except sqlite3.Error as e:
            print("❌ ADMIN ADD CATEGORY:", e)
            bot.send_message(user_id, "⚠️ Не вдалося створити категорію. Введіть іншу назву:")
            return True
        state["category"] = cat_id
        state["step"] = "admin_add_short"
        bot.send_message(user_id, f"✅ Категорію «{text}» створено.")
        prompt_admin_short(user_id)
        return True

    if step == "admin_add_short":
        if not text:
            bot.send_message(user_id, "⚠️ Короткий опис не може бути порожнім. Спробуйте ще раз:")
            return True
        state["short"] = text
        state["step"] = "admin_add_info"
        bot.send_message(user_id, "Введіть детальний опис товару (можна використовувати HTML/Markdown):")
        return True

    if step == "admin_add_info":
        if not text:
            bot.send_message(user_id, "⚠️ Детальний опис не може бути порожнім. Спробуйте ще раз:")
            return True
        state["info"] = text
        state["step"] = "admin_add_image"
        prompt_admin_image(user_id)
        return True

    if step == "admin_add_image":
        bot.send_message(
            user_id,
            "Надішліть фото як зображення або натисніть кнопку пропуску.",
            reply_markup=image_skip_markup()
        )
        return True

    if step == "admin_stock_qty":
        try:
            qty = int(text)
            if qty < 0:
                raise ValueError
        except (TypeError, ValueError):
            bot.send_message(user_id, "⚠️ Введіть ціле невід’ємне число:")
            return True
        key = state.get("product_key")
        if key not in PRODUCTS:
            user_data_cache.pop(user_id, None)
            bot.send_message(user_id, "⚠️ Товар більше не існує.", reply_markup=admin_menu_markup())
            return True
        db_set_stock(key, qty)
        user_data_cache.pop(user_id, None)
        bot.send_message(user_id, f"✅ Залишок «{PRODUCTS[key]['name']}» оновлено: {qty} шт.", reply_markup=admin_menu_markup())
        return True

    if step == "admin_broadcast_content":
        if not text:
            bot.send_message(user_id, "⚠️ Текст розсилки не може бути порожнім.")
            return True
        show_broadcast_preview(user_id, {"type": "text", "text": message.text})
        return True

    if step == "admin_broadcast_preview":
        bot.send_message(user_id, "Підтвердьте або скасуйте розсилку кнопками під попереднім переглядом.")
        return True

    return False

# --- ОБРОБНИК ТЕКСТУ ТА РЕЄСТРАЦІЯ ---
@bot.message_handler(func=lambda m: True)
def handle_all_text(message):
    user_id = message.chat.id
    text = message.text

    if handle_admin_text_input(message):
        return

    if user_id in user_data_cache and 'step' in user_data_cache[user_id]:
        state = user_data_cache[user_id]
        if state['step'] == 'register_name':
            state['name'] = text
            state['step'] = 'register_sex'
            m = types.ReplyKeyboardMarkup(resize_keyboard=True).add("👨 Чоловіча", "👩 Жіноча")
            bot.send_message(user_id, "Оберіть Вашу стать:", reply_markup=m)
            return
        elif state['step'] == 'register_sex':
            state['sex'] = 1 if text == "👨 Чоловіча" else 2 if text == "👩 Жіноча" else 0
            state['step'] = 'register_birthday'
            bot.send_message(user_id, "Введіть дату народження (формат ДД.ММ.РРРР):", reply_markup=types.ReplyKeyboardRemove())
            return
        elif state['step'] == 'register_birthday':
            if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
                state['birthday'] = text
                state['step'] = 'register_email'
                bot.send_message(user_id, "Введіть Ваш E-mail (або натисніть 'Пропустити'):", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("Пропустити ➡️"))
            return
        elif state['step'] == 'register_email':
            state['email'] = text if text != "Пропустити ➡️" else ""
            db_manage_user(user_id, phone=state['phone'])
            create_poster_client_full(user_id)
            bot.send_message(user_id, "🎉 Ваша карта клієнта успішно створена!", reply_markup=main_menu())
            del user_data_cache[user_id]
            return

    if text in ["📂 Каталог", "🛒 Кошик", "📞 Консультант", "🎁 Отримати знижку (Гра)", "🧮 Підбір дози CBD", "👤 Профіль"]: 
        if text == "📞 Консультант":
            bot.send_message(user_id, "👨‍💻 Ваш запит передано! Наш менеджер зв'яжеться з Вами найближчим часом.")
            if ADMIN_ID:
                username = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"
                user_db = db_manage_user(user_id)
                phone_info = f"\n📱 Телефон: `{user_db[0]}`" if user_db and user_db[0] else "\n📱 Телефон: Ще не надав"
                try: bot.send_message(ADMIN_ID, f"🙋‍♂️ **Запит на живу консультацію!**\n\nКлієнт: {username}{phone_info}\nНапишіть йому в особисті повідомлення.", parse_mode="Markdown")
                except: pass
            return
        return
    
    # AI Logic
    bot.send_chat_action(user_id, 'typing')
    history = db_manage_history(user_id)
    db_manage_history(user_id, "user", text)
    avail = [f"{k}: {p['name']}" for k, p in PRODUCTS.items() if db_get_stock(k) > 0]
    
    system_prompt = f"""Ти — привітний, емпатичний та експертний AI-консультант магазину та закладу "Pink Canna".
Твоя мета: допомагати клієнтам підбирати продукцію (CBD олії, екстракти Канни, добавки для сну/енергії, а також каву, десерти та коктейлі), відповідати на їхні питання та створювати атмосферу спокою та релаксу.

Тон спілкування: дружній, турботливий, сучасний. Звертайся до клієнта на "Ви", але без зайвого офіціозу. Використовуй доречні емодзі (🌿, 💧, ☕️, 🧠, 🍰).

📦 ПРАВИЛА РОБОТИ З ТОВАРАМИ (КРИТИЧНО ВАЖЛИВО):
Наразі в наявності є такі товари: {', '.join(avail)}.
Коли ти рекомендуєш якийсь із цих товарів, ти ПОВИНЕН вставити його код (англійське слово до двокрапки) у квадратних дужках. 
Приклад правильної відповіді: "Для легкого розслаблення ідеально підійде наша олія 10% [cbd_10_10], а до неї радимо смачний капучино [cappuccino]."
Ніколи не вигадуй коди товарів! Використовуй тільки ті, що є в списку наявності.

🩺 ПРАВИЛА КОНСУЛЬТАЦІЇ:
1. Ти не лікар. Якщо людина описує серйозні захворювання, порадь звернутися до фахівця, але запропонуй CBD як допоміжний засіб. Нагадуй, що СБД — це дієтична добавка.
2. CBD легальний в Україні, не містить ТГК (THC) і не викликає залежності.
3. Канна (Sceletium tortuosum) — це легальна рослина, природний релаксант та покращувач настрою.
4. Якщо клієнт не знає, яку дозу обрати, нагадай, що внизу є зручна кнопка "🧮 Підбір дози CBD".

🛑 ОБМЕЖЕННЯ:
- Відповідай лаконічно (до 100-150 слів). Користувачі Telegram люблять короткі та чіткі відповіді. Структуруй текст списками.
- Якщо питають про те, що не стосується Pink Canna, асортименту чи релаксу — ввічливо повертай тему до нашого закладу.
- Якщо користувач хоче поговорити з живою людиною, скажи йому натиснути кнопку "📞 Консультант" у меню."""
    
    try:
        response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": text}])
        ai_text = response.choices[0].message.content
        db_manage_history(user_id, "assistant", ai_text)
        keys = re.findall(r'\[([a-zA-Z0-9_]+)\]', ai_text)
        bot.send_message(user_id, re.sub(r'\[[a-zA-Z0-9_]+\]', '', ai_text).strip())
        for k in keys:
            if k in PRODUCTS and db_get_stock(k) > 0: send_product_card(user_id, k)
    except: bot.send_message(user_id, "⚠️ На жаль, AI тимчасово недоступний. Спробуйте пізніше.")

if __name__ == "__main__":
    bot.infinity_polling()
