from plugins.xiaoqing_chat.message_parts import (
    build_message_parts_from_template,
    merge_reply_media_parts,
)
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
        "emoji",
        "text",
        "face",
        "text",
    ]
    assert payload.outbound_batches[0][1]["data"]["summary"] == "[表情包：无语]"
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
    assert [segment["type"] for segment in payload.outbound_batches[0]] == ["text", "emoji", "face"]
    assert payload.outbound_batches[0][1]["data"]["file"].endswith("/tmp/emoji.png")
    assert payload.display_text == "懂了[表情包：无语]\n[QQ表情：狗头]"
    assert [part["kind"] for part in payload.parts] == ["text", "emoji", "text", "qq_face"]


def test_merge_reply_media_keeps_distinct_anonymous_images() -> None:
    parts = merge_reply_media_parts(
        [
            {"kind": "text", "text": "第一张"},
            {"kind": "image", "marker": "[图片：一]", "file_path": "/tmp/one.png"},
            {"kind": "text", "text": "第二张"},
        ],
        [{"kind": "image", "marker": "[图片：二]", "file_path": "/tmp/two.png"}],
    )

    images = [part for part in parts if part["kind"] == "image"]
    assert [item["marker"] for item in images] == ["[图片：一]", "[图片：二]"]


def test_merge_reply_media_updates_matching_stable_identity() -> None:
    parts = merge_reply_media_parts(
        [{"kind": "emoji", "media_hash": "same", "marker": "[表情包：旧]"}],
        [
            {
                "kind": "emoji",
                "media_hash": "same",
                "marker": "[表情包：新]",
                "file_path": "/tmp/new.png",
            }
        ],
    )

    assert parts == (
        {
            "kind": "emoji",
            "media_hash": "same",
            "marker": "[表情包：新]",
            "file_path": "/tmp/new.png",
        },
    )


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
        "emoji",
        "text",
        "face",
        "text",
    ]
    assert payload.outbound_batches[0][1]["data"]["file"].endswith("/tmp/emoji.png")
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
    assert "sub_type" not in payload.outbound_batches[0][1]["data"]
    assert payload.display_text == "这是原图[图片：原图]"
    assert [part["kind"] for part in payload.parts] == ["text", "image"]
