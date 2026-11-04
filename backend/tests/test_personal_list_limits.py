import inspect

from assistant.api.v1.documents.catalog import build_documents_router
from assistant.api.v1.drafts.routes import build_draft_router
from assistant.api.v1.identity.routes import build_identity_router


def _endpoint(router, path):
    return next(route.endpoint for route in router.routes if route.path == path)


def _query_default(endpoint, name):
    return inspect.signature(endpoint).parameters[name].default.default


def test_personal_center_list_defaults_are_50():
    documents = build_documents_router(object(), object())
    drafts = build_draft_router(object(), object())
    identity = build_identity_router(object())

    assert _query_default(_endpoint(documents, "/api/v1/me/documents"), "limit") == 50
    assert _query_default(_endpoint(drafts, "/api/v1/me/drafts"), "limit") == 50
    assert _query_default(_endpoint(identity, "/api/v1/me/match-results"), "limit") == 50
    assert _query_default(_endpoint(identity, "/api/v1/me/visit-records"), "limit") == 50
