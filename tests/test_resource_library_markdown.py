import pytest

from bot.utils.resource_library_markdown import (
    MAX_DISCORD_MESSAGE_CHARS,
    MAX_MARKDOWN_CHARS,
    markdown_diff,
    merge_resource_markdown,
    parse_resource_markdown,
    split_resource_markdown,
    utf16_length,
    validate_markdown_links,
)


def _edits(markdown):
    parsed = parse_resource_markdown(markdown)
    return [
        {
            "id": section["id"],
            "title": section["title"],
            "resources": [dict(resource) for resource in section["resources"]],
        }
        for section in parsed["sections"]
    ]


def test_round_trip_preserves_unedited_markdown_exactly():
    source = (
        "# 演算法\r\n"
        "\r\n"
        "備註原文，不應被改寫。\r\n"
        "\r\n"
        "- [YTP 少年圖靈計劃](https://www.tw-ytp.org/)\r\n"
        "- [成大邀請賽](https://hspc.csie.ncku.edu.tw/)  <!-- 保留註解 -->\r\n"
        "\r\n"
        "## 其他資源\r\n"
        "- [文件](https://example.org/docs)\r\n"
    )

    assert merge_resource_markdown(source, _edits(source)) == source


def test_edits_only_change_selected_heading_and_resource_lines():
    source = "# 原分類  \n\n保留原始說明\n- [舊名稱](https://example.org/old)  <!-- note -->\n"
    edits = _edits(source)
    edits[0]["title"] = "新分類"
    edits[0]["resources"][0]["name"] = "新名稱"
    edits[0]["resources"][0]["url"] = "https://example.org/new"

    assert merge_resource_markdown(source, edits) == (
        "# 新分類  \n\n保留原始說明\n- [新名稱](https://example.org/new)  <!-- note -->\n"
    )


def test_resource_order_add_and_delete_keep_other_markdown():
    source = "# Links\n\n說明文字\n- [A](https://a.example/)\n- [B](https://b.example/)\n"
    edits = _edits(source)
    first, second = edits[0]["resources"]
    edits[0]["resources"] = [
        second,
        {"id": None, "name": "New", "url": "https://new.example/"},
    ]

    assert merge_resource_markdown(source, edits) == (
        "# Links\n\n說明文字\n- [B](https://b.example/)\n"
        "- [New](https://new.example/)\n"
    )
    assert first["id"] not in {item.get("id") for item in edits[0]["resources"]}


def test_add_category_appends_without_reformatting_existing_section():
    source = "# One\n- [A](https://a.example/)\n"
    edits = _edits(source)
    edits.append(
        {
            "id": None,
            "title": "Two",
            "resources": [{"id": None, "name": "B", "url": "https://b.example/"}],
        }
    )

    assert merge_resource_markdown(source, edits) == (
        "# One\n- [A](https://a.example/)\n\n# Two\n- [B](https://b.example/)\n"
    )


def test_unsafe_url_and_reordered_categories_are_rejected():
    source = "# One\n- [A](https://a.example/)\n\n# Two\n- [B](https://b.example/)\n"
    edits = _edits(source)
    edits[0]["resources"][0]["url"] = "javascript:alert(1)"
    with pytest.raises(ValueError, match="不安全的協定"):
        merge_resource_markdown(source, edits)

    edits = _edits(source)[::-1]
    with pytest.raises(ValueError, match="分類順序不可變更"):
        merge_resource_markdown(source, edits)


def test_unified_diff_is_empty_for_unchanged_content():
    assert markdown_diff("# A\n", "# A\n") == ""
    assert "-# A" in markdown_diff("# A\n", "# B\n")
    assert "+# B" in markdown_diff("# A\n", "# B\n")


@pytest.mark.parametrize(
    "content_md",
    [
        "[unsafe](javascript:alert(1))",
        "[outer [inner]](javascript:alert(1))",
        "[unsafe]: data:text/html,hello",
        "[unsafe](javascript&#58;alert(1))",
        "[unsafe](javascript\\:alert(1))",
        '<a href="&#x6a;avascript:alert(1)">unsafe</a>',
        '<a href="file:///etc/passwd">unsafe</a>',
        "[unsafe](blob:https://example.org/resource)",
        "[empty]()",
    ],
)
def test_markdown_rejects_empty_and_unsafe_link_destinations(content_md):
    with pytest.raises(ValueError, match="資源 URL"):
        validate_markdown_links(content_md)


@pytest.mark.parametrize(
    "url",
    ["example.org/resource", "/docs", "mailto:user@example.org", "ftp://example.org/file", "http:resource"],
)
def test_markdown_accepts_non_http_resource_urls(url):
    validate_markdown_links(f"[resource]({url})")


def test_structured_merge_accepts_relative_resource_urls():
    source = "# One\n- [A](https://a.example/)\n"
    edits = _edits(source)
    edits[0]["resources"][0]["url"] = "/resources/a"

    assert merge_resource_markdown(source, edits) == "# One\n- [A](/resources/a)\n"


def test_markdown_allows_free_form_text_and_code_examples():
    content_md = (
        "一般文字、**粗體**、引用與表格都可保留。\n\n"
        "[安全連結](https://example.org/docs)\n\n"
        "```markdown\n[範例](javascript:alert(1))\n```\n"
        "`[inline example](data:text/html,unsafe)`\n"
    )

    validate_markdown_links(content_md)


def test_utf16_length_matches_discord_javascript_string_units():
    assert utf16_length("a漢😀") == 4
    assert utf16_length("👩‍👩‍👧‍👦") == 11


@pytest.mark.parametrize(
    ("source", "expected_lengths"),
    [
        ("x" * 2000, [2000]),
        ("x" * 2001, [2000, 1]),
        ("漢" * 2001, [2000, 1]),
        ("😀" * 1000 + "x", [2000, 1]),
        ("😀" * 1001, [2000, 2]),
        ("x" * MAX_MARKDOWN_CHARS, [MAX_DISCORD_MESSAGE_CHARS] * 10),
    ],
    ids=["exact-limit", "over-limit", "cjk", "non-bmp", "emoji-boundary", "markdown-cap"],
)
def test_split_respects_utf16_limits_and_preserves_all_content(source, expected_lengths):
    parts = split_resource_markdown(source)

    assert [utf16_length(part) for part in parts] == expected_lengths
    assert all(part and utf16_length(part) <= MAX_DISCORD_MESSAGE_CHARS for part in parts)
    assert "".join(parts) == source


def test_split_prefers_paragraphs_and_keeps_bounded_markdown_constructs_whole():
    paragraph_source = "a" * 1990 + "\n\n" + "b" * 30
    paragraph_parts = split_resource_markdown(paragraph_source)
    assert paragraph_parts[0] == "a" * 1990 + "\n\n"

    fence = "```python\nprint('ok')\n```\n"
    fenced_source = "x" * 1970 + "\n" + fence + "y" * 30
    fenced_parts = split_resource_markdown(fenced_source)
    assert "".join(fenced_parts) == fenced_source
    assert fence in fenced_parts[0]
    assert not any(part.startswith("print") for part in fenced_parts)

    resource_line = "- [Example](https://example.org/resource)\n"
    linked_source = "x" * 1980 + "\n" + resource_line + "y" * 30
    linked_parts = split_resource_markdown(linked_source)
    assert "".join(linked_parts) == linked_source
    assert resource_line in linked_parts[1]


def test_split_preserves_crlf_and_avoids_splitting_combining_and_emoji_sequences():
    crlf_source = "a" * 1999 + "\r\n" + "b"
    crlf_parts = split_resource_markdown(crlf_source)
    assert "".join(crlf_parts) == crlf_source
    assert all(not part.endswith("\r") for part in crlf_parts)
    assert all(not part.startswith("\n") for part in crlf_parts[1:])

    family = "👨‍👩‍👧‍👦"
    combined = "é"
    source = "x" * 1998 + family + combined + "z"
    parts = split_resource_markdown(source)
    assert "".join(parts) == source
    assert all(not part.endswith("‍") for part in parts)
    assert all(not part.startswith("‍") for part in parts[1:])
    assert all(not part.startswith("́") for part in parts[1:])


def test_split_long_lines_and_oversized_constructs_without_rewriting():
    long_line = "word " * 1000
    oversized_fence = "```text\n" + "c" * 2500 + "\n```\n"
    for source in (long_line, oversized_fence):
        parts = split_resource_markdown(source)
        assert "".join(parts) == source
        assert all(part and utf16_length(part) <= MAX_DISCORD_MESSAGE_CHARS for part in parts)


def test_split_reserves_nonblank_text_around_whitespace_windows():
    cases = [
        ("x" * 2000 + "\n", ["x" * 1999, "x\n"]),
        ("x" * 1999 + "\r\n", ["x" * 1998, "x\r\n"]),
        ("x" * 2000 + " " * 100, ["x" * 1999, "x" + " " * 100]),
        ("x" * 2000 + " " * 1999, ["x" * 1999, "x" + " " * 1999]),
    ]
    for source, expected in cases:
        parts = split_resource_markdown(source)
        assert parts == expected
        assert all(part.strip() and utf16_length(part) <= MAX_DISCORD_MESSAGE_CHARS for part in parts)
        assert "".join(parts) == source

    blank_lines = "a" * 1999 + "\n" * 2001 + "b"
    blank_line_parts = split_resource_markdown(blank_lines)
    assert all(part.strip() for part in blank_line_parts)
    assert all(utf16_length(part) <= MAX_DISCORD_MESSAGE_CHARS for part in blank_line_parts)
    assert "".join(blank_line_parts) == blank_lines

    long_gap = "x\n" + " " * 2200 + "y"
    gap_parts = split_resource_markdown(long_gap)
    assert gap_parts[0].startswith("x\n")
    assert utf16_length(gap_parts[0]) == MAX_DISCORD_MESSAGE_CHARS
    assert gap_parts[1] == " " * 202 + "y"
    assert "".join(gap_parts) == long_gap


def test_split_rejects_unpublishable_whitespace_and_invalid_bounds():
    with pytest.raises(ValueError, match="非空白"):
        split_resource_markdown(" " * (MAX_DISCORD_MESSAGE_CHARS + 1) + "text")
    with pytest.raises(ValueError, match="非空白"):
        split_resource_markdown("x" + " " * 4001)
    with pytest.raises(ValueError, match="非空白"):
        split_resource_markdown(" \n\t")
    with pytest.raises(ValueError, match="上限"):
        split_resource_markdown("valid", max_chars=0)
    with pytest.raises(ValueError, match="上限"):
        split_resource_markdown("valid", max_chars=MAX_MARKDOWN_CHARS + 1)
    with pytest.raises(ValueError, match="不可超過"):
        split_resource_markdown("x" * (MAX_MARKDOWN_CHARS + 1))
    with pytest.raises(ValueError, match="代理字元"):
        utf16_length("\ud800")
    with pytest.raises(ValueError, match="代理字元"):
        split_resource_markdown("\udfff")
    with pytest.raises(ValueError, match="不足"):
        split_resource_markdown("😀", max_chars=1)


def test_split_empty_markdown_returns_no_messages():
    assert split_resource_markdown("") == []
