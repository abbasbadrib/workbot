#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات مدیریت مالی کارگاه نصب و تعمیرات کولرگازی و پکیج
نسخه Railway: 3.0.0
Single-file implementation
"""

import os
import sys
import re
import json
import sqlite3
import logging
import tempfile
import subprocess
import shutil
import time
import socket
import argparse
import asyncio
import urllib.request
import zipfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

# ============================
# تلگرام
# ============================
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, TelegramError

# ============================
# Vosk
# ============================
import wave
try:
    import vosk
    VOSK_AVAILABLE = True
except ImportError:
    VOSK_AVAILABLE = False

# ============================
# تنظیمات
# ============================

def get_config() -> Dict[str, Any]:
    base_dir = Path(__file__).parent.resolve()
    
    # روی Railway مسیر data برای Volume
    data_dir = Path("/app/data") if os.path.exists("/app") else base_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    
    return {
        "BOT_TOKEN": os.environ.get("BOT_TOKEN", ""),
        "DATABASE_PATH": os.environ.get("DATABASE_PATH", str(data_dir / "business.db")),
        "VOSK_MODEL_PATH": os.environ.get("VOSK_MODEL_PATH", str(base_dir / "vosk-model-small-fa-0.5")),
        "BACKUP_DIR": os.environ.get("BACKUP_DIR", str(data_dir / "backups")),
        "LOG_DIR": os.environ.get("LOG_DIR", str(data_dir / "logs")),
        "AUTHORIZED_USERS": os.environ.get("AUTHORIZED_USERS", ""),
        "MAX_BACKUPS": int(os.environ.get("MAX_BACKUPS", "7")),
        "MAX_RETRIES": int(os.environ.get("MAX_RETRIES", "5")),
        "RETRY_DELAY": int(os.environ.get("RETRY_DELAY", "3")),
    }

CONFIG = get_config()

# ============================
# لاگ
# ============================

def setup_logging():
    log_dir = Path(CONFIG["LOG_DIR"])
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bot.log"
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
        handlers=[
            logging.FileHandler(str(log_file), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# ============================
# ثابت‌های مالی
# ============================

PERSON_ME = "من"
PERSON_ALI = "علی"
PERSON_HOSEIN = "حسین"
PERSON_SHAGERD = "شاگرد"

ALL_PERSONS = [PERSON_ME, PERSON_ALI, PERSON_HOSEIN, PERSON_SHAGERD]
PARTNERS = [PERSON_ME, PERSON_ALI, PERSON_HOSEIN]

SHARE_HOSEIN = 0.35
SHARE_ME = 0.325
SHARE_ALI = 0.325

# ============================
# محاسبات مالی
# ============================

def calculate_daily_cost_shares(amount: int, consumers: List[str]) -> Dict[str, int]:
    """
    محاسبه سهم افراد در هزینه روزانه با قانون شاگرد.
    قانون: سهم شاگرد فقط بین شرکای حاضر در همان هزینه تقسیم می‌شود.
    """
    if amount <= 0 or not consumers:
        return {p: 0 for p in ALL_PERSONS}
    
    consumers = list(dict.fromkeys(consumers))
    partners_present = [c for c in consumers if c != PERSON_SHAGERD]
    has_shagerd = PERSON_SHAGERD in consumers
    
    if not partners_present:
        partners_present = [PERSON_ME, PERSON_ALI]
    
    total_consumers = len(consumers)
    base_share = amount // total_consumers
    shagerd_amount = base_share if has_shagerd else 0
    partners_total = amount - shagerd_amount
    
    n_partners = len(partners_present)
    partner_share = partners_total // n_partners
    partner_remainder = partners_total - (partner_share * n_partners)
    
    result = {p: 0 for p in ALL_PERSONS}
    for i, partner in enumerate(partners_present):
        if i == 0:
            result[partner] = partner_share + partner_remainder
        else:
            result[partner] = partner_share
    
    if has_shagerd:
        result[PERSON_SHAGERD] = 0
    
    return result


# ============================
# دیتابیس
# ============================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG["DATABASE_PATH"], timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def safe_create_index(conn, index_name: str, table: str, column: str):
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in cursor.fetchall()}
        if column not in columns:
            logger.warning(f"⚠️ ستون {table}.{column} وجود ندارد")
            return
        conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({column})")
    except sqlite3.OperationalError as e:
        logger.warning(f"⚠️ خطا در ساخت ایندکس {index_name}: {e}")


def migrate_projects_table(conn):
    cursor = conn.execute("PRAGMA table_info(projects)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    
    required_columns = {
        'phone': "TEXT",
        'location': "TEXT",
        'service_type': "TEXT",
        'device_type': "TEXT",
        'description': "TEXT",
        'total_income': "INTEGER NOT NULL DEFAULT 0",
        'project_cost': "INTEGER NOT NULL DEFAULT 0",
        'net_profit': "INTEGER NOT NULL DEFAULT 0",
        'hosein_share': "INTEGER NOT NULL DEFAULT 0",
        'my_share': "INTEGER NOT NULL DEFAULT 0",
        'ali_share': "INTEGER NOT NULL DEFAULT 0",
        'received_amount': "INTEGER NOT NULL DEFAULT 0",
        'remaining_amount': "INTEGER NOT NULL DEFAULT 0",
        'payment_status': "TEXT DEFAULT 'unpaid'",
        'status': "TEXT DEFAULT 'completed'",
        'project_date': "TEXT",
        'updated_at': "TIMESTAMP",
        'notes': "TEXT",
        'is_deleted': "INTEGER DEFAULT 0",
    }
    
    for col_name, col_type in required_columns.items():
        if col_name not in existing_columns:
            try:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col_name} {col_type}")
                logger.info(f"➕ ستون اضافه شد: projects.{col_name}")
            except sqlite3.OperationalError as e:
                logger.warning(f"⚠️ خطا در اضافه کردن {col_name}: {e}")
    
    try:
        conn.execute("""
            UPDATE projects 
            SET project_date = DATE(created_at) 
            WHERE project_date IS NULL AND created_at IS NOT NULL
        """)
    except Exception:
        pass


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                phone TEXT,
                location TEXT,
                service_type TEXT,
                device_type TEXT,
                description TEXT,
                total_income INTEGER NOT NULL DEFAULT 0,
                project_cost INTEGER NOT NULL DEFAULT 0,
                net_profit INTEGER NOT NULL DEFAULT 0,
                hosein_share INTEGER NOT NULL DEFAULT 0,
                my_share INTEGER NOT NULL DEFAULT 0,
                ali_share INTEGER NOT NULL DEFAULT 0,
                received_amount INTEGER NOT NULL DEFAULT 0,
                remaining_amount INTEGER NOT NULL DEFAULT 0,
                payment_status TEXT DEFAULT 'unpaid',
                status TEXT DEFAULT 'completed',
                project_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notes TEXT,
                is_deleted INTEGER DEFAULT 0
            )
        """)
        migrate_projects_table(conn)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                category TEXT,
                amount INTEGER NOT NULL,
                paid_by TEXT NOT NULL,
                cost_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER DEFAULT 0
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_cost_shares (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cost_id INTEGER NOT NULL,
                person TEXT NOT NULL,
                share_amount INTEGER NOT NULL DEFAULT 0,
                is_consumer INTEGER DEFAULT 1,
                FOREIGN KEY (cost_id) REFERENCES daily_costs(id) ON DELETE CASCADE
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_person TEXT NOT NULL,
                to_person TEXT NOT NULL,
                amount INTEGER NOT NULL,
                description TEXT,
                settlement_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER DEFAULT 0
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                phone TEXT,
                location TEXT,
                device_type TEXT,
                description TEXT,
                estimated_amount INTEGER DEFAULT 0,
                scheduled_date TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notes TEXT,
                is_deleted INTEGER DEFAULT 0
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                operation_type TEXT NOT NULL,
                table_name TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                snapshot TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        safe_create_index(conn, "idx_projects_date", "projects", "project_date")
        safe_create_index(conn, "idx_projects_customer", "projects", "customer_name")
        safe_create_index(conn, "idx_daily_costs_date", "daily_costs", "cost_date")
        safe_create_index(conn, "idx_daily_cost_shares_cost", "daily_cost_shares", "cost_id")
        safe_create_index(conn, "idx_settlements_date", "settlements", "settlement_date")
        safe_create_index(conn, "idx_pending_status", "pending_projects", "status")
        safe_create_index(conn, "idx_oplog_user", "operation_log", "user_id")
        
        conn.commit()
        logger.info("✅ دیتابیس راه‌اندازی شد")

# ===== توابع پروژه =====

def add_project(data: Dict, user_id: int = 0) -> int:
    total_income = int(data.get('total_income', 0))
    project_cost = int(data.get('project_cost', 0))
    net_profit = total_income - project_cost
    
    hosein_share = int(net_profit * SHARE_HOSEIN)
    my_share = int(net_profit * SHARE_ME)
    ali_share = net_profit - hosein_share - my_share
    
    received = int(data.get('received_amount', 0))
    remaining = total_income - received
    if received >= total_income:
        payment_status = 'paid'
    elif received > 0:
        payment_status = 'partial'
    else:
        payment_status = 'unpaid'
    
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO projects (
                customer_name, phone, location, service_type, device_type,
                description, total_income, project_cost, net_profit,
                hosein_share, my_share, ali_share,
                received_amount, remaining_amount, payment_status,
                status, project_date, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('customer_name', ''),
            data.get('phone', ''),
            data.get('location', ''),
            data.get('service_type', ''),
            data.get('device_type', ''),
            data.get('description', ''),
            total_income, project_cost, net_profit,
            hosein_share, my_share, ali_share,
            received, remaining, payment_status,
            data.get('status', 'completed'),
            data.get('project_date', datetime.now().strftime('%Y-%m-%d')),
            data.get('notes', '')
        ))
        project_id = cursor.lastrowid
        conn.execute("""
            INSERT INTO operation_log (user_id, operation_type, table_name, record_id, snapshot)
            VALUES (?, 'INSERT', 'projects', ?, ?)
        """, (user_id, project_id, json.dumps(data, ensure_ascii=False, default=str)))
        conn.commit()
        return project_id


def get_projects(month: Optional[str] = None, include_deleted: bool = False) -> List[Dict]:
    with get_db() as conn:
        query = "SELECT * FROM projects WHERE 1=1"
        params = []
        if not include_deleted:
            query += " AND is_deleted = 0"
        if month:
            query += " AND strftime('%Y-%m', project_date) = ?"
            params.append(month)
        query += " ORDER BY created_at DESC"
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_project_by_id(project_id: int) -> Optional[Dict]:
    with get_db() as conn:
        cursor = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def delete_project(project_id: int, user_id: int = 0) -> bool:
    with get_db() as conn:
        conn.execute("""
            INSERT INTO operation_log (user_id, operation_type, table_name, record_id)
            VALUES (?, 'DELETE', 'projects', ?)
        """, (user_id, project_id))
        conn.execute(
            "UPDATE projects SET is_deleted = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (project_id,)
        )
        conn.commit()
        return True


# ===== توابع هزینه روزانه =====

def add_daily_cost(data: Dict, shares: Dict[str, int], user_id: int = 0) -> int:
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO daily_costs (description, category, amount, paid_by, cost_date)
            VALUES (?, ?, ?, ?, ?)
        """, (
            data.get('description', ''),
            data.get('category', ''),
            int(data.get('amount', 0)),
            data.get('paid_by', PERSON_ME),
            data.get('cost_date', datetime.now().strftime('%Y-%m-%d'))
        ))
        cost_id = cursor.lastrowid
        
        for person, share in shares.items():
            conn.execute("""
                INSERT INTO daily_cost_shares (cost_id, person, share_amount, is_consumer)
                VALUES (?, ?, ?, ?)
            """, (cost_id, person, int(share), 1 if share > 0 else 0))
        
        conn.execute("""
            INSERT INTO operation_log (user_id, operation_type, table_name, record_id, snapshot)
            VALUES (?, 'INSERT', 'daily_costs', ?, ?)
        """, (user_id, cost_id, json.dumps(data, ensure_ascii=False, default=str)))
        
        conn.commit()
        return cost_id


def get_daily_costs(month: Optional[str] = None, include_deleted: bool = False) -> List[Dict]:
    with get_db() as conn:
        query = "SELECT * FROM daily_costs WHERE 1=1"
        params = []
        if not include_deleted:
            query += " AND is_deleted = 0"
        if month:
            query += " AND strftime('%Y-%m', cost_date) = ?"
            params.append(month)
        query += " ORDER BY created_at DESC"
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_daily_cost_shares(cost_id: int) -> List[Dict]:
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT * FROM daily_cost_shares WHERE cost_id = ?", (cost_id,)
        )
        return [dict(row) for row in cursor.fetchall()]


def delete_daily_cost(cost_id: int, user_id: int = 0) -> bool:
    with get_db() as conn:
        conn.execute("""
            INSERT INTO operation_log (user_id, operation_type, table_name, record_id)
            VALUES (?, 'DELETE', 'daily_costs', ?)
        """, (user_id, cost_id))
        conn.execute(
            "UPDATE daily_costs SET is_deleted = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (cost_id,)
        )
        conn.commit()
        return True


# ===== توابع تسویه =====

def add_settlement(data: Dict, user_id: int = 0) -> int:
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO settlements (from_person, to_person, amount, description, settlement_date)
            VALUES (?, ?, ?, ?, ?)
        """, (
            data.get('from_person', ''),
            data.get('to_person', ''),
            int(data.get('amount', 0)),
            data.get('description', ''),
            data.get('settlement_date', datetime.now().strftime('%Y-%m-%d'))
        ))
        settlement_id = cursor.lastrowid
        conn.execute("""
            INSERT INTO operation_log (user_id, operation_type, table_name, record_id, snapshot)
            VALUES (?, 'INSERT', 'settlements', ?, ?)
        """, (user_id, settlement_id, json.dumps(data, ensure_ascii=False, default=str)))
        conn.commit()
        return settlement_id


def get_settlements(month: Optional[str] = None) -> List[Dict]:
    with get_db() as conn:
        query = "SELECT * FROM settlements WHERE is_deleted = 0"
        params = []
        if month:
            query += " AND strftime('%Y-%m', settlement_date) = ?"
            params.append(month)
        query += " ORDER BY created_at DESC"
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def delete_settlement(settlement_id: int, user_id: int = 0) -> bool:
    with get_db() as conn:
        conn.execute("""
            INSERT INTO operation_log (user_id, operation_type, table_name, record_id)
            VALUES (?, 'DELETE', 'settlements', ?)
        """, (user_id, settlement_id))
        conn.execute("UPDATE settlements SET is_deleted = 1 WHERE id = ?", (settlement_id,))
        conn.commit()
        return True


def calculate_debts() -> Dict[str, Dict[str, int]]:
    """
    محاسبه بدهی خالص بین افراد.
    """
    debts = {p: {q: 0 for q in ALL_PERSONS if q != p} for p in ALL_PERSONS}
    
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT dcs.person, dcs.share_amount, dc.paid_by
            FROM daily_cost_shares dcs
            JOIN daily_costs dc ON dc.id = dcs.cost_id
            WHERE dc.is_deleted = 0 AND dcs.share_amount > 0
        """)
        for row in cursor.fetchall():
            person = row['person']
            paid_by = row['paid_by']
            amount = row['share_amount']
            if person != paid_by:
                debts[person][paid_by] += amount
        
        cursor = conn.execute("""
            SELECT from_person, to_person, amount
            FROM settlements WHERE is_deleted = 0
        """)
        for row in cursor.fetchall():
            from_p = row['from_person']
            to_p = row['to_person']
            amount = row['amount']
            if from_p in debts and to_p in debts[from_p]:
                debts[from_p][to_p] -= amount
    
    net_debts = {}
    for person in ALL_PERSONS:
        net_debts[person] = {
            creditor: amount
            for creditor, amount in debts[person].items()
            if amount > 0
        }
    return net_debts


# ===== توابع پروژه‌های در انتظار =====

def add_pending_project(data: Dict, user_id: int = 0) -> int:
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO pending_projects (
                customer_name, phone, location, device_type,
                description, estimated_amount, scheduled_date, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('customer_name', ''),
            data.get('phone', ''),
            data.get('location', ''),
            data.get('device_type', ''),
            data.get('description', ''),
            int(data.get('estimated_amount', 0)),
            data.get('scheduled_date', ''),
            data.get('notes', '')
        ))
        pending_id = cursor.lastrowid
        conn.execute("""
            INSERT INTO operation_log (user_id, operation_type, table_name, record_id, snapshot)
            VALUES (?, 'INSERT', 'pending_projects', ?, ?)
        """, (user_id, pending_id, json.dumps(data, ensure_ascii=False, default=str)))
        conn.commit()
        return pending_id


def get_pending_projects(status: str = 'pending') -> List[Dict]:
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT * FROM pending_projects WHERE status = ? AND is_deleted = 0 ORDER BY created_at DESC",
            (status,)
        )
        return [dict(row) for row in cursor.fetchall()]


def update_pending_status(pending_id: int, status: str) -> bool:
    with get_db() as conn:
        conn.execute(
            "UPDATE pending_projects SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, pending_id)
        )
        conn.commit()
        return True


# ===== Undo =====

def undo_last_operation(user_id: int) -> Optional[Dict]:
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT * FROM operation_log
            WHERE user_id = ? AND operation_type != 'UNDO'
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        op = dict(row)
        
        if op['operation_type'] == 'INSERT':
            table = op['table_name']
            record_id = op['record_id']
            if table in ['projects', 'daily_costs', 'settlements', 'pending_projects']:
                conn.execute(f"UPDATE {table} SET is_deleted = 1 WHERE id = ?", (record_id,))
            conn.execute("""
                INSERT INTO operation_log (user_id, operation_type, table_name, record_id)
                VALUES (?, 'UNDO', ?, ?)
            """, (user_id, table, record_id))
            conn.commit()
            return op
        
        elif op['operation_type'] == 'DELETE':
            table = op['table_name']
            record_id = op['record_id']
            conn.execute(f"UPDATE {table} SET is_deleted = 0 WHERE id = ?", (record_id,))
            conn.execute("""
                INSERT INTO operation_log (user_id, operation_type, table_name, record_id)
                VALUES (?, 'UNDO', ?, ?)
            """, (user_id, table, record_id))
            conn.commit()
            return op
        
        return op

# ============================
# نرمال‌سازی فارسی
# ============================

def normalize_persian_text(text: str) -> str:
    if not text:
        return ""
    replacements = {
        'ي': 'ی', 'ك': 'ک',
        'ة': 'ه', 'ۀ': 'ه',
        'أ': 'ا', 'إ': 'ا',
        'ؤ': 'و', 'ئ': 'ی',
        '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4',
        '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
        '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4',
        '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
        '٬': '', ',': '', '،': ' ',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    
    corrections = {
        'پکیچ': 'پکیج',
        'پکیح': 'پکیج',
        'کولرگازی': 'کولر گازی',
        'اسپیلت': 'اسپلیت',
        'نصبی': 'نصب',
        'تعمیراتی': 'تعمیر',
        'شوفاژ دیواری': 'پکیج',
        'شوفاژ': 'پکیج',
        'تومن': 'تومان',
    }
    for old, new in corrections.items():
        text = text.replace(old, new)
    
    return text.strip()


# ============================
# Parser اعداد فارسی
# ============================

PERSIAN_NUMBER_WORDS = {
    'صفر': 0, 'یک': 1, 'دو': 2, 'سه': 3, 'چهار': 4,
    'پنج': 5, 'شش': 6, 'هفت': 7, 'هشت': 8, 'نه': 9,
    'ده': 10, 'یازده': 11, 'دوازده': 12, 'سیزده': 13,
    'چهارده': 14, 'پانزده': 15, 'شانزده': 16, 'هفده': 17,
    'هجده': 18, 'نوزده': 19, 'بیست': 20, 'سی': 30,
    'چهل': 40, 'پنجاه': 50, 'شصت': 60, 'هفتاد': 70,
    'هشتاد': 80, 'نود': 90, 'صد': 100, 'دویست': 200,
    'سیصد': 300, 'چهارصد': 400, 'پانصد': 500,
    'ششصد': 600, 'هفتصد': 700, 'هشتصد': 800, 'نهصد': 900,
    'هزار': 1000, 'میلیون': 1000000, 'ملیون': 1000000,
    'میلیارد': 1000000000,
}


def parse_persian_number(text: str) -> Optional[int]:
    if not text:
        return None
    text = normalize_persian_text(text)
    
    clean = re.sub(r'[^\d]', '', text)
    if clean and text.strip().isdigit():
        return int(clean)
    
    if 'نیم میلیون' in text:
        return 500000
    if 'یک و نیم میلیون' in text:
        return 1500000
    
    total = 0
    current = 0
    for word in text.split():
        if word in PERSIAN_NUMBER_WORDS:
            value = PERSIAN_NUMBER_WORDS[word]
            if value >= 1000:
                if current == 0:
                    current = 1
                total += current * value
                current = 0
            else:
                current += value
        elif word.isdigit():
            current += int(word)
    total += current
    if total > 0:
        return total
    
    numbers = re.findall(r'\d+', text)
    if numbers:
        return int(numbers[0])
    return None


def extract_amount(text: str) -> Optional[int]:
    if not text:
        return None
    text = normalize_persian_text(text)
    
    match = re.search(r'(\d+(?:[.,]\d+)?)\s*میلیون', text)
    if match:
        num = float(match.group(1).replace(',', '.'))
        return int(num * 1000000)
    
    match = re.search(r'(\d+)\s*هزار', text)
    if match:
        return int(match.group(1)) * 1000
    
    for word, value in PERSIAN_NUMBER_WORDS.items():
        if word in text and value >= 1000:
            if 'میلیون' in text or 'ملیون' in text:
                return value * 1000000
            if 'هزار' in text:
                return value * 1000
    
    if 'نیم میلیون' in text:
        return 500000
    
    numbers = re.findall(r'\d+', text)
    if numbers:
        return int(numbers[0])
    return None


def parse_persian_datetime(text: str) -> Optional[str]:
    if not text:
        return None
    today = datetime.now()
    if 'امروز' in text:
        return today.strftime('%Y-%m-%d')
    if 'دیروز' in text:
        return (today - timedelta(days=1)).strftime('%Y-%m-%d')
    if 'فردا' in text:
        return (today + timedelta(days=1)).strftime('%Y-%m-%d')
    match = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', text)
    if match:
        return f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
    return None


# ============================
# تشخیص نوع و استخراج
# ============================

def detect_operation_type(text: str) -> str:
    if not text:
        return 'unknown'
    text = normalize_persian_text(text).lower()
    
    if any(w in text for w in ['تسویه', 'بدهکار', 'بستانکار', 'دادم به', 'گرفت از']):
        return 'settlement'
    if any(w in text for w in ['هزینه پروژه', 'خرید قطعه', 'گاز', 'خازن', 'لوله']):
        return 'project_cost'
    if any(w in text for w in ['ناهار', 'صبحانه', 'شام', 'بنزین', 'چای', 'قهوه']):
        return 'daily_cost'
    if any(w in text for w in ['پروژه', 'تعمیر', 'نصب', 'کولر', 'پکیج', 'مشتری', 'گرفتیم']):
        return 'project'
    if any(w in text for w in ['فردا', 'شنبه', 'یکشنبه', 'دوشنبه', 'بریم', 'قرار']):
        return 'pending_project'
    return 'unknown'


def extract_persons(text: str) -> List[str]:
    persons = []
    if 'من' in text or 'خودم' in text:
        persons.append(PERSON_ME)
    if 'علی' in text:
        persons.append(PERSON_ALI)
    if 'حسین' in text:
        persons.append(PERSON_HOSEIN)
    if 'شاگرد' in text:
        persons.append(PERSON_SHAGERD)
    return persons


def extract_paid_by(text: str) -> str:
    if 'حسین' in text and any(w in text for w in ['داد', 'پرداخت', 'حساب کرد']):
        return PERSON_HOSEIN
    if 'علی' in text and any(w in text for w in ['داد', 'پرداخت', 'حساب کرد']):
        return PERSON_ALI
    if 'شاگرد' in text and any(w in text for w in ['داد', 'پرداخت']):
        return PERSON_SHAGERD
    if 'من' in text and any(w in text for w in ['دادم', 'پرداخت', 'حساب کردم']):
        return PERSON_ME
    return PERSON_ME


def extract_category(text: str) -> str:
    categories = {
        'ناهار': ['ناهار'],
        'صبحانه': ['صبحانه'],
        'شام': ['شام'],
        'بنزین': ['بنزین', 'سوخت'],
        'خرید متفرقه': ['خرید', 'خریدم'],
        'چای/قهوه': ['چای', 'قهوه'],
    }
    for cat, words in categories.items():
        if any(w in text for w in words):
            return cat
    return 'سایر'


def parse_project_text(text: str) -> Dict:
    text = normalize_persian_text(text)
    total_income = extract_amount(text) or 0
    
    project_cost = 0
    cost_patterns = [
        r'هزینه\s*(?:شد|کردیم|داشت)?\s*(\d+)',
        r'(\d+)\s*(?:تومان|تومن)?\s*هزینه',
        r'قطعه\s*(\d+)',
        r'گاز\s*(\d+)',
    ]
    for pat in cost_patterns:
        match = re.search(pat, text)
        if match:
            project_cost = int(re.sub(r'[^\d]', '', match.group(1)) or 0)
            break
    
    service_type = 'سایر'
    if 'پکیج' in text:
        if 'تعمیر' in text:
            service_type = 'تعمیر پکیج'
        elif 'نصب' in text:
            service_type = 'نصب پکیج'
        else:
            service_type = 'سرویس پکیج'
    elif 'کولر' in text:
        if 'تعمیر' in text:
            service_type = 'تعمیر کولر'
        elif 'نصب' in text:
            service_type = 'نصب کولر'
        else:
            service_type = 'سرویس کولر'
    
    customer_name = ''
    for pat in [r'برای\s+([^\s،.]+)', r'آقای\s+([^\s،.]+)', r'خانم\s+([^\s،.]+)']:
        match = re.search(pat, text)
        if match:
            customer_name = match.group(1)
            break
    
    location = ''
    for loc in ['صادقیه', 'شهرک', 'اکباتان', 'پونک', 'جنت‌آباد', 'تهران', 'کرج', 'دانیال']:
        if loc in text:
            location = loc
            break
    
    return {
        'type': 'project',
        'customer_name': customer_name,
        'location': location,
        'service_type': service_type,
        'device_type': 'کولر گازی' if 'کولر' in text else ('پکیج' if 'پکیج' in text else ''),
        'description': text,
        'total_income': total_income,
        'project_cost': project_cost,
        'project_date': parse_persian_datetime(text) or datetime.now().strftime('%Y-%m-%d'),
    }


def parse_daily_cost_text(text: str) -> Dict:
    text = normalize_persian_text(text)
    amount = extract_amount(text) or 0
    persons = extract_persons(text)
    paid_by = extract_paid_by(text)
    category = extract_category(text)
    if not persons:
        persons = [PERSON_ME, PERSON_ALI, PERSON_HOSEIN, PERSON_SHAGERD]
    return {
        'type': 'daily_cost',
        'description': text,
        'category': category,
        'amount': amount,
        'paid_by': paid_by,
        'consumers': persons,
        'cost_date': parse_persian_datetime(text) or datetime.now().strftime('%Y-%m-%d'),
    }


def parse_settlement_text(text: str) -> Dict:
    text = normalize_persian_text(text)
    amount = extract_amount(text) or 0
    from_person = PERSON_ME
    to_person = PERSON_ALI
    if 'علی' in text and 'به من' in text:
        from_person, to_person = PERSON_ALI, PERSON_ME
    elif 'من' in text and 'به علی' in text:
        from_person, to_person = PERSON_ME, PERSON_ALI
    elif 'حسین' in text and 'به من' in text:
        from_person, to_person = PERSON_HOSEIN, PERSON_ME
    elif 'من' in text and 'به حسین' in text:
        from_person, to_person = PERSON_ME, PERSON_HOSEIN
    return {
        'type': 'settlement',
        'from_person': from_person,
        'to_person': to_person,
        'amount': amount,
        'description': text,
        'settlement_date': parse_persian_datetime(text) or datetime.now().strftime('%Y-%m-%d'),
    }


def parse_pending_project_text(text: str) -> Dict:
    text = normalize_persian_text(text)
    customer_name = ''
    for pat in [r'برای\s+([^\s،.]+)', r'آقای\s+([^\s،.]+)', r'خانم\s+([^\s،.]+)']:
        match = re.search(pat, text)
        if match:
            customer_name = match.group(1)
            break
    device_type = ''
    if 'کولر' in text:
        device_type = 'کولر گازی'
    elif 'پکیج' in text:
        device_type = 'پکیج'
    return {
        'type': 'pending_project',
        'customer_name': customer_name,
        'location': '',
        'device_type': device_type,
        'description': text,
        'estimated_amount': extract_amount(text) or 0,
        'scheduled_date': parse_persian_datetime(text) or '',
    }


def parse_text(text: str) -> Dict:
    if not text:
        return {'type': 'unknown', 'text': ''}
    text = normalize_persian_text(text)
    op_type = detect_operation_type(text)
    if op_type == 'project':
        return parse_project_text(text)
    elif op_type == 'daily_cost':
        return parse_daily_cost_text(text)
    elif op_type == 'settlement':
        return parse_settlement_text(text)
    elif op_type == 'pending_project':
        return parse_pending_project_text(text)
    else:
        return {'type': 'unknown', 'text': text}


# ============================
# Voice
# ============================

vosk_model = None


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def load_vosk_model():
    """بارگذاری مدل Vosk با دانلود خودکار در صورت نبود"""
    global vosk_model
    if vosk_model is not None:
        return vosk_model
    if not VOSK_AVAILABLE:
        raise RuntimeError("Vosk نصب نیست")
    
    model_path = CONFIG["VOSK_MODEL_PATH"]
    
    # اگر مدل وجود نداشت، دانلود کن
    if not os.path.exists(model_path):
        logger.info("📥 دانلود مدل Vosk...")
        base_dir = Path(model_path).parent
        base_dir.mkdir(parents=True, exist_ok=True)
        zip_path = base_dir / "vosk-model.zip"
        
        url = "https://alphacephei.com/vosk/models/vosk-model-small-fa-0.5.zip"
        try:
            urllib.request.urlretrieve(url, str(zip_path))
            with zipfile.ZipFile(str(zip_path), 'r') as zip_ref:
                zip_ref.extractall(str(base_dir))
            os.unlink(str(zip_path))
            logger.info("✅ مدل دانلود و استخراج شد")
        except Exception as e:
            logger.error(f"❌ خطا در دانلود مدل: {e}")
            raise
    
    vosk_model = vosk.Model(model_path)
    logger.info(f"✅ مدل Vosk بارگذاری شد: {model_path}")
    return vosk_model


def convert_ogg_to_wav(ogg_path: str, wav_path: str) -> bool:
    if not check_ffmpeg():
        return False
    try:
        subprocess.run([
            "ffmpeg", "-i", ogg_path,
            "-ar", "16000", "-ac", "1",
            wav_path, "-y"
        ], check=True, capture_output=True, timeout=60)
        return True
    except Exception as e:
        logger.error(f"خطا در تبدیل: {e}")
        return False


def transcribe_audio(wav_path: str) -> str:
    if not VOSK_AVAILABLE:
        return ""
    try:
        model = load_vosk_model()
        wf = wave.open(wav_path, "rb")
        if wf.getframerate() != 16000:
            wf.close()
            return ""
        rec = vosk.KaldiRecognizer(model, wf.getframerate())
        text_parts = []
        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                if result.get("text"):
                    text_parts.append(result["text"])
        final = json.loads(rec.FinalResult())
        if final.get("text"):
            text_parts.append(final["text"])
        wf.close()
        return " ".join(text_parts).strip()
    except Exception as e:
        logger.error(f"خطا در تشخیص: {e}")
        return ""


def process_voice_file(ogg_path: str) -> str:
    wav_path = ogg_path + ".wav"
    try:
        if not convert_ogg_to_wav(ogg_path, wav_path):
            return ""
        return transcribe_audio(wav_path)
    finally:
        if os.path.exists(wav_path):
            try:
                os.unlink(wav_path)
            except Exception:
                pass


# ============================
# گزارش‌ها
# ============================

def format_money(amount: int) -> str:
    return f"{amount:,}"


def generate_today_report() -> str:
    today = datetime.now().strftime('%Y-%m-%d')
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT 
                COUNT(*) as count,
                COALESCE(SUM(total_income), 0) as income,
                COALESCE(SUM(project_cost), 0) as cost,
                COALESCE(SUM(net_profit), 0) as profit,
                COALESCE(SUM(hosein_share), 0) as hosein,
                COALESCE(SUM(my_share), 0) as my,
                COALESCE(SUM(ali_share), 0) as ali
            FROM projects
            WHERE is_deleted = 0 AND project_date = ?
        """, (today,))
        p = dict(cursor.fetchone())
        cursor = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM daily_costs
            WHERE is_deleted = 0 AND cost_date = ?
        """, (today,))
        daily = dict(cursor.fetchone())
    
    return "\n".join([
        f"📊 گزارش امروز ({today})",
        "=" * 30,
        "",
        f"📋 تعداد پروژه: {p['count']}",
        f"💰 درآمد: {format_money(p['income'])} تومان",
        f"💸 هزینه مستقیم: {format_money(p['cost'])} تومان",
        f"📈 سود: {format_money(p['profit'])} تومان",
        "",
        "سهم‌ها:",
        f"  حسین: {format_money(p['hosein'])} تومان",
        f"  من: {format_money(p['my'])} تومان",
        f"  علی: {format_money(p['ali'])} تومان",
        "",
        "=" * 30,
        f"💰 هزینه روزانه: {format_money(daily['total'])} تومان",
    ])


def generate_monthly_report(month: str) -> str:
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT 
                COUNT(*) as count,
                COALESCE(SUM(total_income), 0) as income,
                COALESCE(SUM(project_cost), 0) as cost,
                COALESCE(SUM(net_profit), 0) as profit,
                COALESCE(SUM(hosein_share), 0) as hosein,
                COALESCE(SUM(my_share), 0) as my,
                COALESCE(SUM(ali_share), 0) as ali
            FROM projects
            WHERE is_deleted = 0 AND strftime('%Y-%m', project_date) = ?
        """, (month,))
        p = dict(cursor.fetchone())
        cursor = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM daily_costs
            WHERE is_deleted = 0 AND strftime('%Y-%m', cost_date) = ?
        """, (month,))
        daily = dict(cursor.fetchone())
    
    return "\n".join([
        f"📅 گزارش ماهانه ({month})",
        "=" * 30,
        "",
        f"📋 تعداد پروژه: {p['count']}",
        f"💰 کل درآمد: {format_money(p['income'])} تومان",
        f"💸 کل هزینه: {format_money(p['cost'])} تومان",
        f"📈 سود خالص: {format_money(p['profit'])} تومان",
        "",
        "سهم‌ها:",
        f"  حسین (۳۵%): {format_money(p['hosein'])} تومان",
        f"  من (۳۲.۵%): {format_money(p['my'])} تومان",
        f"  علی (۳۲.۵%): {format_money(p['ali'])} تومان",
        "",
        "=" * 30,
        f"💰 هزینه روزانه: {format_money(daily['total'])} تومان",
    ])


def generate_debt_report() -> str:
    debts = calculate_debts()
    lines = ["💰 حساب تسویه", "=" * 30, ""]
    has_debt = False
    for person, creditors in debts.items():
        for creditor, amount in creditors.items():
            if amount > 0:
                lines.append(f"🔴 {person} باید به {creditor}: {format_money(amount)} تومان")
                has_debt = True
    if not has_debt:
        lines.append("✅ همه تسویه هستند.")
    return "\n".join(lines)

# ============================
# کیبوردها
# ============================

def get_main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📋 پروژه جدید", callback_data="new_project")],
        [InlineKeyboardButton("💰 هزینه روزانه", callback_data="new_daily_cost")],
        [InlineKeyboardButton("💵 تسویه", callback_data="new_settlement")],
        [InlineKeyboardButton("📌 پروژه‌های در انتظار", callback_data="view_pending")],
        [InlineKeyboardButton("📊 گزارش امروز", callback_data="report_today")],
        [InlineKeyboardButton("📅 گزارش ماهانه", callback_data="report_monthly")],
        [InlineKeyboardButton("💳 حساب من و علی", callback_data="report_debts")],
        [InlineKeyboardButton("↩️ آخرین عملیات", callback_data="undo")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأیید", callback_data="confirm_yes"),
         InlineKeyboardButton("✏️ ویرایش", callback_data="confirm_edit")],
        [InlineKeyboardButton("❌ لغو", callback_data="confirm_cancel")],
    ])


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ لغو", callback_data="cancel")]
    ])


def get_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 ویرایش مبلغ", callback_data="edit_amount")],
        [InlineKeyboardButton("👤 ویرایش پرداخت‌کننده", callback_data="edit_payer")],
        [InlineKeyboardButton("👥 ویرایش افراد", callback_data="edit_consumers")],
        [InlineKeyboardButton("✅ پایان ویرایش", callback_data="edit_done")],
        [InlineKeyboardButton("❌ لغو", callback_data="cancel")],
    ])


# ============================
# وضعیت‌ها
# ============================

(
    MAIN_MENU,
    WAITING_PROJECT_INFO,
    WAITING_DAILY_COST_INFO,
    WAITING_SETTLEMENT_INFO,
    WAITING_PENDING_INFO,
    WAITING_CONFIRMATION,
    WAITING_EDIT,
    WAITING_MONTH_INPUT,
    WAITING_AMOUNT_INPUT,
    WAITING_PAYER_INPUT,
    WAITING_CONSUMERS_INPUT,
) = range(11)


# ============================
# امنیت
# ============================

def is_authorized(user_id: int) -> bool:
    authorized_str = CONFIG["AUTHORIZED_USERS"]
    if not authorized_str:
        return True
    try:
        authorized = [int(x.strip()) for x in authorized_str.split(',') if x.strip()]
        return user_id in authorized
    except Exception:
        return True


def auth_decorator(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else 0
        if not is_authorized(user_id):
            if update.message:
                await update.message.reply_text("⛔ دسترسی ندارید.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ دسترسی ندارید", show_alert=True)
            return
        return await func(update, context)
    return wrapper


# ============================
# هندلرهای دستورات
# ============================

@auth_decorator
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 سلام!\nمن ربات مدیریت مالی کارگاه هستم.\n\n"
        "از دکمه‌ها استفاده کن یا متن/ویس بفرست.",
        reply_markup=get_main_keyboard()
    )
    return MAIN_MENU


@auth_decorator
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 راهنما\n\n"
        "🔹 پروژه: «برای احمدی پکیج تعمیر کردیم ۸ میلیون گرفتیم»\n"
        "🔹 هزینه روزانه: «ناهار ۶۰۰ هزار، من و علی و حسین و شاگرد، حسین حساب کرد»\n"
        "🔹 تسویه: «علی ۵۰۰ هزار به من داد»\n"
        "🔹 پروژه در انتظار: «فردا برای رضایی بریم کولر نصب کنیم»",
        reply_markup=get_main_keyboard()
    )
    return MAIN_MENU


# ============================
# نمایش تأیید
# ============================

async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = context.user_data.get('parsed_data', {})
    entry_type = context.user_data.get('entry_type', 'unknown')
    
    if update.callback_query:
        send = update.callback_query.edit_message_text
    else:
        send = update.message.reply_text
    
    if entry_type == 'project' or parsed.get('type') == 'project':
        total = parsed.get('total_income', 0)
        cost = parsed.get('project_cost', 0)
        profit = total - cost
        hosein = int(profit * SHARE_HOSEIN)
        my = int(profit * SHARE_ME)
        ali = profit - hosein - my
        text = (
            f"🔎 بررسی پروژه\n\n"
            f"👤 مشتری: {parsed.get('customer_name') or 'نامشخص'}\n"
            f"🛠️ نوع کار: {parsed.get('service_type') or 'نامشخص'}\n"
            f"📍 مکان: {parsed.get('location') or 'نامشخص'}\n\n"
            f"💰 مبلغ کل: {format_money(total)} تومان\n"
            f"💸 هزینه: {format_money(cost)} تومان\n"
            f"📈 سود: {format_money(profit)} تومان\n\n"
            f"سهم‌ها:\n"
            f"  حسین (۳۵%): {format_money(hosein)}\n"
            f"  من (۳۲.۵%): {format_money(my)}\n"
            f"  علی (۳۲.۵%): {format_money(ali)}\n\n"
            f"❓ آیا اطلاعات صحیح است؟"
        )
    
    elif entry_type == 'daily_cost' or parsed.get('type') == 'daily_cost':
        amount = parsed.get('amount', 0)
        paid_by = parsed.get('paid_by', '')
        consumers = parsed.get('consumers', [])
        shares = calculate_daily_cost_shares(amount, consumers)
        context.user_data['shares'] = shares
        shares_text = "\n".join([
            f"  {p}: {format_money(shares.get(p, 0))}"
            for p in ALL_PERSONS if shares.get(p, 0) > 0
        ])
        text = (
            f"🔎 بررسی هزینه\n\n"
            f"📂 نوع: {parsed.get('category', 'سایر')}\n"
            f"💰 مبلغ کل: {format_money(amount)} تومان\n"
            f"👤 پرداخت‌کننده: {paid_by}\n\n"
            f"👥 افراد: {', '.join(consumers)}\n\n"
            f"سهم نهایی:\n{shares_text}\n\n"
            f"❓ آیا اطلاعات صحیح است؟"
        )
    
    elif entry_type == 'settlement' or parsed.get('type') == 'settlement':
        text = (
            f"🔎 بررسی تسویه\n\n"
            f"👤 از: {parsed.get('from_person', '')}\n"
            f"👤 به: {parsed.get('to_person', '')}\n"
            f"💰 مبلغ: {format_money(parsed.get('amount', 0))} تومان\n\n"
            f"❓ آیا اطلاعات صحیح است؟"
        )
    
    elif entry_type == 'pending_project' or parsed.get('type') == 'pending_project':
        text = (
            f"🔎 بررسی پروژه در انتظار\n\n"
            f"👤 مشتری: {parsed.get('customer_name') or 'نامشخص'}\n"
            f"🛠️ دستگاه: {parsed.get('device_type') or 'نامشخص'}\n"
            f"💰 برآورد: {format_money(parsed.get('estimated_amount', 0))} تومان\n\n"
            f"❓ آیا اطلاعات صحیح است؟"
        )
    
    else:
        await send("❌ نوع اطلاعات نامشخص است.", reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    await send(text, reply_markup=get_confirmation_keyboard())
    return WAITING_CONFIRMATION


# ============================
# تأیید و ذخیره
# ============================

async def confirm_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parsed = context.user_data.get('parsed_data', {})
    entry_type = context.user_data.get('entry_type') or parsed.get('type')
    user_id = update.effective_user.id
    
    try:
        if entry_type == 'project' or parsed.get('type') == 'project':
            project_id = add_project(parsed, user_id)
            project = get_project_by_id(project_id)
            msg = (
                f"✅ پروژه ثبت شد!\n\n"
                f"👤 {project['customer_name']}\n"
                f"💰 درآمد: {format_money(project['total_income'])}\n"
                f"📈 سود: {format_money(project['net_profit'])}\n"
                f"سهم حسین: {format_money(project['hosein_share'])}\n"
                f"سهم من: {format_money(project['my_share'])}\n"
                f"سهم علی: {format_money(project['ali_share'])}"
            )
        
        elif entry_type == 'daily_cost' or parsed.get('type') == 'daily_cost':
            shares = context.user_data.get('shares') or calculate_daily_cost_shares(
                parsed.get('amount', 0), parsed.get('consumers', [])
            )
            add_daily_cost(parsed, shares, user_id)
            msg = (
                f"✅ هزینه روزانه ثبت شد!\n\n"
                f"💰 {format_money(parsed.get('amount', 0))} تومان\n"
                f"👤 پرداخت‌کننده: {parsed.get('paid_by', '')}"
            )
        
        elif entry_type == 'settlement' or parsed.get('type') == 'settlement':
            add_settlement(parsed, user_id)
            msg = (
                f"✅ تسویه ثبت شد!\n\n"
                f"👤 {parsed.get('from_person', '')} → {parsed.get('to_person', '')}\n"
                f"💰 {format_money(parsed.get('amount', 0))} تومان"
            )
        
        elif entry_type == 'pending_project' or parsed.get('type') == 'pending_project':
            add_pending_project(parsed, user_id)
            msg = f"✅ پروژه در انتظار ثبت شد!\n\n👤 {parsed.get('customer_name', '')}"
        
        else:
            msg = "❌ نوع اطلاعات نامشخص است."
        
        context.user_data.clear()
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    except Exception as e:
        logger.error(f"خطا در ذخیره: {e}", exc_info=True)
        await query.edit_message_text(
            f"❌ خطا در ذخیره: {str(e)}",
            reply_markup=get_main_keyboard()
        )
        return MAIN_MENU


# ============================
# Button Handler
# ============================

@auth_decorator
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    
    if data == "new_project":
        context.user_data.clear()
        context.user_data['entry_type'] = 'project'
        await query.edit_message_text(
            "📋 اطلاعات پروژه را بفرستید (متن یا ویس).\n\n"
            "مثال:\n«برای احمدی پکیج تعمیر کردیم ۸ میلیون گرفتیم ۱ میلیون هزینه شد»",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_PROJECT_INFO
    
    elif data == "new_daily_cost":
        context.user_data.clear()
        context.user_data['entry_type'] = 'daily_cost'
        await query.edit_message_text(
            "💰 اطلاعات هزینه روزانه را بفرستید.\n\n"
            "مثال:\n«ناهار ۶۰۰ هزار، من و علی و حسین و شاگرد، حسین حساب کرد»",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_DAILY_COST_INFO
    
    elif data == "new_settlement":
        context.user_data.clear()
        context.user_data['entry_type'] = 'settlement'
        await query.edit_message_text(
            "💵 اطلاعات تسویه را بفرستید.\n\nمثال:\n«علی ۵۰۰ هزار به من داد»",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_SETTLEMENT_INFO
    
    elif data == "view_pending":
        pending = get_pending_projects()
        if not pending:
            await query.edit_message_text(
                "📌 پروژه در انتظاری وجود ندارد.",
                reply_markup=get_main_keyboard()
            )
            return MAIN_MENU
        lines = ["📌 پروژه‌های در انتظار:\n"]
        for p in pending:
            lines.append(
                f"🔸 {p['customer_name']} - {p['device_type'] or 'نامشخص'}\n"
                f"   📅 {p['scheduled_date'] or 'بدون تاریخ'}\n"
                f"   💰 {format_money(p['estimated_amount'])} تومان\n"
            )
        await query.edit_message_text("\n".join(lines), reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    elif data == "report_today":
        await query.edit_message_text(generate_today_report(), reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    elif data == "report_monthly":
        context.user_data['waiting_for'] = 'month'
        await query.edit_message_text(
            "📅 ماه را به فرمت YYYY-MM وارد کنید:\nمثال: 2026-09",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_MONTH_INPUT
    
    elif data == "report_debts":
        await query.edit_message_text(generate_debt_report(), reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    elif data == "undo":
        op = undo_last_operation(user_id)
        if op:
            await query.edit_message_text(
                f"↩️ آخرین عملیات لغو شد.\nنوع: {op['operation_type']}\nجدول: {op['table_name']}",
                reply_markup=get_main_keyboard()
            )
        else:
            await query.edit_message_text(
                "❌ عملیاتی برای لغو وجود ندارد.",
                reply_markup=get_main_keyboard()
            )
        return MAIN_MENU
    
    elif data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ عملیات لغو شد.", reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    elif data == "confirm_yes":
        return await confirm_and_save(update, context)
    
    elif data == "confirm_edit":
        await query.edit_message_text("✏️ چه چیزی را ویرایش می‌کنید؟", reply_markup=get_edit_keyboard())
        return WAITING_EDIT
    
    elif data == "confirm_cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ عملیات لغو شد.", reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    elif data == "edit_amount":
        context.user_data['edit_field'] = 'amount'
        context.user_data['waiting_for'] = 'amount'
        await query.edit_message_text("💰 مبلغ جدید را وارد کنید:", reply_markup=get_cancel_keyboard())
        return WAITING_AMOUNT_INPUT
    
    elif data == "edit_payer":
        keyboard = [[InlineKeyboardButton(p, callback_data=f"set_payer_{p}")] for p in ALL_PERSONS]
        keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="cancel")])
        await query.edit_message_text("👤 پرداخت‌کننده جدید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return WAITING_PAYER_INPUT
    
    elif data == "edit_consumers":
        context.user_data['edit_consumers'] = context.user_data.get('parsed_data', {}).get('consumers', [])
        consumers = context.user_data['edit_consumers']
        keyboard = []
        for p in ALL_PERSONS:
            mark = "✅" if p in consumers else "☐"
            keyboard.append([InlineKeyboardButton(f"{mark} {p}", callback_data=f"toggle_consumer_{p}")])
        keyboard.append([InlineKeyboardButton("✅ تأیید افراد", callback_data="consumers_done")])
        keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="cancel")])
        await query.edit_message_text(
            f"👥 افراد انتخاب‌شده: {', '.join(consumers) if consumers else 'هیچ‌کس'}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_CONSUMERS_INPUT
    
    elif data.startswith("set_payer_"):
        payer = data.replace("set_payer_", "")
        if 'parsed_data' in context.user_data:
            context.user_data['parsed_data']['paid_by'] = payer
        await query.edit_message_text(f"✅ پرداخت‌کننده: {payer}", reply_markup=get_edit_keyboard())
        return WAITING_EDIT
    
    elif data.startswith("toggle_consumer_"):
        person = data.replace("toggle_consumer_", "")
        consumers = context.user_data.get('edit_consumers', [])
        if person in consumers:
            consumers.remove(person)
        else:
            consumers.append(person)
        context.user_data['edit_consumers'] = consumers
        keyboard = []
        for p in ALL_PERSONS:
            mark = "✅" if p in consumers else "☐"
            keyboard.append([InlineKeyboardButton(f"{mark} {p}", callback_data=f"toggle_consumer_{p}")])
        keyboard.append([InlineKeyboardButton("✅ تأیید افراد", callback_data="consumers_done")])
        keyboard.append([InlineKeyboardButton("❌ لغو", callback_data="cancel")])
        await query.edit_message_text(
            f"👥 افراد انتخاب‌شده: {', '.join(consumers) if consumers else 'هیچ‌کس'}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_CONSUMERS_INPUT
    
    elif data == "consumers_done":
        consumers = context.user_data.get('edit_consumers', [])
        if 'parsed_data' in context.user_data:
            context.user_data['parsed_data']['consumers'] = consumers
            parsed = context.user_data['parsed_data']
            if parsed.get('type') == 'daily_cost':
                shares = calculate_daily_cost_shares(parsed.get('amount', 0), consumers)
                context.user_data['shares'] = shares
        await query.edit_message_text("✅ افراد به‌روزرسانی شد.", reply_markup=get_edit_keyboard())
        return WAITING_EDIT
    
    elif data == "edit_done":
        return await show_confirmation(update, context)
    
    return MAIN_MENU


# ============================
# هندلر متن
# ============================

@auth_decorator
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = context.user_data.pop('text_input', None) or update.message.text
    if not text:
        return MAIN_MENU
    
    if context.user_data.get('waiting_for') == 'month':
        month = text.strip()
        if re.match(r'^\d{4}-\d{2}$', month):
            await update.message.reply_text(generate_monthly_report(month), reply_markup=get_main_keyboard())
            context.user_data.clear()
            return MAIN_MENU
        else:
            await update.message.reply_text("❌ فرمت نامعتبر. مثال: 2026-09", reply_markup=get_cancel_keyboard())
            return WAITING_MONTH_INPUT
    
    if context.user_data.get('waiting_for') == 'amount':
        amount = extract_amount(text)
        if amount and amount > 0:
            parsed = context.user_data.get('parsed_data', {})
            edit_field = context.user_data.get('edit_field')
            if edit_field == 'amount' or parsed.get('type') == 'daily_cost':
                parsed['amount'] = amount
            if parsed.get('type') == 'project' or 'total_income' in parsed:
                parsed['total_income'] = amount
            context.user_data['parsed_data'] = parsed
            context.user_data['waiting_for'] = None
            context.user_data['edit_field'] = None
            return await show_confirmation(update, context)
        else:
            await update.message.reply_text(
                "❌ مبلغ نامعتبر. یک عدد معتبر وارد کنید:",
                reply_markup=get_cancel_keyboard()
            )
            return WAITING_AMOUNT_INPUT
    
    parsed = parse_text(text)
    
    if parsed.get('type') == 'unknown':
        await update.message.reply_text(
            "❓ متوجه نشدم. از دکمه‌ها استفاده کن یا واضح‌تر بنویس.",
            reply_markup=get_main_keyboard()
        )
        return MAIN_MENU
    
    if parsed.get('type') in ['project', 'daily_cost']:
        amount_field = 'total_income' if parsed['type'] == 'project' else 'amount'
        if parsed.get(amount_field, 0) == 0:
            context.user_data['parsed_data'] = parsed
            context.user_data['entry_type'] = parsed['type']
            context.user_data['waiting_for'] = 'amount'
            await update.message.reply_text(
                "⚠️ مبلغ تشخیص داده نشد. لطفاً مبلغ را به عدد وارد کنید:",
                reply_markup=get_cancel_keyboard()
            )
            return WAITING_AMOUNT_INPUT
    
    context.user_data['parsed_data'] = parsed
    context.user_data['entry_type'] = parsed['type']
    return await show_confirmation(update, context)


# ============================
# هندلر ویس
# ============================

@auth_decorator
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    
    if not VOSK_AVAILABLE:
        await update.message.reply_text(
            "❌ Vosk نصب نیست. با متن بفرست.",
            reply_markup=get_main_keyboard()
        )
        return MAIN_MENU
    
    if not check_ffmpeg():
        await update.message.reply_text(
            "❌ ffmpeg نصب نیست:\nsudo apt install ffmpeg",
            reply_markup=get_main_keyboard()
        )
        return MAIN_MENU
    
    await update.message.reply_text("🎤 در حال پردازش...")
    
    ogg_path = None
    try:
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            ogg_path = tmp.name
        await file.download_to_drive(ogg_path)
        
        text = process_voice_file(ogg_path)
        if not text:
            await update.message.reply_text(
                "❌ متنی تشخیص داده نشد. واضح‌تر صحبت کن.",
                reply_markup=get_main_keyboard()
            )
            return MAIN_MENU
        
        await update.message.reply_text(f"📝 متن: {text}")
        context.user_data['text_input'] = text
        return await handle_text(update, context)
    
    except Exception as e:
        logger.error(f"خطا در پردازش ویس: {e}", exc_info=True)
        await update.message.reply_text("❌ خطا در پردازش ویس.", reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    finally:
        if ogg_path and os.path.exists(ogg_path):
            try:
                os.unlink(ogg_path)
            except Exception:
                pass


# ============================
# هندلر خطا
# ============================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"خطا: {context.error}", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ خطایی رخ داد. دوباره تلاش کن.",
                reply_markup=get_main_keyboard()
            )
    except Exception:
        pass


# ============================
# Backup
# ============================

def create_backup() -> Optional[str]:
    try:
        backup_dir = Path(CONFIG["BACKUP_DIR"])
        backup_dir.mkdir(parents=True, exist_ok=True)
        db_path = Path(CONFIG["DATABASE_PATH"])
        if not db_path.exists():
            return None
        today = datetime.now().strftime('%Y-%m-%d')
        backup_path = backup_dir / f"business_{today}.db"
        if backup_path.exists():
            return str(backup_path)
        shutil.copy2(str(db_path), str(backup_path))
        logger.info(f"✅ بکاپ: {backup_path}")
        backups = sorted(backup_dir.glob("business_*.db"))
        max_backups = CONFIG["MAX_BACKUPS"]
        if len(backups) > max_backups:
            for old in backups[:-max_backups]:
                old.unlink()
        return str(backup_path)
    except Exception as e:
        logger.error(f"خطا در بکاپ: {e}")
        return None


# ============================
# اینترنت
# ============================

def is_internet_available() -> bool:
    """بررسی اتصال مستقیم به تلگرام (مناسب Railway/VPS)"""
    try:
        socket.create_connection(("api.telegram.org", 443), timeout=5)
        return True
    except (OSError, socket.timeout):
        return False


# ============================
# ساخت Application
# ============================

def build_application():
    request = HTTPXRequest(
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
    )
    app = Application.builder().token(CONFIG["BOT_TOKEN"]).request(request).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CommandHandler("help", help_command),
            MessageHandler(filters.VOICE, handle_voice),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            CallbackQueryHandler(button_handler),
        ],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.VOICE, handle_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
            WAITING_PROJECT_INFO: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.VOICE, handle_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
            WAITING_DAILY_COST_INFO: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.VOICE, handle_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
            WAITING_SETTLEMENT_INFO: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.VOICE, handle_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
            WAITING_PENDING_INFO: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.VOICE, handle_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
            WAITING_CONFIRMATION: [
                CallbackQueryHandler(button_handler),
            ],
            WAITING_EDIT: [
                CallbackQueryHandler(button_handler),
            ],
            WAITING_MONTH_INPUT: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
            WAITING_AMOUNT_INPUT: [
                CallbackQueryHandler(button_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
            WAITING_PAYER_INPUT: [
                CallbackQueryHandler(button_handler),
            ],
            WAITING_CONSUMERS_INPUT: [
                CallbackQueryHandler(button_handler),
            ],
        },
        fallbacks=[
            CommandHandler("start", start_command),
            CallbackQueryHandler(button_handler, pattern="^cancel$"),
        ],
    )
    
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    return app


# ============================
# اجرا با تلاش مجدد
# ============================

def run_bot_with_retry():
    retry_delay = CONFIG["RETRY_DELAY"]
    max_delay = 60
    attempt = 0
    
    while True:
        attempt += 1
        
        if not is_internet_available():
            logger.warning(f"⚠️ اینترنت قطع است. انتظار {retry_delay} ثانیه...")
            time.sleep(retry_delay)
            continue
        
        logger.info(f"🚀 تلاش {attempt} برای اتصال...")
        
        try:
            try:
                asyncio.set_event_loop(asyncio.new_event_loop())
            except Exception:
                pass
            
            app = build_application()
            app.run_polling(drop_pending_updates=True, close_loop=False)
            logger.info("⏹️ ربات متوقف شد.")
            break
        
        except (TimedOut, NetworkError, TelegramError) as e:
            logger.error(f"⚠️ خطای شبکه در تلاش {attempt}: {e}")
            wait_time = min(retry_delay * min(attempt, 10), max_delay)
            logger.info(f"⏳ تلاش مجدد پس از {wait_time} ثانیه...")
            time.sleep(wait_time)
            continue
        
        except RuntimeError as e:
            if "Event loop is closed" in str(e):
                logger.warning("⚠️ حلقه رویداد بسته شد. تلاش مجدد...")
                time.sleep(retry_delay)
                continue
            logger.error(f"❌ RuntimeError: {e}", exc_info=True)
            time.sleep(retry_delay)
            continue
        
        except KeyboardInterrupt:
            logger.info("⏹️ توقف توسط کاربر")
            break
        
        except Exception as e:
            logger.error(f"❌ خطای غیرمنتظره: {e}", exc_info=True)
            time.sleep(retry_delay)
            continue


# ============================
# main
# ============================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    
    if args.test:
        run_tests()
        return
    
    if not CONFIG["BOT_TOKEN"]:
        print("❌ BOT_TOKEN تنظیم نشده")
        sys.exit(1)
    
    init_db()
    create_backup()
    
    if not check_ffmpeg():
        logger.warning("⚠️ ffmpeg نصب نیست. قابلیت ویس کار نمی‌کند.")
    
    if VOSK_AVAILABLE:
        try:
            load_vosk_model()
        except Exception as e:
            logger.error(f"خطا در بارگذاری Vosk: {e}")
    
    run_bot_with_retry()


# ============================
# تست‌های داخلی
# ============================

def run_tests():
    print("🧪 شروع تست‌ها...\n")
    
    print("۱. نرمال‌سازی:")
    for inp, expected in [("پکیچ تعمیر", "پکیج تعمیر"), ("كولرگازي", "کولر گازی")]:
        result = normalize_persian_text(inp)
        print(f"  {'✅' if expected in result else '❌'} '{inp}' → '{result}'")
    
    print("\n۲. اعداد:")
    for inp, expected in [
        ("۸ میلیون", 8000000), ("هشت میلیون", 8000000),
        ("نیم میلیون", 500000), ("پانصد هزار", 500000),
        ("500 هزار", 500000), ("500000", 500000),
        ("یک و نیم میلیون", 1500000),
    ]:
        result = parse_persian_number(inp) or extract_amount(inp)
        print(f"  {'✅' if result == expected else f'❌ (got {result})'} '{inp}' → {result}")
    
    print("\n۳. سناریو ۱: ناهار ۶۰۰ هزار - همه - حسین پرداخت")
    shares = calculate_daily_cost_shares(600000, [PERSON_ME, PERSON_ALI, PERSON_HOSEIN, PERSON_SHAGERD])
    print(f"  سهم‌ها: {shares}")
    print(f"  جمع: {sum(shares.values())} {'✅' if sum(shares.values()) == 600000 else '❌'}")
    
    print("\n۴. سناریو ۲: ناهار ۳۰۰ هزار - من و علی")
    shares = calculate_daily_cost_shares(300000, [PERSON_ME, PERSON_ALI])
    print(f"  سهم‌ها: {shares}")
    print(f"  جمع: {sum(shares.values())} {'✅' if sum(shares.values()) == 300000 else '❌'}")
    
    print("\n۵. سناریو ۳: صبحانه ۳۰۰ هزار - من و حسین")
    shares = calculate_daily_cost_shares(300000, [PERSON_ME, PERSON_HOSEIN])
    print(f"  سهم‌ها: {shares}")
    print(f"  جمع: {sum(shares.values())} {'✅' if sum(shares.values()) == 300000 else '❌'}")
    
    print("\n۶. سناریو ۴: ناهار ۸۰۰ هزار - من + علی + شاگرد")
    shares = calculate_daily_cost_shares(800000, [PERSON_ME, PERSON_ALI, PERSON_SHAGERD])
    print(f"  سهم‌ها: {shares}")
    print(f"  جمع: {sum(shares.values())} {'✅' if sum(shares.values()) == 800000 else '❌'}")
    print(f"  انتظار: من=۴۰۰,۰۰۰، علی=۴۰۰,۰۰۰، حسین=۰")
    
    print("\n۷. پروژه: ۱۰ میلیون، هزینه ۲ میلیون")
    total, cost = 10000000, 2000000
    profit = total - cost
    hosein = int(profit * SHARE_HOSEIN)
    my = int(profit * SHARE_ME)
    ali = profit - hosein - my
    print(f"  سود: {profit:,}")
    print(f"  حسین: {hosein:,} {'✅' if hosein == 2800000 else '❌'}")
    print(f"  من: {my:,} {'✅' if my == 2600000 else '❌'}")
    print(f"  علی: {ali:,} {'✅' if ali == 2600000 else '❌'}")
    
    print("\n✅ تست‌ها پایان یافت.")


if __name__ == "__main__":
    main()