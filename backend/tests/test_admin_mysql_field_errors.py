from types import SimpleNamespace

import pymysql
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.api.v1.admin import build_admin_router


def _client_for_mysql_error(error):
    class Admin:
        async def confirm_task(self, task_id, payload):
            raise error

    runtime = SimpleNamespace(require_admin=lambda: Admin())
    app = FastAPI()
    app.include_router(build_admin_router(runtime))
    return TestClient(app, raise_server_exceptions=False)


def test_admin_confirm_reports_mysql_field_length_without_field_details():
    response = _client_for_mysql_error(
        pymysql.err.DataError(1406, "Data too long for column 'application' at row 1")
    ).post("/api/v1/admin/tasks/task-1/confirm", json={})

    assert response.status_code == 422
    assert response.json() == {"detail": "MySQL 字段长度超限"}


def test_admin_confirm_does_not_mislabel_other_mysql_errors_as_field_length():
    response = _client_for_mysql_error(
        pymysql.err.DataError(1366, "Incorrect string value")
    ).post("/api/v1/admin/tasks/task-1/confirm", json={})

    assert response.status_code == 500
    assert "MySQL 字段长度超限" not in response.text
