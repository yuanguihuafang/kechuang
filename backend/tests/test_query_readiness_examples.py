from services.application.draft_builder import build_draft


def test_draft_title_ignores_conversation_lines():
    draft = build_draft(
        "demand",
        "user: 工业机器人应用：推广多工种多岗位机器换人",
        {"text": "assistant: 请问您有什么具体需求呢？\nuser: 需求类型：创新研发"},
    )

    assert draft["title"] == "待完善需求"
    assert draft["fields"]["title"] == ""


def test_draft_title_keeps_explicit_title_label():
    draft = build_draft(
        "demand",
        "需求标题：工业机器人柔性生产线改造",
        {"text": "需求描述：希望提升多工种多岗位机器换人能力。"},
    )

    assert draft["title"] == "工业机器人柔性生产线改造"
    assert draft["fields"]["title"] == "工业机器人柔性生产线改造"


def test_solution_draft_uses_first_parsed_image_as_cover():
    draft = build_draft(
        "solution",
        "",
        {
            "text": "方案内容",
            "image_candidates": [
                {
                    "url": "/api/v1/perception/images/doc-1/image-1",
                    "relativePath": "images/one.jpg",
                },
                {
                    "url": "/api/v1/perception/images/doc-1/image-2",
                    "relativePath": "images/two.jpg",
                },
            ],
        },
    )

    assert draft["fields"]["first_img_url"] == "/api/v1/perception/images/doc-1/image-1"
    assert draft["mysql_fields"]["first_img_url"] == "/api/v1/perception/images/doc-1/image-1"


def test_solution_draft_never_uses_local_relative_path_as_cover_url():
    draft = build_draft("solution", "", {
        "text": "方案内容",
        "document_id": "doc-1",
        "image_candidates": [{"relativePath": "images/cover.png"}],
    })

    assert "first_img_url" not in draft["fields"]
