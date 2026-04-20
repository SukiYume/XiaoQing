from plugins.xiaoqing_chat.message_parts import build_message_parts_from_template
from plugins.xiaoqing_chat.reply_payload import (
    build_reply_payload_from_parts,
)


def test_build_reply_payload_supports_placeholder_hybrid_flow() -> None:
    payload = build_reply_payload_from_parts(
        build_message_parts_from_template(
            "先看这个[[xc_media_1]]再说一句[[xc_media_2]]收尾",
            [
                {
                    "kind": "emoji",
                    "file_path": "/tmp/emoji.png",
                    "marker": "[表情包：无语]",
                },
                {
                    "kind": "qq_face",
                    "face_id": "14",
                    "marker": "[QQ表情：微笑]",
                },
            ],
        )
    )

    assert len(payload.outbound_batches) == 1
    assert [segment["type"] for segment in payload.outbound_batches[0]] == [
        "text",
        "image",
        "text",
        "face",
        "text",
    ]
    assert payload.display_text == "先看这个[表情包：无语]再说一句[QQ表情：微笑]收尾"
    assert [part["kind"] for part in payload.parts] == [
        "text",
        "emoji",
        "text",
        "qq_face",
        "text",
    ]


def test_build_reply_payload_from_parts_keeps_append_behavior_without_placeholders() -> None:
    payload = build_reply_payload_from_parts(
        build_message_parts_from_template(
            "懂了",
            [
                {
                    "kind": "emoji",
                    "file_path": "/tmp/emoji.png",
                    "marker": "[表情包：无语]",
                },
                {
                    "kind": "qq_face",
                    "face_id": "277",
                    "marker": "[QQ表情：狗头]",
                },
            ],
        )
    )

    assert len(payload.outbound_batches) == 1
    assert [segment["type"] for segment in payload.outbound_batches[0]] == ["text", "image", "face"]
    assert payload.display_text == "懂了[表情包：无语]\n[QQ表情：狗头]"
    assert [part["kind"] for part in payload.parts] == ["text", "emoji", "text", "qq_face"]


def test_build_reply_payload_from_parts_preserves_interleaved_media_order() -> None:
    payload = build_reply_payload_from_parts(
        [
            {"kind": "text", "text": "第一句"},
            {
                "kind": "emoji",
                "file_path": "/tmp/emoji.png",
                "marker": "[表情包：无语]",
            },
            {"kind": "text", "text": "\n第二句"},
            {
                "kind": "qq_face",
                "face_id": "277",
                "marker": "[QQ表情：狗头]",
            },
            {"kind": "text", "text": "\n收尾"},
        ]
    )

    assert len(payload.outbound_batches) == 1
    assert [segment["type"] for segment in payload.outbound_batches[0]] == [
        "text",
        "image",
        "text",
        "face",
        "text",
    ]
    assert payload.display_text == "第一句[表情包：无语]\n第二句[QQ表情：狗头]\n收尾"
    assert [part["kind"] for part in payload.parts] == [
        "text",
        "emoji",
        "text",
        "qq_face",
        "text",
    ]


def test_build_message_parts_from_template_keeps_lossless_media_fields_without_store() -> None:
    parts = build_message_parts_from_template(
        "先看这个[[xc_media_1]]",
        [
            {
                "kind": "emoji",
                "file_path": "/tmp/emoji.png",
                "marker": "[表情包：无语]",
            }
        ],
    )

    assert [part["kind"] for part in parts] == ["text", "emoji"]
    assert parts[1]["file_path"] == "/tmp/emoji.png"


def test_build_reply_payload_from_parts_supports_outbound_image_parts() -> None:
    payload = build_reply_payload_from_parts(
        [
            {"kind": "text", "text": "这是原图"},
            {
                "kind": "image",
                "file_path": "/tmp/photo.png",
                "marker": "[图片：原图]",
                "description": "原图",
            },
        ]
    )

    assert len(payload.outbound_batches) == 1
    assert [segment["type"] for segment in payload.outbound_batches[0]] == ["text", "image"]
    assert payload.display_text == "这是原图[图片：原图]"
    assert [part["kind"] for part in payload.parts] == ["text", "image"]
