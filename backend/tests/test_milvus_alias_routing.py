from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import services.vector.milvus.milvus_service as milvus_module
from services.vector.milvus.milvus_service import MilvusService


class _MilvusState:
    def __init__(self, collections=(), aliases=None):
        self.collections = set(collections)
        self.aliases = dict(aliases or {})
        self.missing_alias_collections = set()
        self.alias_errors = {}
        self.created = []
        self.alias_calls = []
        self.alias_scan_calls = []
        self.alias_describe_calls = []
        self.alias_describe_contexts = []
        self.collection_list_calls = 0
        self.rename_errors = set()


class _MilvusException(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _service(monkeypatch, state, *, collection_name="enterprise", alias_name="kc_enterprise"):
    class Collection:
        def __init__(self, name, *, using, schema=None):
            self.name = name
            self.using = using
            self.schema = schema or SimpleNamespace(fields=[])
            self.state = state
            if schema is not None:
                state.collections.add(name)
                state.created.append(name)

        def create_index(self, **_kwargs):
            pass

        def load(self, **_kwargs):
            pass

    class Utility:
        @staticmethod
        def list_collections(*, using):
            state.collection_list_calls += 1
            return sorted(state.collections)

        @staticmethod
        def list_aliases(*, collection_name, using):
            state.alias_scan_calls.append(collection_name)
            if collection_name in state.missing_alias_collections:
                raise _MilvusException(100, "collection not found")
            if collection_name in state.alias_errors:
                raise _MilvusException(*state.alias_errors[collection_name])
            return [alias for alias, target in state.aliases.items() if target == collection_name]

        @staticmethod
        def has_collection(name, *, using):
            return name in state.aliases or (
                name in state.collections and name not in state.missing_alias_collections
            )

        @staticmethod
        def create_alias(*, collection_name, alias, using):
            state.alias_calls.append(("create", collection_name, alias))
            if alias in state.collections or alias in state.aliases:
                raise RuntimeError("alias name already exists")
            state.aliases[alias] = collection_name

        @staticmethod
        def alter_alias(*, collection_name, alias, using):
            state.alias_calls.append(("alter", collection_name, alias))
            state.aliases[alias] = collection_name

        @staticmethod
        def rename_collection(old_collection_name, new_collection_name, *, using):
            if old_collection_name in state.rename_errors:
                raise RuntimeError(f"rename failed: {old_collection_name}")
            if old_collection_name not in state.collections:
                raise RuntimeError(f"collection not found: {old_collection_name}")
            if new_collection_name in state.collections:
                raise RuntimeError(f"collection already exists: {new_collection_name}")
            state.collections.remove(old_collection_name)
            state.collections.add(new_collection_name)
            state.aliases = {
                alias: new_collection_name if target == old_collection_name else target
                for alias, target in state.aliases.items()
            }

    class Handler:
        @staticmethod
        def describe_alias(alias, *, context=None):
            state.alias_describe_calls.append(alias)
            state.alias_describe_contexts.append(context)
            if alias in state.alias_errors:
                raise _MilvusException(*state.alias_errors[alias])
            if alias not in state.aliases:
                raise _MilvusException(100, "alias not found")
            return {"alias": alias, "collection_name": state.aliases[alias]}

    class Connections:
        def connect(self, **_kwargs):
            pass

        def _fetch_handler(self, _using):
            return Handler()

        def _generate_call_context(self, using):
            return f"db-context:{using}"

    monkeypatch.setattr(
        "runtime.config.get_settings",
        lambda: SimpleNamespace(
            milvus_uri="http://milvus",
            milvus_database="kechuang",
            milvus_username=None,
            milvus_password=None,
            database_url="postgresql://fake/platform",
        ),
    )
    monkeypatch.setattr(
        milvus_module,
        "_lazy_import_pymilvus",
        lambda: (Collection, None, None, None, None, None, None, None, Connections(), Utility),
    )

    @contextmanager
    def advisory_lock(_dsn, _key, *, shared):
        yield

    monkeypatch.setattr("services.persistence.sqlalchemy.postgres_advisory_lock", advisory_lock)
    manager = milvus_module.MilvusConnectionManager(
        uri="http://milvus", database="kechuang", username=None, password=None
    )
    service = MilvusService(
        collection_name=collection_name,
        alias_name=alias_name,
        connection_manager=manager,
    )
    service._assert_source_fields = lambda _collection: None
    service._load_collection = lambda _collection: None
    return service


def test_ensure_collection_attaches_stable_alias_to_legacy_collection(monkeypatch):
    state = _MilvusState(collections={"enterprise"})
    service = _service(monkeypatch, state)

    collection = service.ensure_collection()

    assert collection.name == "kc_enterprise"
    assert state.aliases == {"kc_enterprise": "enterprise"}
    assert state.alias_calls == [("create", "enterprise", "kc_enterprise")]
    assert state.created == []


def test_ensure_collection_uses_existing_alias_target(monkeypatch):
    state = _MilvusState(
        collections={"enterprise", "enterprise__generation_1"},
        aliases={"kc_enterprise": "enterprise__generation_1"},
    )
    service = _service(monkeypatch, state)

    collection = service.ensure_collection()

    assert collection.name == "kc_enterprise"
    assert state.alias_calls == []
    assert state.created == []


def test_ensure_alias_does_not_scan_unrelated_collections(monkeypatch):
    state = _MilvusState(collections={"enterprise", "inner"})
    state.missing_alias_collections.add("inner")
    service = _service(monkeypatch, state)

    target = service.ensure_alias()

    assert target == "enterprise"
    assert state.aliases == {"kc_enterprise": "enterprise"}
    assert state.alias_scan_calls == []
    assert state.alias_describe_calls == []


def test_resolve_alias_checks_only_the_configured_alias(monkeypatch):
    state = _MilvusState(
        collections={"enterprise__generation_1", "inner"},
        aliases={"kc_enterprise": "enterprise__generation_1"},
    )
    service = _service(monkeypatch, state)

    assert service._resolve_collection_name() == "enterprise__generation_1"
    assert state.collection_list_calls == 0
    assert state.alias_describe_calls == ["kc_enterprise"]
    assert state.alias_describe_contexts == [f"db-context:{service.connection_alias}"]
    assert state.alias_scan_calls == []


def test_resolve_alias_propagates_used_alias_lookup_errors(monkeypatch):
    state = _MilvusState(
        collections={"enterprise", "enterprise__generation_1"},
        aliases={"kc_enterprise": "enterprise__generation_1"},
    )
    state.alias_errors["kc_enterprise"] = (1, "unexpected failure")
    service = _service(monkeypatch, state)

    with pytest.raises(_MilvusException, match="unexpected failure"):
        service._resolve_collection_name()

    assert state.alias_scan_calls == []
    assert state.alias_describe_calls == ["kc_enterprise"]


def test_new_collection_uses_distinct_physical_name_then_attaches_alias(monkeypatch):
    state = _MilvusState()
    service = _service(monkeypatch, state)
    service._build_schema = lambda: SimpleNamespace(fields=[])

    collection = service.ensure_collection()

    physical_name = state.aliases["kc_enterprise"]
    assert physical_name.startswith("enterprise__g")
    assert collection.name == "kc_enterprise"
    assert state.created == [physical_name]
    assert state.alias_calls == [("create", physical_name, "kc_enterprise")]


def test_alias_name_collision_fails_without_dropping_collection(monkeypatch):
    state = _MilvusState(collections={"enterprise", "kc_enterprise"})
    service = _service(monkeypatch, state)

    with pytest.raises(RuntimeError, match="别名.*物理 Collection"):
        service.ensure_collection()

    assert state.collections == {"enterprise", "kc_enterprise"}
    assert state.alias_calls == []


def test_incompatible_alias_target_schema_fails_without_mutating_existing_data(monkeypatch):
    state = _MilvusState(
        collections={"enterprise__generation_1"},
        aliases={"kc_enterprise": "enterprise__generation_1"},
    )
    service = _service(monkeypatch, state)

    def reject_schema(_collection):
        raise RuntimeError("incompatible schema")

    service._assert_source_fields = reject_schema
    with pytest.raises(RuntimeError, match="incompatible schema"):
        service.ensure_collection()

    assert state.collections == {"enterprise__generation_1"}
    assert state.aliases == {"kc_enterprise": "enterprise__generation_1"}
    assert state.alias_calls == []


def test_shadow_generation_is_physical_and_can_be_activated_without_dropping_old(monkeypatch):
    state = _MilvusState(collections={"enterprise"})
    service = _service(monkeypatch, state)
    service._build_schema = lambda: SimpleNamespace(fields=[])
    service.ensure_collection()

    shadow = service.new_shadow_generation()
    shadow._build_schema = lambda: SimpleNamespace(fields=[])
    shadow.ensure_collection()
    new_target = shadow.collection_name
    assert new_target in state.collections
    assert shadow.alias_name is None

    backup = "enterprise__backup_test"
    switched = service.activate_shadow_generation(
        shadow,
        backup_collection_name=backup,
        displaced_collection_name="enterprise__backup_stale_test",
    )

    assert switched == {"previous": backup, "active": "enterprise"}
    assert state.aliases["kc_enterprise"] == "enterprise"
    assert backup in state.collections
    assert "enterprise" in state.collections
    assert new_target not in state.collections


def test_shadow_promotion_preserves_stale_canonical_collection_as_backup(monkeypatch):
    state = _MilvusState(
        collections={"enterprise", "enterprise__g_old"},
        aliases={"kc_enterprise": "enterprise__g_old"},
    )
    service = _service(monkeypatch, state)
    shadow = service.new_shadow_generation("enterprise__recovery_test")
    state.collections.add(shadow.collection_name)

    switched = service.activate_shadow_generation(
        shadow,
        backup_collection_name="enterprise__backup_active",
        displaced_collection_name="enterprise__backup_stale_old",
    )

    assert switched == {"previous": "enterprise__backup_active", "active": "enterprise"}
    assert state.aliases["kc_enterprise"] == "enterprise"
    assert state.collections == {
        "enterprise",
        "enterprise__backup_active",
        "enterprise__backup_stale_old",
    }


def test_failed_shadow_rename_keeps_old_data_reachable_through_alias(monkeypatch):
    state = _MilvusState(collections={"enterprise"}, aliases={"kc_enterprise": "enterprise"})
    service = _service(monkeypatch, state)
    shadow = service.new_shadow_generation("enterprise__recovery_test")
    state.collections.add(shadow.collection_name)
    state.rename_errors.add(shadow.collection_name)

    with pytest.raises(RuntimeError, match="rename failed"):
        service.activate_shadow_generation(
            shadow,
            backup_collection_name="enterprise__backup_active",
            displaced_collection_name="enterprise__backup_stale_test",
        )

    assert state.aliases["kc_enterprise"] == "enterprise__backup_active"
    assert state.collections == {"enterprise__backup_active", shadow.collection_name}
