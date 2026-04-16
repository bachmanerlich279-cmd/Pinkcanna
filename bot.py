import telebot
from telebot import types
import os
import re
from openai import OpenAI

# --- НАЛАШТУВАННЯ ---
TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEB_APP_URL = "https://mamutpetr.github.io/Pinkcanna/"

bot = telebot.TeleBot(TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)

# --- ТОВАРИ ---
# Оновлено ціни та додано назви файлів зображень
PRODUCTS = {
    "sleep": {"name": "Happy caps sleep (Інгалятор) 💤", "price": 2000, "image": "sleep.jpg"},
    "gaba": {"name": "Габа #9 🧠", "price": 400, "image": "gaba9.jpg"},
    "energy": {"name": "Happy caps energy ⚡", "price": 2000, "image": "energy.jpg"},
    "cream": {"name": "СБД Крем 🧴", "price": 1600, "image": "cream.jpg"},
    "vape": {"name": "Вейп 💨", "price": 3000, "image": "blackvape.jpg"},
    "jelly": {"name": "СБД Желе 🍬", "price": 1900, "image": "Cbdgele.jpg"}
}

user_carts = {}
user_tapped_discounts = {}

number_words = {
    "одну": 1, "один": 1, "дві": 2, "два": 2, "три": 3, "чотири": 4, "п’ять": 5, "шість": 6
}

# --- ГОЛОВНЕ МЕНЮ ---
def main_menu():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.add("📂 Каталог", "🛒 Кошик")
    m.add(types.KeyboardButton("🍀 Натапати знижку", web_app=types.WebAppInfo(url=WEB_APP_URL)))
    m.add("📞 Консультант", "📰 Новини")
    return m

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "🌿 Вітаємо у Pink Canna! Обирай якісний CBD.", reply_markup=main_menu())

# --- РОЗДІЛ НОВИНИ ---
@bot.message_handler(func=lambda m: m.text == "📰 Новини")
def news_section(message):
    text = (
        "🌿 **Що таке CBD?**\n\n"
        "CBD (Каннабідіол) — це натуральний екстракт конопель, який допомагає організму долати стрес, "
        "біль та безсоння. Він **не є психоактивним**, тому не викликає відчуття «кайфу».\n\n"
        "⚖️ **Чому це легально?**\n\n"
        "В Україні ізолят CBD виключений зі списку наркотичних речовин згідно з **Постановою КМУ №324**. "
        "Наші продукти легальні, сертифіковані та безпечні для використання."
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

# --- КАТАЛОГ З ФОТО ---
@bot.message_handler(func=lambda m: m.text == "📂 Каталог")
def catalog(message):
    bot.send_message(message.chat.id, "⬇️ Натисніть кнопку під фото, щоб замовити:")
    for key, item in PRODUCTS.items():
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(f"➕ Купити за {item['price']} грн", callback_data=f"buy_{key}_1"))
        
        caption = f"🏷 **{item['name']}**\n💰 Ціна: {item['price']} грн"
        
        try:
            if os.path.exists(item['image']):
                with open(item['image'], 'rb') as photo:
                    bot.send_photo(message.chat.id, photo, caption=caption, reply_markup=markup, parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, caption, reply_markup=markup, parse_mode="Markdown")
        except Exception:
            bot.send_message(message.chat.id, caption, reply_markup=markup, parse_mode="Markdown")

# --- КОШИК ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def add_to_cart(call):
    parts = call.data.split("_")
    key = parts[1]
    count = int(parts[2])
    user_carts.setdefault(call.message.chat.id, []).extend([key]*count)
    bot.answer_callback_query(call.id, f"✅ Додано: {PRODUCTS[key]['name']}")

@bot.message_handler(func=lambda m: m.text == "🛒 Кошик")
def show_cart(message):
    chat_id = message.chat.id
    if chat_id not in user_carts or not user_carts[chat_id]:
        bot.send_message(chat_id, "🛒 Кошик порожній.")
        return

    items = user_carts[chat_id]
    total = sum(PRODUCTS[k]['price'] for k in items)
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Оформити", callback_data="checkout"))
    markup.add(types.InlineKeyboardButton("🗑 Очистити", callback_data="clear_cart"))
    
    summary = "\n".join([f"• {PRODUCTS[k]['name']} x{items.count(k)}" for k in set(items)])
    bot.send_message(chat_id, f"**Твій кошик:**\n{summary}\n\n💰 **Разом: {total} грн**", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "clear_cart")
def clear(call):
    user_carts[call.message.chat.id] = []
    bot.edit_message_text("🗑 Кошик порожній", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "checkout")
def checkout(call):
    send_invoice(call.message.chat.id)

# --- ОПЛАТА ---
def send_invoice(chat_id):
    items = user_carts.get(chat_id, [])
    prices = []
    total_price = 0

    for key in set(items):
        count = items.count(key)
        p = PRODUCTS[key]['price']
        total_price += p * count
        prices.append(types.LabeledPrice(f"{PRODUCTS[key]['name']} x{count}", p * count * 100))

    discount = user_tapped_discounts.get(chat_id, 0)
    if discount > 0:
        if discount >= total_price: discount = total_price - 1
        prices.append(types.LabeledPrice("🍀 Знижка", -int(discount * 100)))

    bot.send_invoice(
        chat_id, title="Pink Canna", description="Оплата замовлення",
        invoice_payload="order", provider_token=PAYMENT_TOKEN,
        currency="UAH", prices=prices, start_parameter="pay",
        need_phone_number=True, need_shipping_address=True
    )

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(q):
    bot.answer_pre_checkout_query(q.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def success(message):
    bot.send_message(message.chat.id, "✅ Дякуємо! Замовлення прийнято.")
    user_carts[message.chat.id] = []

# --- AI-КОНСУЛЬТАНТ ---
@bot.message_handler(func=lambda m: True)
def ai_consultant(message):
    if message.text in ["📂 Каталог", "🛒 Кошик", "📞 Консультант", "🍀 Натапати знижку", "📰 Новини"]:
        return

    try:
        catalog_text = ", ".join([f"{p['name']} ({p['price']} грн)" for p in PRODUCTS.values()])
        # Змінено модель на gpt-4o, бо gpt-5 ще не існує
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"Ти консультант Pink Canna. Продавай: {catalog_text}. На питання про легальність відповідай: легально згідно з Постановою КМУ №324."},
                {"role": "user", "content": message.text}
            ]
        )
        bot.send_message(message.chat.id, response.choices[0].message.content)
    except Exception:
        bot.send_message(message.chat.id, "⚠️ Консультант тимчасово недоступний.")

if __name__ == "__main__":
    bot.infinity_polling()
