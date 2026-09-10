import copy

from ac_document.mineru_headers import classify_running_headers


def title(text, page, y, level=2):
    return dict(type="text", text=text, page_idx=page, bbox=[220, y, 520, y + 15], text_level=level)


def test_repeated_margin_titles_filtered_body_and_evidence_preserved():
    items = [title("1.4 Vectors", 14, 99), title("1.4■VECTORS", 14, 596),
             title("1.4 Vectors", 16, 100)]
    original = copy.deepcopy(items)
    result = classify_running_headers(items)
    assert [x["type"] for x in result] == ["header", "text", "header"]
    assert result[1] == items[1]
    assert items == original
    assert result[0]["original_type"] == "text"
    assert classify_running_headers(result) == result


def test_single_header_requires_established_margin_pattern_and_body_on_same_page():
    items = []
    for n, page in [(4, 14), (6, 20)]:
        items += [title(f"1.{n} Title", page, 99), title(f"1.{n} TITLE", page, 200),
                  title(f"1.{n} Title", page + 2, 98)]
    items += [title("1.8 Maxwell", 28, 99), title("1.8 MAXWELL", 28, 243)]
    assert classify_running_headers(items)[-2]["type"] == "header"
    assert classify_running_headers(items[-2:]) == items[-2:]
    items[-1]["page_idx"] = 30
    assert classify_running_headers(items)[-2]["type"] == "text"


def test_ambiguous_titles_and_invalid_geometry_are_not_filtered():
    variants = [
        [title("1.4 Vectors", 0, 99), title("1.4 Vectors", 2, 99)],
        [title("1.4 Vectors", 0, 300), title("1.4 Vectors", 2, 300)],
        [title("1.4 Vectors", 0, 99), title("1.4 Other", 2, 99), title("1.4 Vectors", 0, 200)],
        [title("CHAPTER", 0, 99), title("CHAPTER", 2, 99)],
    ]
    for items in variants:
        assert classify_running_headers(items) == items
    items = [title("1.4 Vectors", 0, 99), title("1.4 Vectors", 2, 99), title("1.4 VECTORS", 0, 200)]
    items[0]["bbox"] = [0, float("nan"), 1, 100]
    assert classify_running_headers(items)[1]["type"] == "text"
