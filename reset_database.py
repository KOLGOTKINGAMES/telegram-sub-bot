# reset_database.py
import sqlite3
import os

def reset_database():
    # Удаляем старую базу
    if os.path.exists('subscriptions.db'):
        os.remove('subscriptions.db')
        print("🗑️ Старая база данных удалена")
    
    # Создаем новую базу без ограничений
    conn = sqlite3.connect('subscriptions.db')
    cursor = conn.cursor()
    
    # Пользователи
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            username TEXT,
            full_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Заявки - БЕЗ UNIQUE ограничения
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_username TEXT,
            channel_link TEXT,
            comment TEXT,
            status TEXT DEFAULT 'moderation',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            moderated_at TIMESTAMP,
            moderated_by INTEGER,
            last_request_time TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Сделки
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            acceptor_id INTEGER,
            acceptor_request_id INTEGER,
            status TEXT DEFAULT 'moderation',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            creator_subscribed BOOLEAN DEFAULT FALSE,
            acceptor_subscribed BOOLEAN DEFAULT FALSE,
            FOREIGN KEY (request_id) REFERENCES requests (id),
            FOREIGN KEY (acceptor_id) REFERENCES users (id),
            FOREIGN KEY (acceptor_request_id) REFERENCES requests (id)
        )
    ''')
    
    # Сообщения в чате сделки
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deal_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER,
            user_id INTEGER,
            message_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (deal_id) REFERENCES deals (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    conn.commit()
    conn.close()
    print("✅ Новая база данных создана без ограничений на каналы")
    print("📊 Структура базы:")
    print("   - users: таблица пользователей")
    print("   - requests: таблица заявок (каналы НЕ уникальны)")
    print("   - deals: таблица сделок") 
    print("   - deal_messages: таблица сообщений чата")

if __name__ == '__main__':
    reset_database()