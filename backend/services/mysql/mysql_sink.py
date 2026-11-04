"""技术方案和内部拜访记录的 MySQL 读写适配器。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from core.contracts.business_records import ConfirmedBusinessRecordRequest
from core.contracts.mysql_schema import all_mysql_fields, form_contract

from .user_identity import MySQLSettings


class MySQLUnavailableError(RuntimeError):
    """MySQL 建连重试后仍不可用。"""


class MySQLBusinessRecordSink:
    """按字段白名单写入正式业务表；不把业务记录复制到 PostgreSQL。"""

    def __init__(self, settings: MySQLSettings | None = None) -> None:
        self.settings = settings or MySQLSettings.from_env()

    def _connect(self) -> Any:
        try:
            import pymysql
            from pymysql.cursors import DictCursor
            from pymysql.err import OperationalError
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("未安装 PyMySQL，无法写入 MySQL 业务库") from exc
        connect_args = {
            "host": self.settings.host, "port": self.settings.port,
            "user": self.settings.user, "password": self.settings.password,
            "database": self.settings.database, "charset": self.settings.charset,
            "connect_timeout": self.settings.connect_timeout,
            "cursorclass": DictCursor, "autocommit": False,
        }
        last_error: OperationalError | None = None
        for attempt in range(2):
            try:
                return pymysql.connect(**connect_args)
            except OperationalError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.2)
        raise MySQLUnavailableError("MySQL 暂时不可用，请检查数据库服务和连接配置") from last_error

    @staticmethod
    def _values(request: ConfirmedBusinessRecordRequest) -> dict[str, Any]:
        structured_data = dict(request.structured_data)
        storage_fields = structured_data.get("mysql_fields")
        if isinstance(storage_fields, dict):
            structured_data.update(storage_fields)
        nested_fields = structured_data.get("fields")
        if isinstance(nested_fields, dict):
            # 前端编辑后的草稿投影优先，完整 mysql_fields 仅补充未展示字段。
            structured_data.update(nested_fields)
        # 草稿字段与 MySQL 字段同名，只保留当前业务类型允许的物理字段。
        allowed_fields = set(all_mysql_fields(request.record_type))
        values = {
            key: value for key, value in structured_data.items()
            if key in allowed_fields and value not in (None, "")
        }
        if request.record_type == "customer_visit":
            # 这两个是 ai_inner_inquiry 的数据库必填关联列，不属于草稿业务字段。
            if str(request.user_id).isdigit():
                values.setdefault("user_card_id", int(request.user_id))
            if request.thread_id:
                values.setdefault("chat_session_id", request.thread_id)
            values.setdefault(
                "enterprise_name",
                structured_data.get("enterprise_name") or structured_data.get("company"),
            )
            values.setdefault("inquiry_status", 1)
            values.setdefault("is_deleted", 0)
            values = {key: value for key, value in values.items() if value not in (None, "")}
        elif request.raw_text and "contect" in allowed_fields:
            values.setdefault("contect", request.raw_text)
        if request.record_type == "achievement":
            values.pop("temp_same_title", None)
            if values:
                values["temp_same_title"] = None
        return values

    @staticmethod
    def _timestamp_values(record_type: str, now: datetime) -> dict[str, datetime]:
        return {
            field: now
            for field in ("create_time", "created_time", "update_time")
            if field in all_mysql_fields(record_type)
        }

    def save(self, request: ConfirmedBusinessRecordRequest) -> dict[str, Any]:
        if not request.confirmed:
            raise ValueError("只有确认后的数据才允许写入业务系统")
        contract = form_contract(request.record_type)
        values = self._values(request)
        if not values:
            raise ValueError("没有可写入 MySQL 的业务字段")
        table = str(contract["table"])
        # 拜访记录只有保存后才进入 MySQL；保存即为私密库中的有效记录。
        now = datetime.now()
        insert_values = {**values, **self._timestamp_values(request.record_type, now)}
        columns = list(insert_values)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO `{table}` ({', '.join(f'`{key}`' for key in columns)}) VALUES ({placeholders})"
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                # 技术方案以来源草稿作为业务幂等键；重复发布更新原记录，避免同一草稿重复插入。
                if request.record_type == "solution" and request.draft_id:
                    cursor.execute(
                        f"SELECT `id` FROM `{table}` WHERE `src_draft_id` = %s LIMIT 1",
                        [request.draft_id],
                    )
                    existing = cursor.fetchone()
                    if existing:
                        update_values = {**values, **self._timestamp_values(request.record_type, now)}
                        assignments = ", ".join(f"`{key}` = %s" for key in update_values)
                        cursor.execute(
                            f"UPDATE `{table}` SET {assignments} WHERE `id` = %s",
                            [*update_values.values(), existing["id"]],
                        )
                        connection.commit()
                        return {
                            "business_record_id": str(existing["id"]), "storage": "mysql",
                            "status": "UPDATED", "record_type": request.record_type,
                            "user_id": request.user_id, "structured_data": values,
                        }
                if request.record_type == "customer_visit" and values.get("user_card_id") is not None and values.get("chat_session_id"):
                    cursor.execute(
                        f"SELECT `id` FROM `{table}` WHERE `user_card_id` = %s AND `chat_session_id` = %s AND `is_deleted` = 0 LIMIT 1",
                        [values["user_card_id"], values["chat_session_id"]],
                    )
                    existing = cursor.fetchone()
                    if existing:
                        update_values = {**values, **self._timestamp_values(request.record_type, now)}
                        assignments = ", ".join(f"`{key}` = %s" for key in update_values)
                        cursor.execute(
                            f"UPDATE `{table}` SET {assignments} WHERE `id` = %s AND `is_deleted` = 0",
                            [*update_values.values(), existing["id"]],
                        )
                        connection.commit()
                        return {
                            "business_record_id": str(existing["id"]), "storage": "mysql",
                            "status": "UPDATED", "record_type": request.record_type,
                            "user_id": request.user_id, "structured_data": update_values,
                        }
                cursor.execute(sql, [insert_values[key] for key in columns])
                record_id = cursor.lastrowid
            connection.commit()
            return {
                "business_record_id": str(record_id), "storage": "mysql",
                "status": "SAVED", "record_type": request.record_type,
                "user_id": request.user_id, "structured_data": insert_values,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def find_by_id(self, *, record_type: str, record_id: str) -> str | None:
        """确认草稿关联的 MySQL 主键仍然存在。"""
        table = str(form_contract(record_type)["table"])
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT `id` FROM `{table}` WHERE `id` = %s LIMIT 1", [record_id])
                row = cursor.fetchone()
            return str(row["id"]) if row else None
        finally:
            connection.close()

    def update(self, *, record_type: str, record_id: str,
               structured_data: dict[str, Any],
               clear_fields: list[str] | None = None) -> dict[str, Any]:
        """按已发布业务主键更新字段，不创建第二条业务记录。"""
        contract = form_contract(record_type)
        table = str(contract["table"])
        values = self._values(ConfirmedBusinessRecordRequest(
            user_id="system", record_type=record_type, confirmed=True,
            confirmed_by="system", structured_data=structured_data,
        ))
        allowed_fields = set(all_mysql_fields(record_type))
        explicit_clears = {
            field for field in (clear_fields or []) if field in allowed_fields
        }
        values.update({field: None for field in explicit_clears})
        if not values:
            raise ValueError("没有可更新的 MySQL 业务字段")
        update_values = {**values, **self._timestamp_values(record_type, datetime.now())}
        assignments = ", ".join(f"`{key}` = %s" for key in update_values)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE `{table}` SET {assignments} WHERE `id` = %s",
                    [*update_values.values(), record_id],
                )
                if not cursor.rowcount:
                    raise ValueError(f"MySQL 业务记录不存在：{record_type}/{record_id}")
            connection.commit()
            return {"business_record_id": str(record_id), "storage": "mysql",
                    "status": "UPDATED", "record_type": record_type,
                    "structured_data": update_values}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list(self, *, user_id: str, record_type: str | None = None) -> list[dict[str, Any]]:
        types = [record_type] if record_type else ["customer_visit"]
        results: list[dict[str, Any]] = []
        connection = self._connect()
        try:
            for item in types:
                if item != "customer_visit":
                    continue
                table = str(form_contract(item)["table"])
                fields = list(all_mysql_fields(item))
                where = " WHERE `user_card_id` = %s AND `inquiry_status` = 1 AND `is_deleted` = 0" if item == "customer_visit" and str(user_id).isdigit() else ""
                params: list[Any] = [int(user_id)] if where else []
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT {', '.join(f'`{field}`' for field in fields)} FROM `{table}`{where} ORDER BY `id` DESC LIMIT 200", params)
                    for row in cursor.fetchall() or []:
                        results.append({"business_record_id": str(row.get("id")), "storage": "mysql", "record_type": item, "user_id": user_id, "structured_data": row})
            return results
        finally:
            connection.close()

    def delete(self, *, record_type: str, record_id: str, user_id: str) -> dict[str, Any]:
        if record_type not in {"demand", "achievement", "solution", "customer_visit"}:
            raise ValueError(f"不支持删除 MySQL 业务记录：{record_type}")
        table = str(form_contract(record_type)["table"])
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                if record_type == "customer_visit":
                    if not str(user_id).isdigit():
                        raise ValueError("拜访记录删除需要内部用户身份")
                    cursor.execute(
                        f"UPDATE `{table}` SET `is_deleted` = 1, `update_time` = %s WHERE `id` = %s AND `user_card_id` = %s AND `is_deleted` = 0",
                        [datetime.now(), record_id, int(user_id)],
                    )
                else:
                    cursor.execute(f"DELETE FROM `{table}` WHERE `id` = %s", [record_id])
                if not cursor.rowcount:
                    raise ValueError("MySQL 业务记录不存在或无权删除")
            connection.commit()
            return {"business_record_id": str(record_id), "record_type": record_type, "status": "DELETED"}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

