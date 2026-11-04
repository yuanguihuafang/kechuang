from assistant.application.materials import _elapsed_until_first_token, _material_token_event


def test_material_summary_elapsed_stops_at_first_token():
    assert _elapsed_until_first_token(10.0, 10.75, 12.0) == 750.0


def test_material_summary_elapsed_falls_back_to_completion_without_tokens():
    assert _elapsed_until_first_token(10.0, None, 12.0) == 2000.0


def test_material_sse_preserves_each_model_token_without_artificial_subdivision():
    assert _material_token_event("answer", "这是一个完整的模型 token。") == {
        "event": "token",
        "phase": "answer",
        "text": "这是一个完整的模型 token。",
    }
