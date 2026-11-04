from services.mysql.mysql_sink import MySQLBusinessRecordSink
from services.mysql.user_identity import MySQLUserIdentityReader


def test_identity_reader_uses_dedicated_database_settings(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "legacy-host")
    monkeypatch.setenv("MYSQL_PORT", "3307")
    monkeypatch.setenv("MYSQL_USER", "legacy-user")
    monkeypatch.setenv("MYSQL_PASSWORD", "legacy-password")
    monkeypatch.setenv("MYSQL_DATABASE", "legacy-database")

    monkeypatch.setenv("MYSQL_IDENTITY_HOST", "identity-host")
    monkeypatch.setenv("MYSQL_IDENTITY_PORT", "3308")
    monkeypatch.setenv("MYSQL_IDENTITY_USER", "identity-user")
    monkeypatch.setenv("MYSQL_IDENTITY_PASSWORD", "identity-password")
    monkeypatch.setenv("MYSQL_IDENTITY_DATABASE", "identity-database")

    identity_settings = MySQLUserIdentityReader().settings
    business_settings = MySQLBusinessRecordSink().settings

    assert identity_settings.host == "identity-host"
    assert identity_settings.port == 3308
    assert identity_settings.user == "identity-user"
    assert identity_settings.password == "identity-password"
    assert identity_settings.database == "identity-database"

    assert business_settings.host == "legacy-host"
    assert business_settings.port == 3307
    assert business_settings.user == "legacy-user"
    assert business_settings.password == "legacy-password"
    assert business_settings.database == "legacy-database"


def test_achievement_write_keeps_temporary_duplicate_marker_null():
    from services.mysql.mysql_sink import MySQLBusinessRecordSink
    from core.contracts.business_records import ConfirmedBusinessRecordRequest
    request = ConfirmedBusinessRecordRequest(
        user_id="admin", confirmed=True, confirmed_by="admin", record_type="achievement",
        structured_data={"title": "成果", "temp_same_title": "不应保存", "fields": {"temp_same_title": "也不保存"}},
    )
    values = MySQLBusinessRecordSink._values(request)
    assert values["title"] == "成果"
    assert values["temp_same_title"] is None
