import copy
from ac_document.mineru_headings import restore_title_labels


def sample():
    items = [
        {
            "type": "image",
            "bbox": [115, 134, 163, 194],
            "page_idx": 0,
            "img_path": "one.jpg",
        },
        {
            "type": "text",
            "text_level": 1,
            "text": "A title",
            "bbox": [394, 133, 798, 200],
            "page_idx": 0,
        },
    ]
    block = {
        "type": "header",
        "bbox": [22, 82, 152, 101],
        "lines": [{"spans": [{"type": "text", "content": "PART"}]}],
    }
    return items, {
        "pdf_info": [
            {"page_idx": 0, "page_size": [595, 842], "discarded_blocks": [block]}
        ]
    }


def test_restore_exact_title_label_preserves_inputs_and_image():
    items, middle = sample()
    original = copy.deepcopy((items, middle))
    result = restore_title_labels(items, middle)
    assert result[0]["text"] == "PART"
    assert result[0]["page_idx"] == 0
    assert result[1:] == items
    assert (items, middle) == original
    assert restore_title_labels(result, middle) == result


def test_repeated_running_header_is_not_restored():
    items, middle = sample()
    second = copy.deepcopy(middle["pdf_info"][0])
    second["page_idx"] = 1
    middle["pdf_info"].append(second)
    assert restore_title_labels(items, middle) == items


def test_no_title_or_large_or_distant_image_leaves_evidence_alone():
    for variant in ("title", "large", "far", "numeric"):
        items, middle = sample()
        if variant == "title":
            items[1]["text_level"] = 2
        if variant == "large":
            items[0]["bbox"][2] = 500
        if variant == "far":
            items[0]["bbox"] = [115, 400, 163, 460]
        if variant == "numeric":
            middle["pdf_info"][0]["discarded_blocks"][0]["lines"][0]["spans"][0][
                "content"
            ] = "12"
        assert restore_title_labels(items, middle) == items


def test_malformed_optional_evidence_does_not_crash():
    items, middle = sample()
    middle["pdf_info"][0]["discarded_blocks"] = None
    assert restore_title_labels(items, middle) == items
    items, middle = sample()
    items.extend([None, {"type": "text", "text": None, "page_idx": 0}])
    assert restore_title_labels(items, middle)[-2:] == items[-2:]
