import sqlite3
from datetime import datetime
from pathlib import Path


class Database:
    def __init__(self, video_db_path: Path) -> None:
        # 打开持久化连接
        self.conn = sqlite3.connect(video_db_path)
        self.cursor = self.conn.cursor()
        # 初始化数据库表
        self.create_table()

    def create_table(self):
        # 创建视频数据表
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS video_data (
                task_id TEXT PRIMARY KEY NOT NULL,
                user_id INTEGER,
                nickname TEXT,
                prompt TEXT,
                image_url TEXT,
                status TEXT,
                video_url TEXT,
                generation_id TEXT,
                message_id INTEGER,
                auth_xor TEXT,
                error_msg TEXT,
                updated_at DATETIME,
                created_at DATETIME
            )
        """)

        # 创建Token数据表，无论用户选择何种Token类型都创建该表，进行统一管理。token_key是用户所填写的Token的最后16位。
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS token_table (
                token_key TEXT PRIMARY KEY NOT NULL,
                token_type TEXT,
                access_token TEXT,
                session_token_expire TEXT,
                access_token_expire TEXT,
                used_count_today INTEGER,
                token_state INTEGER,
                rate_limit_reached INTEGER,
                latest_used_at DATETIME,
                updated_at DATETIME
            )
        """)
        self.conn.commit()

    def load_token_data(self, tokens: list) -> list[tuple]:
        # 从数据库中加载持久化数据
        placeholders = ",".join("?" * len(tokens))
        self.cursor.execute(
            "SELECT token_key, access_token, used_count_today, rate_limit_reached FROM token_table WHERE token_key IN ("
            + placeholders
            + ")",
            tokens,
        )
        rows = self.cursor.fetchall()
        return rows

    def load_video_data(self, task_id: str) -> tuple | None:
        # 从数据库中加载视频数据
        self.cursor.execute(
            "SELECT status, video_url, error_msg, auth_xor FROM video_data WHERE task_id = ?",
            (task_id,),
        )
        row = self.cursor.fetchone()
        return row

    def insert_video_data(
        self,
        task_id: str,
        user_id: str,
        nickname: str | None,
        prompt: str,
        image_url: str | None,
        message_id: str,
        token_key: str,
    ) -> None:
        # 记录任务数据
        datetime_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute(
            """
            INSERT INTO video_data (task_id, user_id, nickname, prompt, image_url, status, message_id, auth_xor, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                user_id,
                nickname,
                prompt,
                image_url,
                "Queued",
                message_id,
                token_key,
                datetime_now,
                datetime_now,
            ),
        )
        self.conn.commit()

    def update_poll_finished_data(
        self, task_id: str, status: str, error_msg: str | None
    ) -> None:
        self.cursor.execute(
            """
            UPDATE video_data SET status = ?, error_msg = ?, updated_at = ? WHERE task_id = ?
        """,
            (
                status,
                error_msg,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                task_id,
            ),
        )
        self.conn.commit()

    def update_video_url_data(
        self,
        task_id: str,
        status: str,
        video_url: str | None,
        generation_id: str | None,
        err: str | None,
    ) -> None:
        self.cursor.execute(
            """
            UPDATE video_data SET status = ?, video_url = ?, generation_id = ?, error_msg = ?, updated_at = ? WHERE task_id = ?
        """,
            (
                status,
                video_url,
                generation_id,
                err,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                task_id,
            ),
        )
        self.conn.commit()

    def update_access_token_data(
        self, token_key: str, token_type: str, access_token: str, expire_time: str
    ) -> None:
        self.cursor.execute(
            """
            INSERT OR REPLACE INTO token_table
            (token_key, token_type, access_token, session_token_expire, token_state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token_key, token_type, access_token, expire_time, 1),
        )
        self.conn.commit()

    def update_session_token_state(self, token_key: str, token_type: str) -> None:
        self.cursor.execute(
            """
            INSERT OR REPLACE INTO token_table
            (token_key, token_type, token_state)
            VALUES (?, ?, ?)
            """,
            (token_key, token_type, 0),
        )
        self.conn.commit()

    def close(self) -> None:
        # 关闭连接
        self.conn.commit()
        self.cursor.close()
        self.conn.close()
