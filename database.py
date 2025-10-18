# database.py
import sqlite3
from datetime import datetime

def init_db():
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
    
    # Заявки
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_username TEXT UNIQUE,
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
    
    # Сделки - ОБНОВЛЕННАЯ ВЕРСИЯ
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

def get_db_connection():
    return sqlite3.connect('subscriptions.db')