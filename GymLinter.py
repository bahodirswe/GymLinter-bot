import os
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from dotenv import load_dotenv

from sqlalchemy import create_engine, String, ForeignKey, Integer, func, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, relationship, scoped_session, joinedload
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, InputMediaPhoto
from telegram.constants import ParseMode
from flask import Flask
from threading import Thread
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)



load_dotenv()

# --- KONFIGURATSIYA ---
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL_ENV = os.getenv("DATABASE_URL")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin") 

if DATABASE_URL_ENV and DATABASE_URL_ENV.startswith("postgres://"):
    FINAL_DB_URL = DATABASE_URL_ENV.replace("postgres://", "postgresql://", 1)
else:
    FINAL_DB_URL = DATABASE_URL_ENV or "sqlite:///gym_linter_bot.db"

# Engine yaratish (faqat bir marta shu yerda bo'lishi kerak)
if "sqlite" in FINAL_DB_URL:
    engine = create_engine(FINAL_DB_URL, connect_args={'check_same_thread': False})
else:
    engine = create_engine(FINAL_DB_URL)


try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", 0)) 
except:
    ADMIN_ID = 0

try:
    GROUP_ID = int(os.getenv("GROUP_ID").strip())
except:
    GROUP_ID = None

try:
    INFO_TOPIC_ID = int(os.getenv("INFO_TOPIC_ID", 130)) # 130 - biz aniqlagan ID
except:
    INFO_TOPIC_ID = None

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- RENDER BEPUL TARIF UCHUN PORT OCHISH ---
app_flask = Flask('')

@app_flask.route('/')
def home():
    return "Bot is running live!"

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app_flask.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
# --------------------------------------------

# --- DATABASE MODELLARI ---
class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = 'users'
    tg_id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String)
    nickname: Mapped[str] = mapped_column(String, index=True, unique=True)
    gender: Mapped[str] = mapped_column(String)
    warnings: Mapped[int] = mapped_column(default=0)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_count: Mapped[int] = mapped_column(default=0)
    bookings: Mapped[list["Booking"]] = relationship("Booking", back_populates="user")

    @property
    def rank(self):
        if self.completed_count < 10: return "🌱 Yangi"
        if self.completed_count < 30: return "🔥 Faol"
        if self.completed_count < 100: return "🏆 Atlet"
        return "👑 Zal Afsonasi"

class Booking(Base):
    __tablename__ = 'bookings'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.tg_id'))
    slot_time: Mapped[str] = mapped_column(String)
    date: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(default='active') 
    joined_users: Mapped[str] = mapped_column(String, default="") 
    user: Mapped["User"] = relationship("User", back_populates="bookings")

# DB ulanishi
Base.metadata.create_all(engine)
session_factory = sessionmaker(bind=engine, expire_on_commit=False)
Session = scoped_session(session_factory)

@contextmanager
def get_db_session():
    session = Session()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        session.close()

REG_NAME, REG_GENDER, REG_NICK, CONFIRM_NICK, UPLOAD_PHOTOS = range(5)

# YANGI QO'SHILADIGAN HOLATLAR (Admin tahrirlashi uchun)
EDIT_USER_FIELD, EDIT_USER_VALUE = range(5, 7)

# --- UTILS ---
def get_mention(tg_id, name):
    return f'<a href="tg://user?id={tg_id}">{name}</a>'

# --- KEYBOARDS ---
def get_main_menu(user_id, is_blocked=False):
    if is_blocked:
        return ReplyKeyboardMarkup([["👨‍✈️ Admin bilan bog'lanish"]], resize_keyboard=True)
    
    menu = [
        ["📅 Smenalar", "🏆 Reyting"], 
        ["❓ Zal holati", "📸 Smenani yakunlash"],
        ["📊 Statistika", "⚠️ Jarimalarim"],
        ["🚫 Chiterlar", "👨‍✈️ Admin bilan bog'lanish"]
    ]
    
    if user_id == ADMIN_ID:
        # Admin uchun qo'shimcha boshqaruv tugmalari
        menu.append(["👥 Barcha foydalanuvchilar"]) # Yangi tugma
        menu.append(["🔐 Bloklanganlarni boshqarish"])
        
    return ReplyKeyboardMarkup(menu, resize_keyboard=True)

def get_gender_keyboard():
    return ReplyKeyboardMarkup([["Erkak 👨", "Ayol 👩"]], resize_keyboard=True, one_time_keyboard=True)

def get_days_keyboard():
    today = datetime.now()
    keyboard = []
    labels = ["Bugun", "Ertaga", "Indinga"]
    for i in range(3):
        day = today + timedelta(days=i)
        keyboard.append([InlineKeyboardButton(f"{day.strftime('%d-%m')} ({labels[i]})", callback_data=f"day_{day.strftime('%Y-%m-%d')}")])
    return InlineKeyboardMarkup(keyboard)

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    
    user_id = update.effective_user.id
    with get_db_session() as session:
        user = session.get(User, user_id)
        
        if user:
            if user.is_blocked:
                await update.message.reply_text(
                    f"🚫 **Siz bloklangansiz!**\nSizning ID: `{user_id}`\nAdmin bilan bog'lanib muammoni hal qiling.",
                    # TUZATISH: user_id ni argument sifatida uzatamiz
                    reply_markup=get_main_menu(user_id, is_blocked=True), 
                    parse_mode=ParseMode.MARKDOWN
                )
                return ConversationHandler.END
            
            await update.message.reply_text(
                f"Xush kelibsiz! Darajangiz: {user.rank}", 
                # TUZATISH: user_id ni argument sifatida uzatamiz
                reply_markup=get_main_menu(user_id, is_blocked=False) 
            )
            return ConversationHandler.END
        
        await update.message.reply_text("Ism-Familiyangizni yuboring:", reply_markup=ReplyKeyboardRemove())
        return REG_NAME

async def finish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    with get_db_session() as session:
        all_bookings = session.query(Booking).filter_by(
            user_id=user_id, date=today_str, status='active'
        ).order_by(Booking.slot_time).all()

        if not all_bookings:
            await update.message.reply_text("Sizda hozircha faol smena yo'q!")
            return ConversationHandler.END

        chains = []
        current_chain = [all_bookings[0]]
        for i in range(1, len(all_bookings)):
            prev_end_h = int(all_bookings[i-1].slot_time.split("-")[1].split(":")[0])
            curr_start_h = int(all_bookings[i].slot_time.split("-")[0].split(":")[0])
            
            if curr_start_h == prev_end_h:
                current_chain.append(all_bookings[i])
            else:
                chains.append(current_chain)
                current_chain = [all_bookings[i]]
        chains.append(current_chain)

        target_chain = None
        for chain in chains:
            first_start_h = int(chain[0].slot_time.split("-")[0].split(":")[0])
            last_end_h = int(chain[-1].slot_time.split("-")[1].split(":")[0])
            
            if now.hour >= first_start_h:
                target_chain = chain
                # Agar oxirgi soat 24 bo'lsa ham tekshirishni to'g'irlaymiz
                if now.hour < last_end_h or last_end_h == 24:
                    break

        if not target_chain:
            await update.message.reply_text("Hozirgi vaqtda yakunlash mumkin bo'lgan smenangiz yo'q!")
            return ConversationHandler.END

        # --- VAQTNI TEKSHIRISHDA 24:00 MUAMMOSINI TUZATISH ---
        last_slot_end = target_chain[-1].slot_time.split("-")[1] # Masalan "24:00"
        
        if last_slot_end == "24:00":
            # Agar 24:00 bo'lsa, uni bugungi kunning 23:59:59 qilib olamiz yoki 
            # ertangi kunning 00:00 qilib hisoblaymiz
            end_time = datetime.strptime(f"{today_str} 23:59", "%Y-%m-%d %H:%M") + timedelta(minutes=1)
        else:
            end_time = datetime.strptime(f"{today_str} {last_slot_end}", "%Y-%m-%d %H:%M")

        allowed_time = end_time - timedelta(minutes=10)

        if now < allowed_time:
            wait_min = int((allowed_time - now).total_seconds() // 60)
            await update.message.reply_text(f"⚠️ Smenangiz {last_slot_end} da tugaydi. Yana {wait_min} daqiqa kuting.")
            return ConversationHandler.END

        context.user_data['chain_ids'] = [b.id for b in target_chain]

    context.user_data['temp_photos'] = []
    await update.message.reply_text(f"Ketma-ket {len(target_chain)} ta smena yakunlanmoqda. 3 ta rasm yuboring:")
    return UPLOAD_PHOTOS

async def contact_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # .env dan olingan username orqali link yaratamiz
    # Agar username @ belgisi bilan yozilgan bo'lsa, uni tozalaymiz
    clean_username = ADMIN_USERNAME.replace("@", "")
    admin_link = f"https://t.me/{clean_username}"
    
    text = (
        "👨‍✈️ **Adminstratsiya bilan aloqa**\n\n"
        "Savollaringiz yoki muammolar bo'yicha quyidagi profilga murojaat qiling:\n"
        f"👉 [Admin bilan bog'lanish]({admin_link})\n\n"
        "⚠️ *Bloklangan bo'lsangiz, ismingiz va ID raqamingizni yuboring.*"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    
async def day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if 'forced_date' in context.user_data:
        date_str = context.user_data.pop('forced_date')
    else:
        date_str = query.data.split("_")[1]
        
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    hours = [f"{h:02d}:00-{(h+1):02d}:00" for h in range(0, 24)]
    keyboard, row = [], []

    with get_db_session() as session:
        booked = session.query(Booking).options(joinedload(Booking.user)).filter(
            Booking.date == date_str, 
            Booking.status != 'rejected'
        ).all()
        bookings_dict = {b.slot_time: b for b in booked}

        for h in hours:
            start_h = int(h[:2])
            end_h = int(h[6:8])
            if end_h == 0: end_h = 24  # 24:00 holati uchun
            
            # O'tib ketgan vaqtni tekshirish
            is_past = (date_str == today_str and start_h < now.hour)
            # AYNI VAQTDA DAVOM ETAYOTGAN SMENANI ANIQLASH
            is_current = (date_str == today_str and start_h <= now.hour < end_h)
            
            if is_past: 
                # O'tib ketgan smenalar
                btn = InlineKeyboardButton(f"🕒 {h[:5]}", callback_data="none")
            elif h in bookings_dict:
                # Band qilingan smenalar
                b = bookings_dict[h]
                joined_count = len([n for n in b.joined_users.split(",") if n.strip()]) if b.joined_users else 0
                total_count = 1 + joined_count
                
                # Agar hozirgi smena bo'lsa ⚡️, bo'lmasa jinsga qarab emoji
                status_emoji = "⚡️" if is_current else ("👨" if b.user.gender == "Erkak" else "👩")
                btn = InlineKeyboardButton(f"{status_emoji} {total_count}/5 @{b.user.nickname}", callback_data=f"join_{b.id}")
            else:
                # Bo'sh smenalar (Hozirgi vaqt bo'lsa ⚡️ bilan ajratiladi)
                prefix = "⚡️" if is_current else "✅"
                btn = InlineKeyboardButton(f"{prefix} {h}", callback_data=f"slot_{h}_{date_str}")
            
            row.append(btn)
            if len(row) == 2: 
                keyboard.append(row)
                row = []
        
        if row: keyboard.append(row)
        keyboard.append([InlineKeyboardButton("❌ Smenamni bekor qilish", callback_data="cancel_my_slot")])
        
        try:
            await query.edit_message_text(
                f"📅 <b>{date_str}</b> jadvali (Maks: 5 kishi):\n"
                f"<i>(⚡️ - ayni vaqtdagi smena)</i>", 
                reply_markup=InlineKeyboardMarkup(keyboard), 
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"day_callback error: {e}")

async def slot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data == "none":
        await query.answer("Bu vaqt o'tib ketgan!", show_alert=True)
        return

    with get_db_session() as session:
        user = session.get(User, user_id) 
        if not user or user.is_blocked:
            await query.answer("🚫 Kirish taqiqlangan!", show_alert=True)
            return

        target_date = None

        # --- 1. SMENANI BEKOR QILISH (20 MINUTLIK JARIMA BILAN) ---
        if data == "cancel_my_slot":
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            
            # Eng yaqin faol smenani qidirish
            my_b = session.query(Booking).filter(
                Booking.user_id == user_id, 
                Booking.status == 'active', 
                Booking.date >= today_str
            ).order_by(Booking.id.desc()).first()
            
            if my_b:
                start_time_str = my_b.slot_time.split("-")[0]
                smena_start_dt = datetime.strptime(f"{my_b.date} {start_time_str}", "%Y-%m-%d %H:%M")
                diff_minutes = (now - smena_start_dt).total_seconds() / 60
                
                penalty_text = ""
                # SHART: Smena boshlangan bo'lsa VA 20 minutdan ko'proq vaqt o'tgan bo'lsa
                if now > smena_start_dt and diff_minutes > 20:
                    user.warnings += 1
                    if user.warnings >= 3:
                        user.is_blocked = True
                    penalty_text = f"\n⚠️ <b>KECHIKIB BEKOR QILISH:</b>\nSmena boshlanganiga {int(diff_minutes)} minut bo'lgani uchun +1 jarima berildi! ({user.warnings}/3)"

                target_date = my_b.date
                user_mention = get_mention(user.tg_id, user.nickname)
                
                # 130-mavzuga xabardor qilish
                msg_info = (
                    f"♻️ <b>SMENA OCHILDI (BEKOR QILINDI)</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"👤 <b>Kim:</b> {user_mention}\n"
                    f"📅 <b>Sana:</b> {target_date}\n"
                    f"⏰ <b>Vaqt:</b> {my_b.slot_time}"
                    f"{penalty_text}\n"
                    f"━━━━━━━━━━━━━━"
                )
                
                session.delete(my_b)
                await query.answer("Smena bekor qilindi!", show_alert=True)
                
                if GROUP_ID:
                    await context.bot.send_message(
                        chat_id=GROUP_ID,
                        message_thread_id=INFO_TOPIC_ID, # 130-mavzu
                        text=msg_info, 
                        parse_mode=ParseMode.HTML
                    )
            else:
                await query.answer("Sizda faol smena topilmadi.", show_alert=True)
                return

        # --- 2. SMENAGA QO'SHILISH (JOIN) ---
        elif data.startswith("join_"):
            parts = data.split("_")
            b_id = int(parts[1])
            b = session.get(Booking, b_id)
            if not b: return
            target_date = b.date
            joined_list = [n.strip() for n in b.joined_users.split(",") if n.strip()]
            user_nick = f"@{user.nickname}"
            
            if b.user_id == user_id or user_nick in joined_list:
                await query.answer("Siz allaqachon ushbu smenadasiz!", show_alert=True)
                return
            if len(joined_list) + 1 >= 5:
                await query.answer("Kecherasiz, bu smenada joy qolmagan (Maks 5 kishi)!", show_alert=True)
                return
                
            joined_list.append(user_nick)
            b.joined_users = ", ".join(joined_list)
            session.add(b)
            session.commit() # Joinni saqlash

            # JOIN QILGANGA HAM ESLATMA
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🤝 <b>Smenaga muvaffaqiyatli qo'shildingiz!</b>\n"
                        f"⏰ <b>Vaqt:</b> {b.slot_time}\n\n"
                        f"📌 Siz ham ushbu smena uchun hisobot (3 rasm) berishga mas'ulsiz."
                    ),
                    parse_mode=ParseMode.HTML
                )
            except: pass
            
            await query.answer("Muvaffaqiyatli qo'shildingiz!")

        # --- 3. YANGI SMENA BAND QILISH (SLOT) ---
        elif data.startswith("slot_"):
            payload = data[len("slot_"):]         
            slot, s_date = payload.rsplit("_", 1)
            target_date = s_date
            
            daily_count = session.query(Booking).filter(
                Booking.user_id == user_id, 
                Booking.date == s_date, 
                Booking.status != 'rejected'
            ).count()
            
            if daily_count >= 3:
                await query.answer("Kunlik limit: 3 ta!", show_alert=True)
                return
            
            # Bazaga yangi smenani qo'shish
            new_booking = Booking(user_id=user_id, slot_time=slot, date=s_date)
            session.add(new_booking)
            session.commit() # Ma'lumotni bazaga yozishni tasdiqlaymiz

            # 1. Guruhga xabar yuborish (130-topic)
            if GROUP_ID:
                mention = get_mention(user.tg_id, f"@{user.nickname}")
                await context.bot.send_message(
                    chat_id=GROUP_ID, 
                    message_thread_id=INFO_TOPIC_ID,
                    text=f"📌 {mention} smena oldi: {slot}", 
                    parse_mode=ParseMode.HTML
                )

            # 2. FOYDALANUVCHIGA SHAXSIY XABAR (ESLATMA) YUBORISH
            reminder_text = (
                f"✅ <b>Smena band qilindi!</b>\n"
                f"⏰ <b>Vaqt:</b> {slot}\n"
                f"📅 <b>Sana:</b> {s_date}\n\n"
                f"📝 <b>MUHIM QOIDALAR:</b>\n"
                f"1. Smenangiz tugashiga 10 daqiqa qolganda botga <b>3 ta rasm</b> (zal holati) yuboring.\n"
                f"2. Hisobot topshirish uchun 📸 <b>'Smenani yakunlash'</b> tugmasidan foydalaning.\n"
                f"3. Smena tugagach 1 soat ichida hisobot kelmasa, avtomatik <b>+1 jarima</b> beriladi.\n\n"
                f"🚫 3 ta jarima = <b>Blok!</b>"
            )
            
            try:
                await context.bot.send_message(
                    chat_id=user_id, 
                    text=reminder_text, 
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Foydalanuvchiga eslatma yuborishda xato: {e}")

            await query.answer("Smena band qilindi!")

        if target_date:
            context.user_data['forced_date'] = target_date
            return await day_callback(update, context)

async def gym_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    with get_db_session() as session:
        bookings = session.query(Booking).options(joinedload(Booking.user)).filter(Booking.date == today_str, Booking.status == 'active').all()
        current_b = None
        for b in bookings:
            start_h = int(b.slot_time.split("-")[0].split(":")[0])
            end_str = b.slot_time.split("-")[1].split(":")[0]
            end_h = int(end_str)
            # 24:00 maxsus holat — oxirgi soat (23:xx)
            is_current = (end_h == 24 and now.hour == 23) or (start_h <= now.hour < end_h)
            if is_current:
                current_b = b
                break
        if current_b:
            msg = (f"🟢 <b>ZAL BAND</b>\n\n⏰ Vaqt: {current_b.slot_time}\n👤 Mas'ul: @{current_b.user.nickname}\n👥 Sheriklar: {current_b.joined_users or 'Yo\'q'}")
        else:
            msg = "⚪️ <b>ZAL BO'SH</b>\n\nHozircha hech kim smena olmagan."
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def handle_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo: 
        return UPLOAD_PHOTOS
    
    context.user_data.setdefault('temp_photos', []).append(update.message.photo[-1].file_id)
    
    if len(context.user_data['temp_photos']) < 3:
        await update.message.reply_text(f"📸 {len(context.user_data['temp_photos'])}/3 rasm qabul qilindi. Yana yuboring...")
        return UPLOAD_PHOTOS
    
    user_id = update.effective_user.id
    chain_ids = context.user_data.get('chain_ids', [])

    with get_db_session() as session:
        user = session.get(User, user_id)
        bookings_to_close = session.query(Booking).filter(Booking.id.in_(chain_ids)).all()
        
        if not bookings_to_close:
            await update.message.reply_text("Sizda faol smena topilmadi!", reply_markup=get_main_menu(user_id))
            return ConversationHandler.END

        for b in bookings_to_close: 
            b.status = 'pending'
            
        first_b, last_b = bookings_to_close[0], bookings_to_close[-1]
        time_range = f"{first_b.slot_time.split('-')[0]} - {last_b.slot_time.split('-')[1]}"
        
        if GROUP_ID:
            caption_text = (
                f"🔎 <b>YANGI HISOBOT:</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"👤 <b>Mas'ul:</b> {get_mention(user_id, f'@{user.nickname}')}\n"
                f"📅 <b>Sana:</b> {first_b.date}\n"
                f"⏰ <b>Vaqt:</b> {time_range}\n"
                f"━━━━━━━━━━━━━━\n"
                f"⚠️ <i>Faqat Admin tasdiqlashi mumkin.</i>"
            )
            
            media = []
            for i, file_id in enumerate(context.user_data['temp_photos']):
                if i == 0:
                    media.append(InputMediaPhoto(file_id, caption=caption_text, parse_mode=ParseMode.HTML))
                else:
                    media.append(InputMediaPhoto(file_id))
            
            # 1. Rasmlar guruhidagi kerakli mavzuga yuboriladi
            await context.bot.send_media_group(
                chat_id=GROUP_ID, 
                media=media,
                message_thread_id=INFO_TOPIC_ID # <--- QO'SHILDI
            )
            
            keyboard = [[
                InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"rev_app_{first_b.id}"), 
                InlineKeyboardButton("❌ Rad etish", callback_data=f"rev_rej_{first_b.id}")
            ]]
            
            # 2. Admin tugmalari ham o'sha mavzuga yuboriladi
            await context.bot.send_message(
                chat_id=GROUP_ID, 
                message_thread_id=INFO_TOPIC_ID, # <--- QO'SHILDI
                text=f"👆 @{user.nickname} ning hisobotini tekshiring:", 
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    context.user_data.clear()
    
    await update.message.reply_text(
        "✅ Rasmlar guruhga yuborildi. Admin tasdiqlashini kuting.", 
        reply_markup=get_main_menu(user_id)
    )
    return ConversationHandler.END

async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # Faqat admin ekanligini qat'iy tekshirish
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔️ Bu amal faqat admin uchun!", show_alert=True)
        return

    data_parts = query.data.split("_")
    # action: "app" yoki "rej"
    action, b_id = data_parts[1], int(data_parts[2])

    with get_db_session() as session:
        # Booking va User ma'lumotlarini birga yuklaymiz
        main_b = session.query(Booking).options(joinedload(Booking.user)).get(b_id)
        
        # Hisobot holatini tekshirish (faqat 'pending' bo'lsa ko'rish mumkin)
        if not main_b or main_b.status != 'pending': 
            await query.answer("⚠️ Bu hisobot allaqachon ko'rib chiqilgan!", show_alert=True)
            # Tugmalarni olib tashlaymiz
            await query.edit_message_reply_markup(reply_markup=None) 
            return

        # Ushbu foydalanuvchining o'sha kundagi barcha kutilayotgan smenalarini topamiz
        chain = session.query(Booking).filter(
            Booking.user_id == main_b.user_id, 
            Booking.date == main_b.date, 
            Booking.status == 'pending'
        ).all()
        
        # --- TASDIQLASH ---
        if action == "app":
            for b in chain: 
                b.status = 'completed'
            
            # Foydalanuvchi reytingini oshirish
            main_b.user.completed_count += len(chain) 
            
            await query.edit_message_text(f"✅ @{main_b.user.nickname} ning {len(chain)} ta smenasi tasdiqlandi.")
            
            # GURUHGA HABAR YUBORISH (130-MAVZUGA)
            if GROUP_ID:
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    message_thread_id=INFO_TOPIC_ID, # 130-mavzu
                    text=f"✅ <b>HISOBOT TASDIQLANDI</b>\n━━━━━━━━━━━━━━\n👤 <b>Kim:</b> @{main_b.user.nickname}\n📅 <b>Sana:</b> {main_b.date}\n✅ <b>Holat:</b> {len(chain)} ta smena qabul qilindi",
                    parse_mode=ParseMode.HTML
                )

            try: 
                await context.bot.send_message(
                    main_b.user_id, 
                    f"🎉 Tabriklaymiz! {main_b.date} kungi hisobotingiz tasdiqlandi!"
                )
            except: pass
            
        # --- RAD ETISH ---
        else:
            # Smenalarni rad etilgan holatga o'tkazamiz
            for b in chain: 
                b.status = 'rejected'
            
            # Jarima qo'shish
            main_b.user.warnings += 1 
            
            # Bloklash holatini tekshirish
            if main_b.user.warnings >= 3:
                main_b.user.is_blocked = True

            # Sababini so'rash uchun foydalanuvchi ID sini saqlaymiz
            context.user_data['waiting_rej_reason_for'] = main_b.user_id
            
            await query.edit_message_text(f"❌ @{main_b.user.nickname} rad etildi. Sababini yozing:")

    await query.answer("Amal bajarildi")

async def check_pending_reports(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    with get_db_session() as session:
        active = session.query(Booking).options(joinedload(Booking.user)).filter(Booking.status=='active', Booking.date==today_str).all()
        for b in active:
            last_slot_end = b.slot_time.split('-')[1]
            
            # 24:00 muammosini tuzatish
            if last_slot_end == "24:00":
                end_time = datetime.strptime(f"{today_str} 23:59", "%Y-%m-%d %H:%M") + timedelta(minutes=1)
            else:
                end_time = datetime.strptime(f"{today_str} {last_slot_end}", "%Y-%m-%d %H:%M")
            
            # Smena tugaganidan keyin 1 soat o'tgach tekshirish
            if now > end_time + timedelta(hours=1):
                b.status = 'missed'
                b.user.warnings += 1
                if b.user.warnings >= 3: b.user.is_blocked = True
                
                # Guruhga (Infolar mavzusiga) xabar yuborish
                if GROUP_ID:
                    await context.bot.send_message(
                        chat_id=GROUP_ID,
                        message_thread_id=INFO_TOPIC_ID, # <-- QO'SHILDI: 130-mavzuga yuboradi
                        text=f"⏰ Hisobot yo'q: @{b.user.nickname} ({b.user.warnings}/3)"
                    )
                
                # Foydalanuvchining o'ziga (lichkasiga) xabar yuborish
                try: 
                    await context.bot.send_message(
                        chat_id=b.user_id, 
                        text=f"⚠️ Smenangiz uchun hisobot bermadingiz. Jarima: {b.user.warnings}/3"
                    )
                except: 
                    pass

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Faqat admin yoza olishini va rad etish kutilyotganini tekshirish
    if update.effective_user.id != ADMIN_ID or 'waiting_rej_reason_for' not in context.user_data: 
        return
        
    target_id = context.user_data.pop('waiting_rej_reason_for')
    reason = update.message.text # Admin yozgan sabab
    
    with get_db_session() as session:
        user = session.query(User).get(target_id)
        if not user:
            return

        # Agar jarimalar 3 taga yetsa, foydalanuvchini bloklaymiz
        if user.warnings >= 3: 
            user.is_blocked = True
            
        # 1. GURUHGA (130-mavzuga) xabar yuborish
        if GROUP_ID:
            await context.bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=INFO_TOPIC_ID, # 130-mavzu
                text=(
                    f"❌ <b>HISOBOT RAD ETILDI</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"👤 <b>Kim:</b> @{user.nickname}\n"
                    f"⚠️ <b>Sabab:</b> {reason}\n"
                    f"🚫 <b>Jarima:</b> {user.warnings}/3"
                ),
                parse_mode=ParseMode.HTML
            )
            
    # 2. FOYDALANUVCHINING O'ZIGA xabar yuborish
    try:
        status_msg = f"❌ Hisobotingiz rad etildi!\n⚠️ Sabab: {reason}\nJarima: {user.warnings}/3"
        if user.is_blocked:
            status_msg += "\n\n🚫 Siz 3 ta jarima sababli bloklandingiz!"
            
        await context.bot.send_message(target_id, status_msg)
        await update.message.reply_text("✅ Sabab foydalanuvchiga va guruhga yuborildi.")
    except Exception as e:
        logger.error(f"Error sending rejection message: {e}")

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db_session() as session:
        stats = session.query(Booking.slot_time, func.count(Booking.id)).filter(Booking.status=='completed').group_by(Booking.slot_time).all()
        msg = "📊 **Muvaffaqiyatli smenalar:**\n\n"
        for slot, count in stats: msg += f"<code>{slot}</code> {'🟩'*min(count,5)} ({count})\n"
        await update.message.reply_text(msg or "Hozircha yo'q", parse_mode=ParseMode.HTML)

async def show_my_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db_session() as session:
        u = session.query(User).get(update.effective_user.id)
        msg = f"👤 Ism: {u.full_name}\n🎖 Daraja: {u.rank}\n⚠️ Jarimalar: {u.warnings}/3\n📊 Jami: {u.completed_count}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db_session() as session:
        users = session.query(User).filter(User.completed_count>0).order_by(User.completed_count.desc()).limit(10).all()
        msg = "🏆 **TOP 10 ATLETLAR**\n"
        for i, u in enumerate(users, 1): msg += f"{i}. <b>{u.nickname}</b> — {u.completed_count}\n"
    await update.message.reply_text(msg or "Ro'yxat bo'sh", parse_mode=ParseMode.HTML)

async def list_cheaters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db_session() as session:
        cheaters = session.query(User).filter(User.warnings>0).all()
        msg = "🚫 **CHITERLAR**\n"
        for u in cheaters: msg += f"• @{u.nickname} — {u.warnings}/3 ({'💀 BLOK' if u.is_blocked else '⚠️ FAOL'})\n"
    await update.message.reply_text(msg or "Zal toza!", parse_mode=ParseMode.HTML)

async def list_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Barcha foydalanuvchilar ro'yxatini tugmalar bilan chiqarish"""
    if update.effective_user.id != ADMIN_ID:
        return

    with get_db_session() as session:
        users = session.query(User).all()
        
        if not users:
            await update.message.reply_text("Botda hali foydalanuvchilar yo'q.")
            return

        await update.message.reply_text("👥 <b>Barcha foydalanuvchilar boshqaruvi:</b>", parse_mode=ParseMode.HTML)

        for u in users:
            status = "🚫 Bloklangan" if u.is_blocked else "✅ Faol"
            user_link = get_mention(u.tg_id, f"@{u.nickname}")
            
            # Har bir foydalanuvchi uchun alohida xabar va unga biriktirilgan tugmalar
            text = (
                f"👤 <b>Foydalanuvchi:</b> {user_link}\n"
                f"🆔 <b>ID:</b> <code>{u.tg_id}</code>\n"
                f"📝 <b>Ism:</b> {u.full_name}\n"
                f"📊 <b>Smenalar:</b> {u.completed_count} ta\n"
                f"⚠️ <b>Jarimalar:</b> {u.warnings}/3\n"
                f"📌 <b>Holati:</b> {status}"
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("✏️ Tahrirlash", callback_data=f"edit_u_{u.tg_id}"),
                    InlineKeyboardButton("🗑 O'chirish", callback_data=f"del_u_{u.tg_id}")
                ]
            ]
            
            # Agar foydalanuvchi bloklangan bo'lsa, blokdan chiqarish tugmasini ham qo'shamiz
            if u.is_blocked:
                keyboard.append([InlineKeyboardButton("🔓 Blokdan chiqarish", callback_data=f"unblock_{u.tg_id}")])

            await update.message.reply_text(
                text, 
                reply_markup=InlineKeyboardMarkup(keyboard), 
                parse_mode=ParseMode.HTML
            )
            
# --- ADMIN FUNKSIYALARI: BLOKDAN CHIQARISH ---

# 1. O'chirish funksiyasi
async def delete_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data_parts = query.data.split("_")
    user_id_to_del = int(data_parts[2])
    
    # Agar bu shunchaki tasdiqlash so'rovi bo'lsa
    if len(data_parts) == 3:
        keyboard = [
            [
                InlineKeyboardButton("✅ Ha, o'chirilsin", callback_data=f"{query.data}_confirm"),
                InlineKeyboardButton("❌ Yo'q, bekor qilish", callback_data="cancel_edit")
            ]
        ]
        await query.edit_message_text("❓ Haqiqatan ham ushbu foydalanuvchini va uning barcha ma'lumotlarini o'chirmoqchimisiz?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Agar tasdiqlangan bo'lsa (confirm qismi bo'lsa)
    with get_db_session() as session:
        user = session.get(User, user_id_to_del)
        if user:
            session.query(Booking).filter_by(user_id=user_id_to_del).delete()
            session.delete(user)
            await query.answer("Foydalanuvchi butunlay o'chirildi")
            await query.edit_message_text(f"🗑 ID: {user_id_to_del} tizimdan muvaffaqiyatli o'chirildi.")
# 2. Tahrirlashni boshlash (Maydonni tanlash)
async def edit_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = int(query.data.split("_")[2])
    context.user_data['editing_user_id'] = user_id
    
    keyboard = [
        [InlineKeyboardButton("Ism", callback_data="field_full_name"),
         InlineKeyboardButton("Nickname", callback_data="field_nickname")],
        [InlineKeyboardButton("Jins", callback_data="field_gender")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_edit")]
    ]
    await query.edit_message_text("Nimani tahrirlaymiz?", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_USER_FIELD

# 3. Yangi qiymatni kutish
async def edit_user_field_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    field = query.data.replace("field_", "")
    
    if field == "cancel_edit":
        await query.edit_message_text("Tahrirlash bekor qilindi.")
        return ConversationHandler.END
        
    context.user_data['editing_field'] = field
    await query.edit_message_text(f"Yangi qiymatni yuboring ({field}):")
    return EDIT_USER_VALUE

# 4. Bazaga saqlash
async def edit_user_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_value = update.message.text.strip()
    user_id = context.user_data.get('editing_user_id')
    field = context.user_data.get('editing_field')
    
    with get_db_session() as session:
        user = session.get(User, user_id)
        if not user:
            await update.message.reply_text("Foydalanuvchi topilmadi.")
            return ConversationHandler.END

        # Agar nickname tahrirlanayotgan bo'lsa, unique-likni tekshiramiz
        if field == "nickname":
            new_value = new_value.replace("@", "").lower()
            existing = session.query(User).filter(User.nickname == new_value, User.tg_id != user_id).first()
            if existing:
                await update.message.reply_text("❌ Bu nickname band! Boshqa qiymat yuboring:")
                return EDIT_USER_VALUE

        # Ma'lumotni yangilash
        setattr(user, field, new_value)
        await update.message.reply_text(
            f"✅ Muvaffaqiyatli yangilandi!\n<b>{field}:</b> {new_value}", 
            reply_markup=get_main_menu(ADMIN_ID),
            parse_mode=ParseMode.HTML
        )
        
        # Foydalanuvchini xabardor qilish
        try:
            await context.bot.send_message(user_id, f"⚠️ Ma'lumotlaringiz admin tomonidan yangilandi:\n<b>{field}:</b> {new_value}", parse_mode=ParseMode.HTML)
        except: pass
            
    return ConversationHandler.END

async def manage_blocked_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db_session() as session:
        blocked_users = session.query(User).filter(User.is_blocked == True).all()
        if not blocked_users:
            await update.message.reply_text("Hozirda bloklangan foydalanuvchilar yo'q. ✅")
            return
        keyboard = []
        for u in blocked_users:
            keyboard.append([InlineKeyboardButton(f"🔓 {u.nickname} (ID: {u.tg_id})", callback_data=f"unblock_{u.tg_id}")])
        await update.message.reply_text("Blokdan chiqarish uchun tanlang:", reply_markup=InlineKeyboardMarkup(keyboard))

# SIZ SO'RAGAN FUNKSIYA SHU YERDA:
async def unblock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Ruxsat yo'q!"); return

    target_id = int(query.data.split("_")[1])

    with get_db_session() as session:
        user = session.get(User, target_id)
        if user:
            user.is_blocked = False
            user.warnings = 0
            
            await query.answer(f"@{user.nickname} blokdan chiqarildi!", show_alert=True)
            await query.edit_message_text(f"✅ @{user.nickname} muvaffaqiyatli blokdan chiqarildi.")
            
            try:
                await context.bot.send_message(target_id, "🎉 Admin sizni blokdan chiqardi. Endi botdan foydalanishingiz mumkin!")
            except: pass
        else:
            await query.answer("Foydalanuvchi topilmadi.")

# --- END OF ADMIN FUNCTIONS ---

async def weekly_winner(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_ID: 
        return
    
    with get_db_session() as session:
        # Eng ko'p smena bajargan foydalanuvchini olamiz
        top = session.query(User).order_by(User.completed_count.desc()).first()
        
        if top and top.completed_count > 0:
            win_text = (
                f"🏆 <b>HAFTALIK G'OLIB</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"👑 <b>G'olib:</b> @{top.nickname}\n"
                f"📊 <b>Natija:</b> {top.completed_count} ta smena\n"
                f"🏅 <b>Daraja:</b> {top.rank}\n"
                f"━━━━━━━━━━━━━━\n"
                f"Tabriklaymiz! Shunday davom eting! 🔥"
            )
            
            await context.bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=INFO_TOPIC_ID, # <-- 130-mavzuga yuboradi
                text=win_text,
                parse_mode=ParseMode.HTML
            )

async def reg_name(update, context):
    context.user_data['full_name'] = update.message.text
    await update.message.reply_text("Jinsingizni tanlang:", reply_markup=get_gender_keyboard())
    return REG_GENDER

async def reg_gender(update, context):
    context.user_data['gender'] = "Erkak" if "Erkak" in update.message.text else "Ayol"
    await update.message.reply_text("Login (nickname) yuboring:")
    return REG_NICK

async def reg_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nick = update.message.text.lower().strip().replace("@", "")
    
    with get_db_session() as session:
        # Bazada bunday nick bor-yo'qligini tekshirish
        existing_user = session.query(User).filter(User.nickname == nick).first()
        if existing_user:
            await update.message.reply_text("❌ Bu login band. Iltimos, boshqa login yuboring:")
            return REG_NICK
            
    context.user_data['temp_nick'] = nick
    await update.message.reply_text(f"Tasdiqlash uchun <b>{nick}</b> ni qayta yozing:", parse_mode=ParseMode.HTML)
    return CONFIRM_NICK

async def reg_confirm(update, context):
    if update.message.text.lower().strip() == context.user_data['temp_nick']:
        with get_db_session() as session:
            if not session.query(User).get(update.effective_user.id):
                session.add(User(tg_id=update.effective_user.id, full_name=context.user_data['full_name'], nickname=context.user_data['temp_nick'], gender=context.user_data['gender']))
        await update.message.reply_text("✅ Ro'yxatdan o'tdingiz!", reply_markup=get_main_menu(update.effective_user.id))
        return ConversationHandler.END
    await update.message.reply_text("Mos kelmadi. Qayta yuboring:"); return CONFIRM_NICK


def main():
    keep_alive()
    
    app = Application.builder().token(TOKEN).build()
    
    # 1. Avtomatik vazifalar (Job Queue)
    if app.job_queue:
        app.job_queue.run_daily(weekly_winner, time=datetime.strptime("21:00", "%H:%M").time(), days=(6,))
        app.job_queue.run_repeating(check_pending_reports, interval=900, first=10)

    # 2. ADMIN UCHUN TAHRIRLASH (ConversationHandler)
    # Bu handler ro'yxatdan o'tish handleridan alohida bo'lishi kerak
    admin_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_user_start, pattern="^edit_u_")],
        states={
            EDIT_USER_FIELD: [CallbackQueryHandler(edit_user_field_chosen, pattern="^field_|^cancel_edit")],
            EDIT_USER_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_user_save)],
        },
        fallbacks=[CallbackQueryHandler(lambda u, c: ConversationHandler.END, pattern="^cancel_edit")],
        allow_reentry=True
    )

    # 3. RO'YXATDAN O'TISH (Mavjud ConversationHandler)
    registration_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start), 
            MessageHandler(filters.Regex(r"📸 Smenani yakunlash"), finish_start)
        ],
        states={
            REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            REG_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_gender)],
            REG_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_nick)],
            CONFIRM_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_confirm)],
            UPLOAD_PHOTOS: [MessageHandler(filters.PHOTO, handle_photos)],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: u.message.reply_text(
                "Bekor qilindi.", 
                reply_markup=get_main_menu(u.effective_user.id)
            ))
        ],
        allow_reentry=True
    )

# HANDLERLARNI QO'SHISH (Tartib muhim!)
    app.add_handler(admin_edit_conv) # Yangi tahrirlash handleri
    app.add_handler(registration_conv)
    
    # O'chirish uchun oddiy CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(delete_user_callback, pattern="^del_u_"))

    # --- Qolgan barcha menyu handlerlari o'zgarishsiz qoladi ---
    app.add_handler(MessageHandler(filters.Regex(r"📅 Smenalar"), lambda u, c: u.message.reply_text("Kunni tanlang:", reply_markup=get_days_keyboard())))
    app.add_handler(MessageHandler(filters.Regex(r"👨‍✈️ Admin bilan bog'lanish"), contact_admin))
    app.add_handler(MessageHandler(filters.Regex(r"👥 Barcha foydalanuvchilar"), list_all_users))
    app.add_handler(MessageHandler(filters.Regex(r"🔐 Bloklanganlarni boshqarish"), manage_blocked_users))
    app.add_handler(CallbackQueryHandler(unblock_callback, pattern="^unblock_"))
    app.add_handler(MessageHandler(filters.Regex(r"📊 Statistika"), show_stats))
    app.add_handler(MessageHandler(filters.Regex(r"🏆 Reyting"), show_leaderboard))
    app.add_handler(MessageHandler(filters.Regex(r"🚫 Chiterlar"), list_cheaters))
    app.add_handler(MessageHandler(filters.Regex(r"❓ Zal holati"), gym_status))
    app.add_handler(MessageHandler(filters.Regex(r"⚠️ Jarimalarim"), show_my_warnings))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_reply))
    app.add_handler(CallbackQueryHandler(day_callback, pattern="^day_"))
    app.add_handler(CallbackQueryHandler(slot_callback, pattern="^slot_|^join_|^cancel_my_slot|^none$"))
    app.add_handler(CallbackQueryHandler(review_callback, pattern="^rev_"))
    
    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

# FUNKSIYADAN TASHQARIDA:
if __name__ == "__main__":
    main()