# bot.py
import logging
import sqlite3
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, JobQueue
from telegram.ext import CallbackContext

from config import BOT_TOKEN, ADMIN_ID, COOLDOWN_MINUTES, SUBSCRIPTION_TIMEOUT, CHECK_SUBSCRIPTION_INTERVAL
from database import init_db, get_db_connection

# Обязательный канал для подписки
REQUIRED_CHANNEL = "@giftinfluencers"
SUPPORT_CONTACT = "@wakeupxddd"

# Увеличиваем время на сделку до 2 минут
SUBSCRIPTION_TIMEOUT = 120

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

class SubscriptionBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        self.setup_jobs()
    
    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("create_request", self.create_request))
        self.application.add_handler(CommandHandler("requests_list", self.requests_list))
        self.application.add_handler(CommandHandler("moderate", self.moderate_requests))
        self.application.add_handler(CommandHandler("delete_request", self.delete_request))
        self.application.add_handler(CommandHandler("my_requests", self.my_requests))
        self.application.add_handler(CommandHandler("my_deals", self.my_deals))
        
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_messages))
    
    def setup_jobs(self):
        job_queue = self.application.job_queue
        if job_queue:
            job_queue.run_repeating(self.check_subscriptions, interval=CHECK_SUBSCRIPTION_INTERVAL, first=10)
            job_queue.run_repeating(self.check_deal_timeouts, interval=30, first=15)
    
    async def check_required_subscription(self, user_id: int) -> bool:
        try:
            chat_member = await self.application.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
            return chat_member.status in ['member', 'administrator', 'creator']
        except Exception as e:
            print(f"Ошибка проверки подписки: {e}")
            return False
    
    async def require_subscription(self, update: Update, context: CallbackContext):
        keyboard = [
            [InlineKeyboardButton("📢 Подписаться на канал", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")],
            [InlineKeyboardButton("✅ Я подписался", callback_data="check_subscription")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            "📢 Для использования бота необходимо подписаться на наш канал!\n\n"
            f"Канал: {REQUIRED_CHANNEL}\n\n"
            "После подписки нажмите кнопку '✅ Я подписался'"
        )
        
        if update.message:
            await update.message.reply_text(message_text, reply_markup=reply_markup)
        else:
            await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)
    
    async def check_subscriptions(self, context: CallbackContext):
        if not self.application.job_queue:
            return
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT d.id, d.request_id, d.acceptor_id, d.acceptor_request_id,
                       r1.channel_username as creator_channel,
                       r2.channel_username as acceptor_channel,
                       u1.telegram_id as creator_tg_id,
                       u2.telegram_id as acceptor_tg_id
                FROM deals d
                JOIN requests r1 ON d.request_id = r1.id
                JOIN requests r2 ON d.acceptor_request_id = r2.id
                JOIN users u1 ON r1.user_id = u1.id
                JOIN users u2 ON r2.user_id = u2.id
                WHERE d.status = 'active' 
                AND (d.creator_subscribed = FALSE OR d.acceptor_subscribed = FALSE)
            ''')
            
            active_deals = cursor.fetchall()
            
            for deal in active_deals:
                (deal_id, request_id, acceptor_id, acceptor_request_id, 
                 creator_channel, acceptor_channel, creator_tg_id, acceptor_tg_id) = deal
                
                creator_subscribed = await self.check_user_subscription(creator_tg_id, acceptor_channel)
                acceptor_subscribed = await self.check_user_subscription(acceptor_tg_id, creator_channel)
                
                if creator_subscribed and acceptor_subscribed:
                    await self.complete_deal(deal_id)
                else:
                    conn_update = get_db_connection()
                    cursor_update = conn_update.cursor()
                    cursor_update.execute('''
                        UPDATE deals 
                        SET creator_subscribed = ?, acceptor_subscribed = ?
                        WHERE id = ?
                    ''', (creator_subscribed, acceptor_subscribed, deal_id))
                    conn_update.commit()
                    conn_update.close()
        finally:
            conn.close()
    
    async def check_deal_timeouts(self, context: CallbackContext):
        if not self.application.job_queue:
            return
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT d.id, d.request_id, d.acceptor_id, d.acceptor_request_id,
                       u1.telegram_id as creator_tg_id,
                       u2.telegram_id as acceptor_tg_id,
                       r1.channel_username as creator_channel,
                       r2.channel_username as acceptor_channel
                FROM deals d
                JOIN requests r1 ON d.request_id = r1.id
                JOIN requests r2 ON d.acceptor_request_id = r2.id
                JOIN users u1 ON r1.user_id = u1.id
                JOIN users u2 ON r2.user_id = u2.id
                WHERE d.status = 'active' AND d.expires_at < ?
            ''', (datetime.now(),))
            
            expired_deals = cursor.fetchall()
            
            for deal in expired_deals:
                (deal_id, request_id, acceptor_id, acceptor_request_id,
                 creator_tg_id, acceptor_tg_id, creator_channel, acceptor_channel) = deal
                
                cursor.execute('UPDATE deals SET status = "expired" WHERE id = ?', (deal_id,))
                
                timeout_message = f"❌ Время на взаимную подписку истекло! Сделка #{deal_id} отменена."
                await self.send_message_safe(creator_tg_id, timeout_message)
                await self.send_message_safe(acceptor_tg_id, timeout_message)
            
            conn.commit()
        finally:
            conn.close()
    
    async def check_user_subscription(self, user_id: int, channel_username: str) -> bool:
        try:
            chat_member = await self.application.bot.get_chat_member(f"@{channel_username}", user_id)
            return chat_member.status in ['member', 'administrator', 'creator']
        except Exception as e:
            print(f"Ошибка проверки подписки: {e}")
            return False
    
    async def validate_channel(self, channel_username: str) -> dict:
        try:
            chat = await self.application.bot.get_chat(f"@{channel_username}")
            bot_member = await self.application.bot.get_chat_member(chat.id, self.application.bot.id)
            
            return {
                'exists': True,
                'is_bot_admin': bot_member.status in ['administrator', 'creator'],
                'chat_type': chat.type,
                'title': chat.title
            }
        except Exception as e:
            return {'exists': False, 'is_bot_admin': False, 'error': str(e)}
    
    async def send_message_safe(self, chat_id: int, text: str, **kwargs):
        try:
            await self.application.bot.send_message(chat_id, text, **kwargs)
        except Exception as e:
            print(f"Ошибка отправки сообщения: {e}")
    
    def escape_markdown(self, text):
        if not text:
            return ""
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        return ''.join(['\\' + char if char in escape_chars else char for char in text])
    
    async def save_user(self, user):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO users (telegram_id, username, full_name)
                VALUES (?, ?, ?)
            ''', (user.id, user.username, user.full_name))
            conn.commit()
        finally:
            conn.close()
    
    async def get_user_id(self, telegram_id):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (telegram_id,))
            result = cursor.fetchone()
            return result[0] if result else None
        finally:
            conn.close()

    async def has_completed_deal_with_channel(self, user_id: int, channel_username: str) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT COUNT(*) 
                FROM deals d
                JOIN requests r ON d.request_id = r.id
                WHERE (r.user_id = ? OR d.acceptor_id = ?) 
                AND r.channel_username = ? 
                AND d.status = 'completed'
            ''', (user_id, user_id, channel_username))
            
            result = cursor.fetchone()
            return result[0] > 0 if result else False
        finally:
            conn.close()
    
    async def start(self, update: Update, context: CallbackContext):
        user = update.effective_user
        await self.save_user(user)
        
        if not await self.check_required_subscription(user.id):
            await self.require_subscription(update, context)
            return
        
        keyboard = [
            [InlineKeyboardButton("📋 Создать заявку", callback_data="create_request")],
            [InlineKeyboardButton("📊 Список заявок", callback_data="requests_list")],
            [InlineKeyboardButton("📋 Мои заявки", callback_data="my_requests")],
            [InlineKeyboardButton("🔄 Мои активные подписки", callback_data="my_deals")],
            [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")]
        ]
        
        if user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("🛡️ Модерация", callback_data="moderate")])
            keyboard.append([InlineKeyboardButton("🗑️ Удалить заявку", callback_data="delete_request_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_text = (
            f"👋 Добро пожаловать, {user.first_name}!\n\n"
            "Это бот для взаимных подписок в Telegram.\n"
            "Вы можете создать заявку на взаимную подписку или выбрать из существующих заявок."
        )
        
        if update.message:
            await update.message.reply_text(welcome_text, reply_markup=reply_markup)
        else:
            await update.callback_query.edit_message_text(welcome_text, reply_markup=reply_markup)
    
    async def my_requests(self, update: Update, context: CallbackContext):
        user = update.effective_user
        
        if not await self.check_required_subscription(user.id):
            await self.require_subscription(update, context)
            return
        
        user_id = await self.get_user_id(user.id)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT id, channel_username, comment, status, created_at
                FROM requests 
                WHERE user_id = ?
                ORDER BY created_at DESC
            ''', (user_id,))
            
            user_requests = cursor.fetchall()
            
            if not user_requests:
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                if update.message:
                    await update.message.reply_text("📭 У вас пока нет созданных заявок.", reply_markup=reply_markup)
                else:
                    await update.callback_query.edit_message_text("📭 У вас пока нет созданных заявок.", reply_markup=reply_markup)
                return
            
            message_text = "📋 Ваши заявки:\n\n"
            
            for req in user_requests:
                req_id, channel, comment, status, created_at = req
                status_emoji = "✅" if status == 'approved' else "🛑" if status == 'moderation' else "❌"
                message_text += f"{status_emoji} #{req_id} @{channel} - {status}\n"
            
            keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if update.message:
                await update.message.reply_text(message_text, reply_markup=reply_markup)
            else:
                await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)
        finally:
            conn.close()
    
    async def my_deals(self, update: Update, context: CallbackContext):
        user = update.effective_user
        
        if not await self.check_required_subscription(user.id):
            await self.require_subscription(update, context)
            return
        
        user_id = await self.get_user_id(user.id)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT d.id, d.status, d.created_at, d.expires_at,
                       r1.channel_username as creator_channel,
                       r2.channel_username as acceptor_channel,
                       u1.username as creator_username,
                       u2.username as acceptor_username
                FROM deals d
                JOIN requests r1 ON d.request_id = r1.id
                JOIN requests r2 ON d.acceptor_request_id = r2.id
                JOIN users u1 ON r1.user_id = u1.id
                JOIN users u2 ON r2.user_id = u2.id
                WHERE (r1.user_id = ? OR r2.user_id = ?) AND d.status = 'active'
                ORDER BY d.created_at DESC
            ''', (user_id, user_id))
            
            user_deals = cursor.fetchall()
            
            if not user_deals:
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                if update.message:
                    await update.message.reply_text("📭 У вас пока нет активных взаимных подписок.", reply_markup=reply_markup)
                else:
                    await update.callback_query.edit_message_text("📭 У вас пока нет активных взаимных подписок.", reply_markup=reply_markup)
                return
            
            message_text = "🔄 Ваши активные взаимные подписки:\n\n"
            
            for deal in user_deals:
                (deal_id, status, created_at, expires_at, 
                 creator_channel, acceptor_channel, creator_username, acceptor_username) = deal
                
                # ФИКС: Преобразуем expires_at в datetime если это строка
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at)
                
                if creator_username == user.username:
                    role = "Создатель"
                    your_channel = creator_channel
                    partner_channel = acceptor_channel
                else:
                    role = "Акцептор"
                    your_channel = acceptor_channel
                    partner_channel = creator_channel
                
                time_left = (expires_at - datetime.now()).seconds
                minutes = time_left // 60
                seconds = time_left % 60
                
                message_text += (
                    f"🔹 Сделка #{deal_id}\n"
                    f"   Ваш канал: @{your_channel}\n"
                    f"   Канал партнера: @{partner_channel}\n"
                    f"   Роль: {role}\n"
                    f"   Осталось времени: {minutes}:{seconds:02d}\n\n"
                )
            
            # Кнопка для возврата к сделке
            keyboard = [
                [InlineKeyboardButton("💬 Написать сообщение", callback_data=f"send_message_{user_deals[0][0]}")],
                [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if update.message:
                await update.message.reply_text(message_text, reply_markup=reply_markup)
            else:
                await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)
        finally:
            conn.close()
    
    async def create_request(self, update: Update, context: CallbackContext):
        user = update.effective_user
        
        if not await self.check_required_subscription(user.id):
            await self.require_subscription(update, context)
            return
        
        user_id = await self.get_user_id(user.id)
        
        # Проверка кулдауна
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT last_request_time FROM requests WHERE user_id = ? ORDER BY created_at DESC LIMIT 1', (user_id,))
            result = cursor.fetchone()
            
            if result and result[0]:
                last_time = datetime.fromisoformat(result[0])
                if datetime.now() - last_time < timedelta(minutes=COOLDOWN_MINUTES):
                    remaining = COOLDOWN_MINUTES - (datetime.now() - last_time).seconds // 60
                    response_text = f"⏰ Вы можете создать следующую заявку через {remaining} минут"
                    
                    keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    if update.message:
                        await update.message.reply_text(response_text, reply_markup=reply_markup)
                    else:
                        await update.callback_query.edit_message_text(response_text, reply_markup=reply_markup)
                    return
        finally:
            conn.close()
        
        context.user_data['creating_request'] = True
        
        response_text = (
            "📝 Создание новой заявки\n\n"
            "Отправьте username канала (например: @channel_name) или ссылку на канал\n\n"
            "⚠️ Убедитесь, что:\n"
            "1. Канал существует\n"
            "2. Бот добавлен в администраторы канала\n"
            "3. Бот имеет права на просмотр участников"
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.message:
            await update.message.reply_text(response_text, reply_markup=reply_markup)
        else:
            await update.callback_query.edit_message_text(response_text, reply_markup=reply_markup)
    
    async def parse_channel(self, text: str):
        if text.startswith('@'):
            return {'username': text[1:], 'link': f"https://t.me/{text[1:]}"}
        elif 't.me/' in text:
            username = text.split('t.me/')[-1].replace('/', '')
            return {'username': username, 'link': text}
        return None
    
    async def process_request_creation(self, update: Update, context: CallbackContext, message_text: str):
        user = update.effective_user
        
        if not await self.check_required_subscription(user.id):
            await self.require_subscription(update, context)
            return
        
        if 'channel_data' not in context.user_data:
            channel_data = await self.parse_channel(message_text)
            if not channel_data:
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("❌ Неверный формат канала. Используйте @username или ссылку", reply_markup=reply_markup)
                return
            
            validation = await self.validate_channel(channel_data['username'])
            
            if not validation['exists']:
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    f"❌ Канал @{channel_data['username']} не существует или недоступен!\n\n"
                    f"Проверьте правильность написания username канала.",
                    reply_markup=reply_markup
                )
                context.user_data.pop('creating_request', None)
                return
            
            if not validation['is_bot_admin']:
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    f"❌ Бот не является администратором канала @{channel_data['username']}!\n\n"
                    f"Добавьте бота в администраторы канала и предоставьте права на просмотр участников.",
                    reply_markup=reply_markup
                )
                context.user_data.pop('creating_request', None)
                return
            
            context.user_data['channel_data'] = channel_data
            
            keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ Канал @{channel_data['username']} проверен!\n\n"
                "Теперь добавьте комментарий к заявке (максимум 35 символов):",
                reply_markup=reply_markup
            )
        else:
            if len(message_text) > 35:
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("❌ Комментарий слишком длинный. Максимум 35 символов.", reply_markup=reply_markup)
                return
            
            user_id = await self.get_user_id(user.id)
            channel_data = context.user_data['channel_data']
            
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO requests (user_id, channel_username, channel_link, comment, last_request_time)
                    VALUES (?, ?, ?, ?, ?)
                ''', (user_id, channel_data['username'], channel_data['link'], message_text, datetime.now().isoformat()))
                
                request_id = cursor.lastrowid
                conn.commit()
                
                context.user_data.pop('creating_request', None)
                context.user_data.pop('channel_data', None)
                
                await self.notify_admin_about_new_request(request_id, user.username, channel_data['username'], message_text)
                
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    "✅ Заявка создана и отправлена на модерацию!\n"
                    "Вы получите уведомление, когда администратор проверит заявку.",
                    reply_markup=reply_markup
                )
            except Exception as e:
                print(f"Ошибка при создании заявки: {e}")
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "✅ Заявка создана и отправлена на модерацию!\n"
                    "Вы получите уведомление, когда администратор проверит заявку.",
                    reply_markup=reply_markup
                )
            finally:
                conn.close()
    
    async def notify_admin_about_new_request(self, request_id: int, username: str, channel: str, comment: str):
        keyboard = [
            [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_req_{request_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_req_{request_id}")],
            [InlineKeyboardButton("🗑️ Удалить", callback_data=f"delete_req_{request_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            f"🛑 Новая заявка на модерацию #{request_id}\n\n"
            f"Канал: @{channel}\n"
            f"Пользователь: @{username}\n"
            f"Комментарий: {comment}"
        )
        
        await self.application.bot.send_message(ADMIN_ID, message_text, reply_markup=reply_markup)
    
    async def requests_list(self, update: Update, context: CallbackContext):
        user = update.effective_user
        
        if not await self.check_required_subscription(user.id):
            await self.require_subscription(update, context)
            return
        
        if update.message:
            message = update.message
        else:
            message = update.callback_query.message
        
        page = context.user_data.get('requests_page', 0)
        await self.show_requests_page(message, page, context, user.id)
    
    async def show_requests_page(self, message, page: int, context: CallbackContext, user_id: int):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            current_user_id = await self.get_user_id(user_id)
            
            cursor.execute('''
                SELECT r.id, r.channel_username, r.comment, u.username 
                FROM requests r 
                JOIN users u ON r.user_id = u.id 
                WHERE r.status = 'approved'
                AND r.id NOT IN (
                    SELECT d.request_id 
                    FROM deals d 
                    WHERE (d.acceptor_id = ? OR r.user_id = ?) 
                    AND d.status = 'completed'
                )
                ORDER BY r.created_at DESC
            ''', (current_user_id, current_user_id))
            
            all_requests = cursor.fetchall()
            
            if not all_requests:
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                if hasattr(message, 'edit_message_text'):
                    await message.edit_message_text("📭 Пока нет доступных заявок для вас.", reply_markup=reply_markup)
                else:
                    await message.reply_text("📭 Пока нет доступных заявок для вас.", reply_markup=reply_markup)
                return
            
            requests_per_page = 5
            total_pages = (len(all_requests) + requests_per_page - 1) // requests_per_page
            start_idx = page * requests_per_page
            end_idx = start_idx + requests_per_page
            page_requests = all_requests[start_idx:end_idx]
            
            message_text = f"📊 Список заявок (Страница {page + 1}/{total_pages})\n\n"
            message_text += "💡 Заявки, с которыми у вас уже были завершенные сделки, скрыты\n\n"
            
            for i, request in enumerate(page_requests, start=1):
                req_id, channel, comment, creator = request
                safe_comment = self.escape_markdown(comment) if comment else ""
                message_text += (
                    f"🔹 Заявка #{req_id}\n"
                    f"   Канал: @{channel}\n"
                    f"   Автор: @{creator}\n"
                    f"   Комментарий: {safe_comment}\n\n"
                )
            
            keyboard = []
            for request in page_requests:
                req_id, channel, comment, creator = request
                
                cursor.execute('SELECT user_id FROM requests WHERE id = ?', (req_id,))
                result = cursor.fetchone()
                creator_user_id = result[0] if result else None
                
                if creator_user_id == current_user_id:
                    button_text = f"📋 Моя заявка #{req_id} (@{channel})"
                    callback_data = f"my_request_{req_id}"
                else:
                    button_text = f"✅ Выбрать #{req_id} (@{channel})"
                    callback_data = f"select_req_{req_id}"
                
                keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
            
            nav_buttons = []
            if page > 0:
                nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{page-1}"))
            if page < total_pages - 1:
                nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"page_{page+1}"))
            
            if nav_buttons:
                keyboard.append(nav_buttons)
            
            keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if hasattr(message, 'edit_message_text'):
                await message.edit_message_text(message_text, reply_markup=reply_markup)
            else:
                await message.reply_text(message_text, reply_markup=reply_markup)
        
        finally:
            conn.close()
    
    async def help_command(self, update: Update, context: CallbackContext):
        user = update.effective_user
        
        if not await self.check_required_subscription(user.id):
            await self.require_subscription(update, context)
            return
        
        help_text = (
            "ℹ️ Помощь по боту\n\n"
            "📋 Создать заявку - разместите свой канал для взаимных подписок\n"
            "📊 Список заявок - просмотр доступных каналов для подписки\n"
            "📋 Мои заявки - просмотр ваших созданных заявок\n"
            "🔄 Мои активные подписки - просмотр текущих сделок\n"
            "🔄 Взаимная подписка - вы подписываетесь на канал, а владелец подписывается на ваш\n\n"
            f"📢 Обязательная подписка: {REQUIRED_CHANNEL}\n"
            f"💬 Поддержка: {SUPPORT_CONTACT}\n\n"
            "⏰ Кулдаун: 20 минут между заявками\n"
            "⏱️ Время на подписку: 2 минуты\n\n"
            "❓ Проблемы? Свяжитесь с поддержкой."
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.message:
            await update.message.reply_text(help_text, reply_markup=reply_markup)
        else:
            await update.callback_query.edit_message_text(help_text, reply_markup=reply_markup)
    
    async def moderate_requests(self, update: Update, context: CallbackContext):
        user = update.effective_user
        
        if user.id != ADMIN_ID:
            if update.message:
                await update.message.reply_text("❌ У вас нет прав для модерации")
            else:
                await update.callback_query.answer("❌ У вас нет прав для модерации", show_alert=True)
            return
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT r.id, r.channel_username, r.comment, u.username, r.created_at
                FROM requests r 
                JOIN users u ON r.user_id = u.id 
                WHERE r.status = 'moderation'
                ORDER BY r.created_at
            ''')
            
            pending_requests = cursor.fetchall()
            
            if not pending_requests:
                if update.message:
                    await update.message.reply_text("✅ Нет заявок для модерации")
                else:
                    await update.callback_query.edit_message_text("✅ Нет заявок для модерации")
                return
            
            message_text = "🛑 Заявки на модерацию:\n\n"
            
            for req in pending_requests:
                req_id, channel, comment, username, created_at = req
                safe_comment = self.escape_markdown(comment) if comment else "нет комментария"
                message_text += f"🔹 #{req_id} @{channel}\n👤 @{username}\n💬 {safe_comment}\n\n"
            
            if update.message:
                await update.message.reply_text(message_text)
            else:
                await update.callback_query.edit_message_text(message_text)
        
        finally:
            conn.close()
    
    async def delete_request(self, update: Update, context: CallbackContext):
        user = update.effective_user
        
        if user.id != ADMIN_ID:
            if update.message:
                await update.message.reply_text("❌ У вас нет прав для удаления заявок")
            else:
                await update.callback_query.answer("❌ У вас нет прав для удаления заявок", show_alert=True)
            return
        
        if context.args:
            try:
                request_id = int(context.args[0])
                await self.delete_request_by_id(update, request_id)
                return
            except ValueError:
                if update.message:
                    await update.message.reply_text("❌ Неверный формат ID заявки")
                return
        
        instruction_text = (
            "🗑️ Удаление заявки\n\n"
            "Использование: /delete_request <ID_заявки>\n"
            "Пример: /delete_request 5"
        )
        
        if update.message:
            await update.message.reply_text(instruction_text)
        else:
            await update.callback_query.edit_message_text(instruction_text)
    
    async def delete_request_by_id(self, update: Update, request_id: int):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('SELECT channel_username FROM requests WHERE id = ?', (request_id,))
            result = cursor.fetchone()
            
            if not result:
                if update.message:
                    await update.message.reply_text(f"❌ Заявка #{request_id} не найдена")
                return
            
            channel_username = result[0]
            
            cursor.execute('DELETE FROM requests WHERE id = ?', (request_id,))
            cursor.execute('DELETE FROM deals WHERE request_id = ? OR acceptor_request_id = ?', (request_id, request_id))
            
            conn.commit()
            
            if update.message:
                await update.message.reply_text(f"✅ Заявка #{request_id} (@{channel_username}) удалена")
        
        finally:
            conn.close()
    
    async def approve_request(self, query, context: CallbackContext, request_id: int):
        user = query.from_user
        
        if user.id != ADMIN_ID:
            await query.answer("❌ У вас нет прав для модерации", show_alert=True)
            return
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                UPDATE requests 
                SET status = 'approved', moderated_at = ?, moderated_by = ?
                WHERE id = ?
            ''', (datetime.now().isoformat(), user.id, request_id))
            
            cursor.execute('''
                SELECT u.telegram_id, r.channel_username 
                FROM requests r 
                JOIN users u ON r.user_id = u.id 
                WHERE r.id = ?
            ''', (request_id,))
            
            result = cursor.fetchone()
            conn.commit()
            
            if result:
                user_tg_id, channel_username = result
                await self.send_message_safe(
                    user_tg_id, 
                    f"✅ Ваша заявка для канала @{channel_username} одобрена!\n"
                    f"Теперь она видна в списке заявок для других пользователей."
                )
            
            await query.edit_message_text(f"✅ Заявка #{request_id} одобрена!")
        
        finally:
            conn.close()
    
    async def reject_request(self, query, context: CallbackContext, request_id: int):
        user = query.from_user
        
        if user.id != ADMIN_ID:
            await query.answer("❌ У вас нет прав для модерации", show_alert=True)
            return
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT u.telegram_id, r.channel_username 
                FROM requests r 
                JOIN users u ON r.user_id = u.id 
                WHERE r.id = ?
            ''', (request_id,))
            
            result = cursor.fetchone()
            
            if result:
                user_tg_id, channel_username = result
                await self.send_message_safe(
                    user_tg_id, 
                    f"❌ Ваша заявка для канала @{channel_username} отклонена модератором.\n"
                    f"Вы можете создать новую заявку через 20 минут."
                )
            
            cursor.execute('DELETE FROM requests WHERE id = ?', (request_id,))
            conn.commit()
            
            await query.edit_message_text(f"❌ Заявка #{request_id} отклонена и удалена")
        
        finally:
            conn.close()
    
    async def delete_request_callback(self, query, context: CallbackContext, request_id: int):
        user = query.from_user
        
        if user.id != ADMIN_ID:
            await query.answer("❌ У вас нет прав для удаления заявок", show_alert=True)
            return
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('SELECT channel_username FROM requests WHERE id = ?', (request_id,))
            result = cursor.fetchone()
            
            if not result:
                await query.answer("❌ Заявка не найдена", show_alert=True)
                return
            
            channel_username = result[0]
            
            cursor.execute('DELETE FROM requests WHERE id = ?', (request_id,))
            cursor.execute('DELETE FROM deals WHERE request_id = ? OR acceptor_request_id = ?', (request_id, request_id))
            
            conn.commit()
            
            await query.edit_message_text(f"✅ Заявка #{request_id} (@{channel_username}) удалена")
        
        finally:
            conn.close()
    
    async def button_handler(self, update: Update, context: CallbackContext):
        query = update.callback_query
        await query.answer()
        
        user = query.from_user
        
        if not await self.check_required_subscription(user.id):
            await self.require_subscription(update, context)
            return
        
        data = query.data
        
        if data.startswith('select_req_'):
            request_id = int(data.split('_')[2])
            await self.select_request(query, context, request_id)
        
        elif data.startswith('my_request_'):
            request_id = int(data.split('_')[2])
            await query.answer("📋 Это ваша заявка", show_alert=True)
        
        elif data.startswith('approve_req_'):
            request_id = int(data.split('_')[2])
            await self.approve_request(query, context, request_id)
        
        elif data.startswith('reject_req_'):
            request_id = int(data.split('_')[2])
            await self.reject_request(query, context, request_id)
        
        elif data.startswith('delete_req_'):
            request_id = int(data.split('_')[2])
            await self.delete_request_callback(query, context, request_id)
        
        elif data.startswith('approve_deal_'):
            deal_id = int(data.split('_')[2])
            await self.approve_deal(query, context, deal_id)
        
        elif data.startswith('reject_deal_'):
            deal_id = int(data.split('_')[2])
            await self.reject_deal(query, context, deal_id)
        
        elif data.startswith('confirm_sub_'):
            deal_id = int(data.split('_')[2])
            await self.confirm_subscription(query, context, deal_id)
        
        elif data.startswith('deal_chat_'):
            deal_id = int(data.split('_')[2])
            await self.start_deal_chat(query, context, deal_id)
        
        elif data.startswith('send_message_'):
            parts = data.split('_')
            deal_id = int(parts[2])
            await self.request_chat_message(query, context, deal_id)
        
        elif data.startswith('page_'):
            page = int(data.split('_')[1])
            context.user_data['requests_page'] = page
            await self.show_requests_page(query.message, page, context, user.id)
        
        elif data == 'create_request':
            await self.create_request(update, context)
        
        elif data == 'requests_list':
            await self.requests_list(update, context)
        
        elif data == 'my_requests':
            await self.my_requests(update, context)
        
        elif data == 'my_deals':
            await self.my_deals(update, context)
        
        elif data == 'moderate':
            await self.moderate_requests(update, context)
        
        elif data == 'delete_request_menu':
            instruction_text = (
                "🗑️ Удаление заявки\n\n"
                "Использование: /delete_request <ID_заявки>\n"
                "Пример: /delete_request 5\n\n"
                "Используйте команду в чате с ботом."
            )
            await query.edit_message_text(instruction_text)
        
        elif data == 'help':
            await self.help_command(update, context)
        
        elif data == 'main_menu':
            await self.start(update, context)
        
        elif data == 'check_subscription':
            if await self.check_required_subscription(user.id):
                await self.start(update, context)
            else:
                await query.answer("❌ Вы еще не подписались на канал!", show_alert=True)
    
    async def select_request(self, query, context: CallbackContext, request_id: int):
        user = query.from_user
        
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT user_id FROM requests WHERE id = ?', (request_id,))
            result = cursor.fetchone()
            
            if result and result[0] == await self.get_user_id(user.id):
                await query.answer("❌ Вы не можете выбрать свою собственную заявку", show_alert=True)
                return
        finally:
            conn.close()
        
        keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📝 Чтобы принять заявку, отправьте username своего канала (например: @my_channel)\n\n"
            "⚠️ Убедитесь, что:\n"
            "1. Канал существует\n"
            "2. Бот добавлен в администраторы канала\n"
            "3. Бот имеет права на просмотр участников\n\n"
            "Ваш канал также будет отправлен на модерацию.",
            reply_markup=reply_markup
        )
        context.user_data['accepting_request'] = request_id
        context.user_data['acceptor_user'] = user.id
    
    async def create_deal(self, request_id: int, acceptor_id: int, acceptor_channel: dict):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO requests (user_id, channel_username, channel_link, comment, status)
                VALUES (?, ?, ?, ?, ?)
            ''', (acceptor_id, acceptor_channel['username'], acceptor_channel['link'], 
                  "Канал для взаимной подписки", "moderation"))
            
            acceptor_request_id = cursor.lastrowid
            
            expires_at = datetime.now() + timedelta(seconds=SUBSCRIPTION_TIMEOUT)
            # ФИКС: Сохраняем как строку в ISO формате
            cursor.execute('''
                INSERT INTO deals (request_id, acceptor_id, acceptor_request_id, expires_at)
                VALUES (?, ?, ?, ?)
            ''', (request_id, acceptor_id, acceptor_request_id, expires_at.isoformat()))
            
            deal_id = cursor.lastrowid
            conn.commit()
            
            await self.notify_admin_about_acceptor_channel(
                acceptor_request_id, deal_id, acceptor_channel['username'], 
                request_id
            )
            
            return deal_id
        except Exception as e:
            print(f"Ошибка при создании сделки: {e}")
            conn.rollback()
            return None
        finally:
            conn.close()
    
    async def notify_admin_about_acceptor_channel(self, acceptor_request_id: int, deal_id: int, 
                                                acceptor_channel: str, request_id: int):
        keyboard = [
            [InlineKeyboardButton("✅ Одобрить сделку", callback_data=f"approve_deal_{deal_id}")],
            [InlineKeyboardButton("❌ Отклонить сделку", callback_data=f"reject_deal_{deal_id}")],
            [InlineKeyboardButton("🗑️ Удалить заявку", callback_data=f"delete_req_{acceptor_request_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            f"🔄 Новая сделка для модерации #{deal_id}\n\n"
            f"📢 Заявка: #{request_id}\n"
            f"👤 Канал акцептора: @{acceptor_channel}\n\n"
            f"После одобрения начнется взаимная подписка."
        )
        
        await self.application.bot.send_message(ADMIN_ID, message_text, reply_markup=reply_markup)
    
    async def approve_deal(self, query, context: CallbackContext, deal_id: int):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT d.request_id, d.acceptor_id, d.acceptor_request_id,
                       r1.channel_username as creator_channel,
                       r2.channel_username as acceptor_channel,
                       u1.telegram_id as creator_tg_id,
                       u2.telegram_id as acceptor_tg_id
                FROM deals d
                JOIN requests r1 ON d.request_id = r1.id
                JOIN requests r2 ON d.acceptor_request_id = r2.id
                JOIN users u1 ON r1.user_id = u1.id
                JOIN users u2 ON r2.user_id = u2.id
                WHERE d.id = ?
            ''', (deal_id,))
            
            result = cursor.fetchone()
            if not result:
                await query.answer("❌ Сделка не найдена", show_alert=True)
                return
            
            (request_id, acceptor_id, acceptor_request_id, creator_channel, 
             acceptor_channel, creator_tg_id, acceptor_tg_id) = result
            
            cursor.execute('UPDATE deals SET status = "active" WHERE id = ?', (deal_id,))
            
            conn.commit()
            
            await self.notify_deal_participants(deal_id, creator_tg_id, acceptor_tg_id, 
                                              creator_channel, acceptor_channel)
            
            keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(f"✅ Сделка #{deal_id} одобрена! Участники уведомлены.", reply_markup=reply_markup)
        
        finally:
            conn.close()
    
    async def notify_deal_participants(self, deal_id: int, creator_tg_id: int, acceptor_tg_id: int,
                                     creator_channel: str, acceptor_channel: str):
        # ФИКС: Упрощенная клавиатура без кнопки сообщений
        confirm_keyboard = [
            [InlineKeyboardButton("🔄 Мои активные подписки", callback_data="my_deals")],
            [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(confirm_keyboard)
        
        creator_message = (
            f"🔄 Взаимная подписка началась! (#{deal_id})\n\n"
            f"📢 Ваш канал: @{creator_channel}\n"
            f"👤 Канал партнера: @{acceptor_channel}\n\n"
            f"🤖 Бот автоматически проверит подписки\n"
            f"⏱️ У вас есть 2 минуты!\n\n"
            f"Действия:\n"
            f"1. Подпишитесь на @{acceptor_channel}\n"
            f"2. Партнер подпишется на @{creator_channel}\n"
            f"3. Сделка завершится автоматически\n\n"
            f"💬 Для общения используйте команду /my_deals"
        )
        
        acceptor_message = (
            f"🔄 Взаимная подписка началась! (#{deal_id})\n\n"
            f"📢 Ваш канал: @{acceptor_channel}\n"
            f"👤 Канал партнера: @{creator_channel}\n\n"
            f"🤖 Бот автоматически проверит подписки\n"
            f"⏱️ У вас есть 2 минуты!\n\n"
            f"Действия:\n"
            f"1. Подпишитесь на @{creator_channel}\n"
            f"2. Партнер подпишется на @{acceptor_channel}\n"
            f"3. Сделка завершится автоматически\n\n"
            f"💬 Для общения используйте команду /my_deals"
        )
        
        await self.application.bot.send_message(creator_tg_id, creator_message, reply_markup=reply_markup)
        await self.application.bot.send_message(acceptor_tg_id, acceptor_message, reply_markup=reply_markup)
    
    async def confirm_subscription(self, query, context: CallbackContext, deal_id: int):
        await query.answer("🤖 Бот автоматически проверяет подписки. Просто подпишитесь на канал партнера!", show_alert=True)
    
    async def complete_deal(self, deal_id: int):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('UPDATE deals SET status = "completed" WHERE id = ?', (deal_id,))
            
            cursor.execute('''
                SELECT u1.telegram_id, u2.telegram_id
                FROM deals d
                JOIN requests r1 ON d.request_id = r1.id
                JOIN requests r2 ON d.acceptor_request_id = r2.id
                JOIN users u1 ON r1.user_id = u1.id
                JOIN users u2 ON r2.user_id = u2.id
                WHERE d.id = ?
            ''', (deal_id,))
            
            result = cursor.fetchone()
            if result:
                creator_tg_id, acceptor_tg_id = result
                
                completion_message = (
                    "🎉 Сделка успешно завершена!\n\n"
                    "🤖 Бот проверил, что обе стороны выполнили взаимную подписку.\n"
                    "Спасибо за участие!"
                )
                
                await self.send_message_safe(creator_tg_id, completion_message)
                await self.send_message_safe(acceptor_tg_id, completion_message)
            
            conn.commit()
        finally:
            conn.close()
    
    async def reject_deal(self, query, context: CallbackContext, deal_id: int):
        user = query.from_user
        
        if user.id != ADMIN_ID:
            await query.answer("❌ У вас нет прав для модерации", show_alert=True)
            return
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT u1.telegram_id, u2.telegram_id, r1.channel_username, r2.channel_username
                FROM deals d
                JOIN requests r1 ON d.request_id = r1.id
                JOIN requests r2 ON d.acceptor_request_id = r2.id
                JOIN users u1 ON r1.user_id = u1.id
                JOIN users u2 ON r2.user_id = u2.id
                WHERE d.id = ?
            ''', (deal_id,))
            
            result = cursor.fetchone()
            
            if result:
                creator_tg_id, acceptor_tg_id, creator_channel, acceptor_channel = result
                
                rejection_message = (
                    f"❌ Сделка #{deal_id} отклонена модератором.\n\n"
                    f"Причина: канал не прошел проверку модерации."
                )
                
                await self.send_message_safe(creator_tg_id, rejection_message)
                await self.send_message_safe(acceptor_tg_id, rejection_message)
            
            cursor.execute('DELETE FROM deals WHERE id = ?', (deal_id,))
            cursor.execute('DELETE FROM requests WHERE id IN (SELECT acceptor_request_id FROM deals WHERE id = ?)', (deal_id,))
            
            conn.commit()
            
            await query.edit_message_text(f"❌ Сделка #{deal_id} отклонена и удалена")
        
        finally:
            conn.close()
    
    async def request_chat_message(self, query, context: CallbackContext, deal_id: int):
        user = query.from_user
        
        context.user_data['chatting_deal'] = deal_id
        context.user_data['chatting_user'] = await self.get_user_id(user.id)
        
        keyboard = [
            [InlineKeyboardButton("🔄 Мои активные подписки", callback_data="my_deals")],
            [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "💬 Введите сообщение для вашего партнера по сделке:\n\n"
            "После отправки сообщения вы вернетесь к просмотру сделки.",
            reply_markup=reply_markup
        )
    
    async def start_deal_chat(self, query, context: CallbackContext, deal_id: int):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT u.username, dm.message_text, dm.created_at
                FROM deal_messages dm
                JOIN users u ON dm.user_id = u.id
                WHERE dm.deal_id = ?
                ORDER BY dm.created_at
            ''', (deal_id,))
            
            messages = cursor.fetchall()
            
            if not messages:
                message_text = "💬 Чат сделки пуст\n\nОтправьте первое сообщение!"
            else:
                message_text = "💬 История чата:\n\n"
                for msg in messages:
                    username, text, created_at = msg
                    message_text += f"👤 @{username}: {text}\n"
            
            keyboard = [
                [InlineKeyboardButton("✉️ Написать сообщение", callback_data=f"send_message_{deal_id}")],
                [InlineKeyboardButton("🔄 Мои активные подписки", callback_data="my_deals")],
                [InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(message_text, reply_markup=reply_markup)
        
        finally:
            conn.close()
    
    async def send_chat_message(self, deal_id: int, user_id: int, message_text: str):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO deal_messages (deal_id, user_id, message_text)
                VALUES (?, ?, ?)
            ''', (deal_id, user_id, message_text))
            
            cursor.execute('''
                SELECT u1.telegram_id, u2.telegram_id, u1.username, u2.username
                FROM deals d
                JOIN requests r1 ON d.request_id = r1.id
                JOIN requests r2 ON d.acceptor_request_id = r2.id
                JOIN users u1 ON r1.user_id = u1.id
                JOIN users u2 ON r2.user_id = u2.id
                WHERE d.id = ?
            ''', (deal_id,))
            
            result = cursor.fetchone()
            conn.commit()
            
            if result:
                creator_tg_id, acceptor_tg_id, creator_username, acceptor_username = result
                
                cursor_user = get_db_connection().cursor()
                cursor_user.execute('SELECT telegram_id FROM users WHERE id = ?', (user_id,))
                sender_result = cursor_user.fetchone()
                cursor_user.connection.close()
                
                if sender_result and sender_result[0] == creator_tg_id:
                    partner_tg_id = acceptor_tg_id
                    partner_username = acceptor_username
                    sender_username = creator_username
                else:
                    partner_tg_id = creator_tg_id
                    partner_username = creator_username
                    sender_username = acceptor_username
                
                notification_text = (
                    f"💬 Новое сообщение в сделке #{deal_id}\n\n"
                    f"👤 От: @{sender_username}\n"
                    f"💭 Сообщение: {message_text}\n\n"
                    f"✉️ Ответить: используйте команду /my_deals"
                )
                
                await self.send_message_safe(partner_tg_id, notification_text)
        
        finally:
            conn.close()
    
    async def handle_messages(self, update: Update, context: CallbackContext):
        # ФИКС: Проверяем что сообщение существует
        if not update.message or not update.message.text:
            return
            
        user = update.effective_user
        message_text = update.message.text
        
        if not await self.check_required_subscription(user.id):
            await self.require_subscription(update, context)
            return
        
        if context.user_data.get('creating_request'):
            await self.process_request_creation(update, context, message_text)
            return
        
        if context.user_data.get('accepting_request'):
            request_id = context.user_data['accepting_request']
            acceptor_user_id = context.user_data.get('acceptor_user')
            channel_data = await self.parse_channel(message_text)
            
            if channel_data and acceptor_user_id:
                validation = await self.validate_channel(channel_data['username'])
                
                if not validation['exists']:
                    keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(
                        f"❌ Канал @{channel_data['username']} не существует или недоступен!\n\n"
                        f"Проверьте правильность написания username канала.",
                        reply_markup=reply_markup
                    )
                    return
                
                if not validation['is_bot_admin']:
                    keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(
                        f"❌ Бот не является администратором канала @{channel_data['username']}!\n\n"
                        f"Добавьте бота в администраторы канала и предоставьте права на просмотр участников.",
                        reply_markup=reply_markup
                    )
                    return
                
                user_id = await self.get_user_id(acceptor_user_id)
                
                deal_id = await self.create_deal(request_id, user_id, channel_data)
                
                if deal_id:
                    context.user_data.pop('accepting_request', None)
                    context.user_data.pop('acceptor_user', None)
                    
                    keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await update.message.reply_text(
                        "✅ Ваш канал принят и отправлен на модерацию!\n\n"
                        "После одобрения администратором начнется взаимная подписка.",
                        reply_markup=reply_markup
                    )
                else:
                    keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text("❌ Ошибка: произошла ошибка при создании сделки", reply_markup=reply_markup)
            else:
                keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("❌ Неверный формат канала. Используйте @username или ссылку", reply_markup=reply_markup)
        
        # Обработка сообщений чата - ФИКС: возвращаем к сделке
        if context.user_data.get('chatting_deal'):
            deal_id = context.user_data['chatting_deal']
            user_id = context.user_data.get('chatting_user')
            
            await self.send_chat_message(deal_id, user_id, message_text)
            
            # ФИКС: Возвращаем пользователя к просмотру сделки
            await update.message.reply_text(
                "✅ Сообщение отправлено! Возвращаем к сделке...",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Мои активные подписки", callback_data="my_deals")]])
            )
            
            context.user_data.pop('chatting_deal', None)
            context.user_data.pop('chatting_user', None)

# Запуск бота
def main():
    init_db()
    bot = SubscriptionBot()
    
    print("Бот запущен!")
    print(f"📢 Обязательная подписка: {REQUIRED_CHANNEL}")
    print(f"⏱️ Время на сделку: 2 минуты")
    
    # Запускаем бота в отдельном потоке
    import threading
    def run_bot():
        bot.application.run_polling()
    
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Запускаем Flask сервер
    from app import run_flask
    run_flask()

if __name__ == '__main__':
    main()


