#!/usr/bin/env python3
"""
Скрипт миграции данных из SQLite в PostgreSQL
Запуск: python3 migrate.py
"""
import sqlite3
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv('/home/patriot/telegram-ai-bot/.env')

SQLITE_PATH = '/home/patriot/telegram-ai-bot/bot.db'
PG_DSN      = os.getenv('DATABASE_URL')

def migrate():
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(PG_DSN)
    dst.autocommit = False
    cur_dst = dst.cursor()

    print("=== Миграция users ===")
    rows = src.execute("SELECT * FROM users").fetchall()
    for r in rows:
        cur_dst.execute("""
            INSERT INTO users (
                user_id, model, free_tokens, paid_tokens,
                image_mode, image_model, image_flow, pending_image_prompt,
                video_mode, video_model, video_flow, video_duration,
                video_aspect_ratio, last_free_reset_at, is_banned
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id) DO NOTHING
        """, (
            r['user_id'], r['model'], r['free_tokens'], r['paid_tokens'],
            r['image_mode'], r['image_model'], r['image_flow'] or '',
            r['pending_image_prompt'] or '',
            r['video_mode'], r['video_model'], r['video_flow'] or 'prompt_only',
            r['video_duration'], r['video_aspect_ratio'],
            r['last_free_reset_at'], r['is_banned'] or 0
        ))
    print(f"  ✅ Перенесено пользователей: {len(rows)}")

    print("=== Миграция payments ===")
    rows = src.execute("SELECT * FROM payments").fetchall()
    for r in rows:
        cur_dst.execute("""
            INSERT INTO payments (
                payment_id, idempotence_key, user_id, plan_key,
                amount, tokens_count, status, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        """, (
            r['payment_id'], r['idempotence_key'], r['user_id'], r['plan_key'],
            r['amount'], r['tokens_count'], r['status'], r['created_at']
        ))
    print(f"  ✅ Перенесено платежей: {len(rows)}")

    print("=== Миграция chat_history ===")
    rows = src.execute("SELECT * FROM chat_history").fetchall()
    for r in rows:
        cur_dst.execute("""
            INSERT INTO chat_history (user_id, role, content, created_at)
            VALUES (%s,%s,%s,%s)
        """, (r['user_id'], r['role'], r['content'], r['created_at']))
    print(f"  ✅ Перенесено сообщений: {len(rows)}")

    print("=== Миграция generation_jobs ===")
    rows = src.execute("SELECT * FROM generation_jobs").fetchall()
    for r in rows:
        cur_dst.execute("""
            INSERT INTO generation_jobs (
                job_uuid, user_id, provider, kind, model, flow,
                prompt_text, cost, status, provider_generation_id,
                polling_url, file_path, error_text, charged,
                chat_id, wait_msg_id, next_poll_at, attempts,
                created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (job_uuid) DO NOTHING
        """, (
            r['job_uuid'], r['user_id'], r['provider'], r['kind'], r['model'],
            r['flow'] or '', r['prompt_text'] or '', r['cost'], r['status'],
            r['provider_generation_id'] or '', r['polling_url'] or '',
            r['file_path'] or '', r['error_text'] or '', r['charged'],
            r['chat_id'] or 0, r['wait_msg_id'] or 0,
            r['next_poll_at'] or 0, r['attempts'] or 0,
            r['created_at'], r['updated_at']
        ))
    print(f"  ✅ Перенесено задач генерации: {len(rows)}")

    dst.commit()
    dst.close()
    src.close()
    print("\n🎉 Миграция завершена успешно!")

if __name__ == '__main__':
    migrate()
