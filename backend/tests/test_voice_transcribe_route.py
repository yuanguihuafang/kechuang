from pathlib import Path


def test_standalone_voice_transcribe_route_is_not_registered():
    router_source = (Path(__file__).parents[1] / "assistant" / "api" / "v1" / "router.py").read_text(encoding="utf-8")

    assert "build_media_router" not in router_source
