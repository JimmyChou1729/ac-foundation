"""Replay bounded, reviewed inline repairs without changing PDF source anchors."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString
from jsonschema import Draft202012Validator

from .pdf_source import PDFSourceBundleError


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


INLINE_PROPOSAL_SCHEMA = _object({
    "proposal_id": {"type": "string", "minLength": 1, "maxLength": 80},
    "anchor_id": {"type": "string", "minLength": 1},
    "before_html": {"type": "string", "minLength": 1, "maxLength": 4000},
    "after": {"type": "array", "minItems": 1, "maxItems": 20, "items": _object({
        "kind": {"enum": ["text", "math", "sup"]},
        "value": {"type": "string", "minLength": 1, "maxLength": 4000}})},
    "reason": {"type": "string", "minLength": 1},
})
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _invalid():
    raise PDFSourceBundleError("pdf_inline_invalid", "Inline repair cannot preserve its exact page and paragraph boundary.")


class _Paragraphs(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.offsets = [0] + [m.end() for m in re.finditer("\n", source)]
        self.stack, self.paragraphs = [], []
        self.feed(source)
        self.close()
        if self.stack:
            _invalid()

    def position(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, tag in _VOID)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def _start(self, tag, attrs, closed):
        if closed:
            return
        start = self.position() + len(self.get_starttag_text())
        self.stack.append((tag, dict(attrs), start))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            _invalid()
        name, attrs, start = self.stack.pop()
        if name == "p":
            self.paragraphs.append((attrs.get("id"), start, self.position()))


def apply_inline_repair(source, bundle, page, proposal):
    """Apply one typed proposal; preserve all bytes outside its exact inline span."""
    if not isinstance(source, str) or not Draft202012Validator(INLINE_PROPOSAL_SCHEMA).is_valid(proposal):
        _invalid()
    anchor = proposal["anchor_id"]
    owners = [entry["page_number"] for entry in bundle["entries"] if anchor in entry["source_ids"]]
    if type(page) is not int or owners != [page]:
        _invalid()
    soup = BeautifulSoup(source, "html.parser")
    matches = soup.find_all(id=anchor)
    if len(matches) != 1 or matches[0].name != "p":
        _invalid()
    for node in [matches[0], *matches[0].parents]:
        style = re.sub(r"\s+", "", str(node.get("style", ""))).lower()
        if (node.name in {"head", "script", "style", "template", "noscript"}
                or node.has_attr("hidden") or node.get("aria-hidden") == "true"
                or re.search(r"(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)", style)):
            _invalid()
    spans = [item for item in _Paragraphs(source).paragraphs if item[0] == anchor]
    if len(spans) != 1:
        _invalid()
    _, start, end = spans[0]
    inner = source[start:end]
    before = proposal["before_html"]
    if inner.count(before) != 1:
        _invalid()
    fragment = BeautifulSoup(before, "html.parser")
    if any(isinstance(node, (Comment, Doctype)) for node in fragment.descendants):
        _invalid()
    for tag in fragment.find_all(True):
        if (tag.name not in {"math", "sup"}
                or set(tag.attrs) - ({"alttext"} if tag.name == "math" else set())
                or tag.find(True) or (tag.name == "math" and (not tag.get("alttext") or tag.contents))):
            _invalid()
    index = inner.index(before)
    prefix, suffix = inner[:index], inner[index + len(before):]
    if (str(BeautifulSoup(prefix, "html.parser")) + str(fragment)
            + str(BeautifulSoup(suffix, "html.parser")) != str(BeautifulSoup(inner, "html.parser"))
            or before.count("<") != before.count(">")):
        _invalid()
    parts = []
    for part in proposal["after"]:
        value = part["value"]
        if not value.strip() or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            _invalid()
        if part["kind"] == "text":
            parts.append(html.escape(value, quote=False))
        elif part["kind"] == "math":
            parts.append('<math alttext="' + html.escape(value, quote=True) + '"></math>')
        else:
            parts.append('<sup>' + html.escape(value, quote=False) + '</sup>')
    replacement = "".join(parts)
    if replacement == before or len(replacement) > 8000:
        _invalid()
    updated = source[:start + index] + replacement + source[start + index + len(before):]
    _Paragraphs(updated)
    return updated



def _validate_text_baseline(original, baseline, bundle):
    from .pdf_revision import _structure
    if _structure(original) != _structure(baseline):
        _invalid()
    left, right = BeautifulSoup(original, "html.parser"), BeautifulSoup(baseline, "html.parser")
    anchors = {sid for entry in bundle["entries"] for sid in entry["source_ids"]}
    for old, new in zip([left, *left.find_all(True)], [right, *right.find_all(True)], strict=True):
        ancestors = [old, *old.parents]
        editable = any(node.get("id") in anchors for node in ancestors)
        for node in ancestors:
            style = re.sub(r"\s+", "", str(node.get("style", ""))).lower()
            if (node.name in {"head", "script", "style", "template", "noscript"}
                    or node.has_attr("hidden") or node.get("aria-hidden") == "true"
                    or re.search(r"(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)", style)):
                editable = False
        old_text = [child for child in old.children if isinstance(child, NavigableString)]
        new_text = [child for child in new.children if isinstance(child, NavigableString)]
        if len(old_text) != len(new_text):
            _invalid()
        for a, b in zip(old_text, new_text):
            if (type(a) is not type(b) or ((not editable or isinstance(a, (Comment, Doctype))) and a != b)
                    or (str(a).strip() and not str(b).strip())):
                _invalid()
        attribute = "alttext" if old.name == "math" else "alt" if old.name == "img" else None
        if attribute:
            a, b = old.get(attribute, ""), new.get(attribute, "")
            if (not editable and a != b) or (str(a).strip() and not str(b).strip()):
                _invalid()


def validate_inline_revision(original, reviewed, bundle, review):
    """Validate v3 against a same-structure text baseline and exact repair replay."""
    from .pdf_revision import _structure

    baseline, repairs = review.get("inline_baseline_html"), review.get("inline_repairs")
    if (review.get("schema_version") != "ac.document.pdf_review.v3"
            or not isinstance(baseline, str) or not isinstance(repairs, list)
            or not repairs or _structure(original) != _structure(baseline)):
        _invalid()
    _validate_text_baseline(original, baseline, bundle)
    seen, anchors = set(), set()
    result = baseline
    for record in repairs:
        if (not isinstance(record, dict)
                or set(record) != {"page_number", "proposal", "verified"}
                or record.get("verified") is not True):
            _invalid()
        proposal = record["proposal"]
        if (not isinstance(proposal, dict) or type(record["page_number"]) is not int
                or not isinstance(proposal.get("proposal_id"), str)):
            _invalid()
        identity = (record["page_number"], proposal.get("proposal_id"))
        anchor = proposal.get("anchor_id")
        if not isinstance(anchor, str) or identity in seen or anchor in anchors:
            _invalid()
        seen.add(identity)
        anchors.add(anchor)
        result = apply_inline_repair(result, bundle, record["page_number"], proposal)
    expected = reviewed.decode("utf-8") if isinstance(reviewed, bytes) else reviewed
    if result != expected:
        _invalid()
