import pytest

from bot.utils.resource_library_markdown import (
    markdown_diff,
    merge_resource_markdown,
    parse_resource_markdown,
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
