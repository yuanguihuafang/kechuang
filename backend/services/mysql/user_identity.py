"""从现有 MySQL 用户表读取身份信息。

平台读取用户 ID、内外部标记，以及可选的用户类型和所属公司；不写入用户表。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - 由运行环境依赖负责提供
    pymysql = None
    DictCursor = Any



@dataclass(frozen=True)
class MySQLSettings:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "kechuang"
    connect_timeout: int = 10
    charset: str = "utf8mb4"

    @classmethod
    def from_env(cls) -> MySQLSettings:
        return cls._from_env_prefix("MYSQL_")

    @classmethod
    def from_identity_env(cls) -> MySQLSettings:
        """读取独立身份库配置，不回退到业务库配置。"""

        return cls._from_env_prefix("MYSQL_IDENTITY_")

    @classmethod
    def _from_env_prefix(cls, prefix: str) -> MySQLSettings:
        return cls(
            host=os.getenv(f"{prefix}HOST", "127.0.0.1"),
            port=int(os.getenv(f"{prefix}PORT", "3306")),
            user=os.getenv(f"{prefix}USER", "root"),
            password=os.getenv(f"{prefix}PASSWORD", ""),
            database=os.getenv(f"{prefix}DATABASE", "kechuang"),
            connect_timeout=int(os.getenv(f"{prefix}CONNECT_TIMEOUT_SECONDS", "10")),
        )


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    name: str | None
    is_inner_user: bool
    user_type: str | None = None
    company: str | None = None
    phone: str | None = None
    source: str = "mysql.xp_user_card_simple"

    def as_dict(self) -> dict[str, Any]:
        result = {
            "user_id": self.user_id,
            "is_inner_user": self.is_inner_user,
            "source": self.source,
        }
        if self.name is not None:
            result["name"] = self.name
        if self.user_type is not None:
            result["user_type"] = self.user_type
        if self.company is not None:
            result["company"] = self.company
        if self.phone is not None:
            result["phone"] = self.phone
        return result


class MySQLUserIdentityReader:
    """只读用户表，不参与业务资料入库。"""

    table_name = "xp_user_card_simple"

    def __init__(self, settings: MySQLSettings | None = None) -> None:
        self.settings = settings or MySQLSettings.from_identity_env()

    def _connect(self):
        if pymysql is None:
            raise RuntimeError("未安装 PyMySQL，请安装项目依赖后再启用 MySQL 身份读取")
        return pymysql.connect(
            host=self.settings.host,
            port=self.settings.port,
            user=self.settings.user,
            password=self.settings.password,
            database=self.settings.database,
            charset=self.settings.charset,
            connect_timeout=self.settings.connect_timeout,
            cursorclass=DictCursor,
            autocommit=True,
        )

    def get(self, user_id: str) -> UserIdentity | None:
        normalized_id = str(user_id or "").strip()
        if not normalized_id:
            return None

        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT `userid`, `name`, `is_inner_user`, `user_type`, `company`, "
                    "COALESCE(NULLIF(`telphone`, ''), NULLIF(`linkphone`, '')) AS `phone` "
                    "FROM `xp_user_card_simple` WHERE `userid` = %s LIMIT 1",
                    [normalized_id],
                )
                row = cursor.fetchone()
            if row is None:
                return None
            is_inner = str(row.get("is_inner_user") or "0") == "1"
            return UserIdentity(
                user_id=str(row["userid"]),
                name=str(row["name"]).strip() if row.get("name") else None,
                is_inner_user=is_inner,
                user_type=str(row["user_type"]).strip() if row.get("user_type") else None,
                company=str(row["company"]).strip() if row.get("company") else None,
                phone=str(row["phone"]).strip() if row.get("phone") else None,
            )
        finally:
            connection.close()

    def list_test_users(self, limit: int = 100) -> list[dict[str, Any]]:
        """仅为本地测试下拉框返回用户 ID 和内外部标记。"""

        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT `userid`, `name`, `is_inner_user`, `user_type`, `company`, "
                    "COALESCE(NULLIF(`telphone`, ''), NULLIF(`linkphone`, '')) AS `phone` "
                    "FROM `xp_user_card_simple` "
                    "WHERE `userid` IS NOT NULL AND `userid` <> '' "
                    "ORDER BY `id` DESC LIMIT %s",
                    [max(1, min(int(limit), 200))],
                )
                rows = cursor.fetchall()
            result = []
            for row in rows:
                item = {
                    "user_id": str(row["userid"]),
                    "is_inner_user": str(row.get("is_inner_user") or "0") == "1",
                    "source": "mysql.xp_user_card_simple",
                }
                if row.get("name"):
                    item["name"] = str(row["name"]).strip()
                if row.get("user_type"):
                    item["user_type"] = str(row["user_type"]).strip()
                if row.get("company"):
                    item["company"] = str(row["company"]).strip()
                if row.get("phone"):
                    item["phone"] = str(row["phone"]).strip()
                result.append(item)
            return result
        finally:
            connection.close()


def mysql_identity_enabled() -> bool:
    return os.getenv("MYSQL_IDENTITY_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }
