import hashlib
import json
from pathlib import Path

import pytest
from ac_document import (
    AcDocumentService,
    PDFSourceBundleError,
    import_mineru_bundle,
    verify_pdf_source_bundle,
)
from ac_document.parse import PDFTextLayer
from ac_document.pdf_revision import publish_reviewed_pdf_source
from ac_document.pdf_source import file_record, json_bytes


@pytest.fixture
def original(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    content = tmp_path / "content.json"
    content.write_text(
        json.dumps([{"type": "text", "text": "Original wording", "page_idx": 0}])
    )
    middle = tmp_path / "middle.json"
    middle.write_text(
        json.dumps(
            {
                "_backend": "pipeline",
                "_version_name": "3.4.5",
                "pdf_info": [
                    {
                        "page_idx": 0,
                        "page_size": [595, 842],
                        "para_blocks": [
                            {
                                "type": "text",
                                "lines": [{"spans": [{"content": "Original wording"}]}],
                            }
                        ],
                        "discarded_blocks": [],
                    }
                ],
            }
        )
    )

    class Pages:
        def extract(self, payload):
            return PDFTextLayer(("",))

    r = import_mineru_bundle(
        pdf,
        content_list=content,
        middle_json=middle,
        output_dir=tmp_path / "bundle",
        pdf_text_extractor=Pages(),
    )
    m = verify_pdf_source_bundle(r["manifest"])
    new = (
        Path(r["source"])
        .read_bytes()
        .replace(b"Original wording", b"Corrected wording")
    )
    review = {
        "schema_version": "ac.document.pdf_review.v1",
        "source_bundle_digest": m["bundle_digest"],
        "source_sha256": m["source"]["sha256"],
        "reviewed_source_sha256": hashlib.sha256(new).hexdigest(),
        "candidate_digest": "a" * 64,
        "reviewer": "user",
        "approved": True,
        "uncertainty_count": 0,
        "page_count": 1,
    }
    return r, new, review


def test_reviewed_source_retains_provenance_and_original_evidence(original, tmp_path):
    r, new, review = original
    result = publish_reviewed_pdf_source(
        r["manifest"],
        reviewed_source=new,
        review=review,
        output_dir=tmp_path / "reviewed",
    )
    manifest = verify_pdf_source_bundle(result["manifest"])
    doc = AcDocumentService(cache_root=tmp_path / "cache").parse_pdf_source(
        result["source"], manifest=result["manifest"]
    )
    assert doc.metadata["pdf_source"]["proofread"] is True
    assert len(doc.blocks) == len(doc.page_map) == 1
    assert doc.metadata["pdf_source"]["proofreading"]["candidate_digest"] == "a" * 64
    assert b"Original wording" in Path(r["source"]).read_bytes()
    assert manifest["original"] == verify_pdf_source_bundle(r["manifest"])["original"]
    assert (
        publish_reviewed_pdf_source(
            r["manifest"],
            reviewed_source=new,
            review=review,
            output_dir=tmp_path / "reviewed",
        )
        == result
    )


@pytest.mark.parametrize(
    "field,value",
    [("approved", False), ("uncertainty_count", 1), ("source_bundle_digest", "b" * 64)],
)
def test_unapproved_or_unbound_revision_is_not_published(
    original, tmp_path, field, value
):
    r, new, review = original
    review[field] = value
    with pytest.raises(PDFSourceBundleError):
        publish_reviewed_pdf_source(
            r["manifest"],
            reviewed_source=new,
            review=review,
            output_dir=tmp_path / "reviewed",
        )
    assert not (tmp_path / "reviewed").exists()


def test_structure_change_rejected(original, tmp_path):
    r, new, review = original
    new = new.replace(b"</article>", b'<img src="https://example.org/leak"></article>')
    review["reviewed_source_sha256"] = hashlib.sha256(new).hexdigest()
    with pytest.raises(PDFSourceBundleError, match="structure"):
        publish_reviewed_pdf_source(
            r["manifest"],
            reviewed_source=new,
            review=review,
            output_dir=tmp_path / "reviewed",
        )


def _write_manifest(path, manifest):
    manifest["bundle_digest"] = hashlib.sha256(
        json_bytes(
            {key: value for key, value in manifest.items() if key != "bundle_digest"}
        )
    ).hexdigest()
    path.write_bytes(json_bytes(manifest))


def _replace_bound_file(root, manifest, relative, payload):
    (root / relative).write_bytes(payload)
    records = [
        manifest["original"],
        manifest["source"],
        *manifest["evidence"],
        *manifest["resources"],
    ]
    next(record for record in records if record["path"] == relative).update(
        file_record(relative, payload)
    )


def _replace_approval(root, manifest, review):
    payload = json_bytes(review)
    _replace_bound_file(root, manifest, "review/approval.json", payload)
    manifest["proofreading"]["review_sha256"] = hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    "mutation",
    [
        "original_pdf",
        "page_size",
        "entry_bbox",
        "resource_inventory",
        "provider",
        "source_path",
        "original_evidence_deleted",
        "original_evidence_changed",
        "original_source_deleted",
        "original_source_changed",
        "previous_schema",
        "previous_digest",
        "current_structure",
    ],
)
def test_old_approval_cannot_authorize_changed_source_bindings(
    original, tmp_path, mutation
):
    source, new, review = original
    result = publish_reviewed_pdf_source(
        source["manifest"],
        reviewed_source=new,
        review=review,
        output_dir=tmp_path / "reviewed",
    )
    path = Path(result["manifest"])
    root = path.parent
    manifest = verify_pdf_source_bundle(path)
    if mutation == "original_pdf":
        _replace_bound_file(
            root, manifest, manifest["original"]["path"], b"%PDF-1.4\nA different PDF"
        )
    elif mutation == "page_size":
        manifest["pages"][0]["size"] = [400, 500]
    elif mutation == "entry_bbox":
        manifest["entries"][0]["bbox"] = [1, 2, 3, 4]
    elif mutation == "resource_inventory":
        (root / "assets").mkdir(exist_ok=True)
        relative, payload = "assets/new.png", b"\x89PNG\r\n\x1a\nextra image evidence"
        (root / relative).write_bytes(payload)
        manifest["resources"].append(
            {
                **file_record(relative, payload),
                "media_type": "image/png",
                "rendered": False,
                "original_paths": ["images/new.png"],
            }
        )
    elif mutation == "provider":
        manifest["provider"]["version"] = "different-version"
    elif mutation == "source_path":
        (root / manifest["source"]["path"]).rename(root / "renamed.html")
        manifest["source"]["path"] = "renamed.html"
    elif mutation in {"original_evidence_deleted", "original_source_deleted"}:
        relative = (
            "evidence/content-list.json"
            if mutation == "original_evidence_deleted"
            else "review/original-source.html"
        )
        manifest["evidence"] = [
            record for record in manifest["evidence"] if record["path"] != relative
        ]
        (root / relative).unlink()
    elif mutation == "original_evidence_changed":
        _replace_bound_file(root, manifest, "evidence/content-list.json", b"[]\n")
    elif mutation == "original_source_changed":
        _replace_bound_file(
            root,
            manifest,
            "review/original-source.html",
            b"<p>A different original source</p>",
        )
    elif mutation in {"previous_schema", "previous_digest"}:
        previous = json.loads((root / "review/original-manifest.json").read_bytes())
        if mutation == "previous_schema":
            previous["unexpected_field"] = "not part of the original manifest contract"
            previous["bundle_digest"] = hashlib.sha256(
                json_bytes(
                    {
                        key: value
                        for key, value in previous.items()
                        if key != "bundle_digest"
                    }
                )
            ).hexdigest()
            manifest["proofreading"]["source_bundle_digest"] = previous["bundle_digest"]
            review["source_bundle_digest"] = previous["bundle_digest"]
            _replace_approval(root, manifest, review)
        else:
            previous["bundle_digest"] = "b" * 64
        _replace_bound_file(
            root, manifest, "review/original-manifest.json", json_bytes(previous)
        )
    elif mutation == "current_structure":
        changed = new.replace(
            b"Corrected wording", b"<strong>Corrected wording</strong>"
        )
        _replace_bound_file(root, manifest, manifest["source"]["path"], changed)
        manifest["proofreading"]["source_sha256"] = hashlib.sha256(changed).hexdigest()
        review["reviewed_source_sha256"] = hashlib.sha256(changed).hexdigest()
        _replace_approval(root, manifest, review)
    _write_manifest(path, manifest)
    with pytest.raises(PDFSourceBundleError):
        verify_pdf_source_bundle(path)


def test_old_approval_cannot_move_content_between_original_pages(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\nTwo-page fixture")
    content, middle = tmp_path / "content.json", tmp_path / "middle.json"
    content.write_text(
        json.dumps(
            [
                {"type": "text", "text": text, "page_idx": index}
                for index, text in enumerate(["First page", "Second page"])
            ]
        )
    )
    middle.write_text(
        json.dumps(
            {
                "_backend": "pipeline",
                "_version_name": "3.4.5",
                "pdf_info": [
                    {
                        "page_idx": index,
                        "page_size": [595, 842],
                        "para_blocks": [
                            {
                                "type": "text",
                                "lines": [{"spans": [{"content": "Page text"}]}],
                            }
                        ],
                        "discarded_blocks": [],
                    }
                    for index in range(2)
                ],
            }
        )
    )

    class Pages:
        def extract(self, payload):
            return PDFTextLayer(("", ""))

    source = import_mineru_bundle(
        pdf,
        content_list=content,
        middle_json=middle,
        output_dir=tmp_path / "source",
        pdf_text_extractor=Pages(),
    )
    before = verify_pdf_source_bundle(source["manifest"])
    source_bytes = Path(source["source"]).read_bytes()
    review = {
        "schema_version": "ac.document.pdf_review.v1",
        "source_bundle_digest": before["bundle_digest"],
        "source_sha256": before["source"]["sha256"],
        "reviewed_source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "candidate_digest": "a" * 64,
        "reviewer": "user",
        "approved": True,
        "uncertainty_count": 0,
        "page_count": 2,
    }
    result = publish_reviewed_pdf_source(
        source["manifest"],
        reviewed_source=source_bytes,
        review=review,
        output_dir=tmp_path / "reviewed",
    )
    path = Path(result["manifest"])
    manifest = verify_pdf_source_bundle(path)
    manifest["entries"][0]["page_number"] = 2
    manifest["entries"][1]["page_number"] = 1
    _write_manifest(path, manifest)
    with pytest.raises(PDFSourceBundleError, match="original source bindings"):
        verify_pdf_source_bundle(path)


def test_model_revision_preserves_uncertainties_without_human_review_claim(
    original, tmp_path
):
    r, new, review = original
    review.update(
        schema_version="ac.document.pdf_review.v2",
        reviewer="model",
        approved=False,
        uncertainty_count=3,
    )
    result = publish_reviewed_pdf_source(
        r["manifest"], reviewed_source=new, review=review, output_dir=tmp_path / "model"
    )
    manifest = verify_pdf_source_bundle(result["manifest"])
    doc = AcDocumentService(cache_root=tmp_path / "cache").parse_pdf_source(
        result["source"], manifest=result["manifest"]
    )
    assert doc.metadata["pdf_source"]["proofread"] is False
    assert doc.metadata["pdf_source"]["proofreading"]["review_mode"] == "model"
    assert doc.metadata["pdf_source"]["proofreading"]["uncertainty_count"] == 3
    assert any("3 unresolved" in w for w in manifest["warnings"])
    changed = json.loads(Path(result["manifest"]).read_text())
    changed["proofreading"]["review_mode"] = "human"
    changed["bundle_digest"] = hashlib.sha256(
        json_bytes({k: v for k, v in changed.items() if k != "bundle_digest"})
    ).hexdigest()
    Path(result["manifest"]).write_bytes(json_bytes(changed))
    with pytest.raises(PDFSourceBundleError):
        verify_pdf_source_bundle(result["manifest"])


@pytest.mark.parametrize("first_mode", ["model", "human"])
def test_conflicting_review_mode_preserves_existing_output(
    original, tmp_path, first_mode
):
    source, corrected, review = original
    human = {**review, "schema_version": "ac.document.pdf_review.v2"}
    model = {**human, "reviewer": "model", "approved": False}
    first, second = (model, human) if first_mode == "model" else (human, model)
    output = tmp_path / "reviewed"
    result = publish_reviewed_pdf_source(
        source["manifest"], reviewed_source=corrected, review=first, output_dir=output
    )
    before = {
        p.relative_to(output): p.read_bytes()
        for p in output.rglob("*") if p.is_file()
    }
    with pytest.raises(PDFSourceBundleError) as error:
        publish_reviewed_pdf_source(
            source["manifest"],
            reviewed_source=corrected,
            review=second,
            output_dir=output,
        )
    assert error.value.code == "pdf_review_output_exists"
    after = {
        p.relative_to(output): p.read_bytes()
        for p in output.rglob("*") if p.is_file()
    }
    assert after == before
    manifest = verify_pdf_source_bundle(result["manifest"])
    assert manifest["proofreading"]["review_mode"] == first_mode
    assert (
        publish_reviewed_pdf_source(
            source["manifest"], reviewed_source=corrected, review=first, output_dir=output
        )
        == result
    )


def test_changed_manual_review_evidence_is_not_an_idempotent_retry(
    original, tmp_path
):
    source, corrected, review = original
    review = {
        **review,
        "schema_version": "ac.document.pdf_review.v2",
        "manual_edits": {"entry-0": "First note"},
    }
    output = tmp_path / "reviewed"
    result = publish_reviewed_pdf_source(
        source["manifest"], reviewed_source=corrected, review=review, output_dir=output
    )
    receipt = (output / "review/approval.json").read_bytes()
    changed = {**review, "manual_edits": {"entry-0": "Changed note"}}
    with pytest.raises(PDFSourceBundleError) as error:
        publish_reviewed_pdf_source(
            source["manifest"],
            reviewed_source=corrected,
            review=changed,
            output_dir=output,
        )
    assert error.value.code == "pdf_review_output_exists"
    assert (output / "review/approval.json").read_bytes() == receipt
    verify_pdf_source_bundle(result["manifest"])


@pytest.mark.parametrize(
    "fields",
    [
        dict(reviewer="model", approved=True),
        dict(reviewer="user", uncertainty_count=2),
        dict(reviewer="model", approved=False, uncertainty_count=-1),
    ],
)
def test_invalid_review_mode_combinations(original, tmp_path, fields):
    r, new, review = original
    review.update(schema_version="ac.document.pdf_review.v2", **fields)
    with pytest.raises(PDFSourceBundleError):
        publish_reviewed_pdf_source(
            r["manifest"],
            reviewed_source=new,
            review=review,
            output_dir=tmp_path / "invalid-mode",
        )


def _inline_review(original):
    from ac_document.pdf_inline import apply_inline_repair
    r, baseline, review = original
    bundle = verify_pdf_source_bundle(r['manifest'])
    anchor = bundle['entries'][0]['source_ids'][0]
    proposal = {'proposal_id': 'missing-domain', 'anchor_id': anchor,
                'before_html': 'Corrected wording',
                'after': [{'kind': 'text', 'value': 'Corrected '}, {'kind': 'math', 'value': r'\mathcal D'}],
                'reason': 'Visible domain symbol in PDF'}
    corrected = apply_inline_repair(baseline.decode(), bundle, 1, proposal).encode()
    review = {**review, 'schema_version': 'ac.document.pdf_review.v3',
              'reviewer': 'model', 'approved': False,
              'reviewed_source_sha256': hashlib.sha256(corrected).hexdigest(),
              'inline_baseline_html': baseline.decode(),
              'inline_repairs': [{'page_number': 1, 'proposal': proposal, 'verified': True}]}
    return r, corrected, review


def test_inline_review_publishes_verifiable_bundle_and_preserves_original(original, tmp_path):
    r, corrected, review = _inline_review(original)
    result = publish_reviewed_pdf_source(r['manifest'], reviewed_source=corrected,
                                        review=review, output_dir=tmp_path/'inline')
    bundle = verify_pdf_source_bundle(result['manifest'])
    previous = verify_pdf_source_bundle(r['manifest'])
    for field in ('original', 'pages', 'entries', 'resources', 'provider'):
        assert bundle[field] == previous[field]
    assert bundle['proofreading']['review_mode'] == 'model'
    assert (tmp_path/'inline/review/original-source.html').read_bytes() == Path(r['source']).read_bytes()
    assert Path(result['source']).read_bytes() == corrected


@pytest.mark.parametrize('mutation', ['wrong_page', 'unverified', 'baseline_structure', 'outside_change', 'replay_mismatch', 'duplicate'])
def test_inline_review_rejects_unbound_structural_changes(original, tmp_path, mutation):
    r, corrected, review = _inline_review(original)
    if mutation == 'wrong_page':
        review['inline_repairs'][0]['page_number'] = 2
    elif mutation == 'unverified':
        review['inline_repairs'][0]['verified'] = False
    elif mutation == 'baseline_structure':
        review['inline_baseline_html'] = review['inline_baseline_html'].replace('</body>', '<p>extra</p></body>')
    elif mutation == 'outside_change':
        corrected = corrected.replace(b'</body>', b'<p>extra</p></body>')
    elif mutation == 'replay_mismatch':
        corrected = corrected.replace(b'Corrected ', b'Different ')
    else:
        review['inline_repairs'].append(review['inline_repairs'][0])
    review['reviewed_source_sha256'] = hashlib.sha256(corrected).hexdigest()
    with pytest.raises(PDFSourceBundleError):
        publish_reviewed_pdf_source(r['manifest'], reviewed_source=corrected,
                                    review=review, output_dir=tmp_path/'bad-inline')
    assert not (tmp_path/'bad-inline').exists()


@pytest.mark.parametrize('before,after', [
    ('<math alttext="P_n"></math> 1', [{'kind':'math','value':'P_{n-1}'}]),
    ('If is', [{'kind':'text','value':'If '},{'kind':'math','value':r'\mathcal D'},{'kind':'text','value':' is'}]),
    ('&lt;sup&gt;4&lt;/sup&gt;', [{'kind':'sup','value':'4'}]),
])
def test_inline_repair_preserves_unselected_bytes(before, after):
    from ac_document.pdf_inline import apply_inline_repair
    source = '<html><body><p id="p1">Prefix '+before+' suffix</p></body></html>'
    bundle = {'entries':[{'source_ids':['p1'],'page_number':1}]}
    proposal = dict(proposal_id='p',anchor_id='p1',before_html=before,after=after,reason='PDF comparison')
    result = apply_inline_repair(source,bundle,1,proposal)
    assert result.startswith('<html><body><p id="p1">Prefix ')
    assert result.endswith(' suffix</p></body></html>')
    assert result != source


@pytest.mark.parametrize('before,part', [
    ('word', {'kind':'image','value':'remote'}),
    ('word', {'kind':'math','value':'x\x00y'}),
    ('<!--word-->', {'kind':'text','value':'visible'}),
    ('<sup id="keep">1</sup>', {'kind':'sup','value':'2'}),
    ('<img src="keep.png">', {'kind':'text','value':'gone'}),
])
def test_inline_repair_rejects_markup_and_protected_elements(before, part):
    from ac_document.pdf_inline import apply_inline_repair
    source = '<p id="p1">'+before+'</p>'
    proposal = dict(proposal_id='p',anchor_id='p1',before_html=before,after=[part],reason='PDF comparison')
    with pytest.raises(PDFSourceBundleError):
        apply_inline_repair(source,{'entries':[{'source_ids':['p1'],'page_number':1}]},1,proposal)


@pytest.mark.parametrize('kind,value', [('math', 'x<y'), ('math', 'r>0'), ('text', '<script>alert(1)</script>')])
def test_inline_typed_values_escape_markup_without_rejecting_comparisons(kind, value):
    from ac_document.pdf_inline import apply_inline_repair
    from bs4 import BeautifulSoup
    proposal = dict(proposal_id='p',anchor_id='p1',before_html='word',after=[{'kind':kind,'value':value}],reason='PDF comparison')
    result = apply_inline_repair('<p id="p1">word</p>',{'entries':[{'source_ids':['p1'],'page_number':1}]},1,proposal)
    soup = BeautifulSoup(result, 'html.parser')
    assert soup.find('script') is None
    if kind == 'math':
        assert soup.find('math')['alttext'] == value
    else:
        assert soup.p.get_text() == value


@pytest.mark.parametrize('original,baseline', [
    ('<script>safe()</script><p id="p1">word</p>', '<script>evil()</script><p id="p1">word</p>'),
    ('<p hidden>hidden</p><p id="p1">word</p>', '<p hidden>changed</p><p id="p1">word</p>'),
    ('<p id="p2">keep</p><p id="p1">word</p>', '<p id="p2"> </p><p id="p1">word</p>'),
    ('<p>unmapped</p><p id="p1">word</p>', '<p>changed</p><p id="p1">word</p>'),
])
def test_inline_baseline_cannot_modify_hidden_or_erase_other_content(original, baseline):
    from ac_document.pdf_inline import apply_inline_repair, validate_inline_revision
    bundle = {'entries':[{'source_ids':['p1','p2'],'page_number':1}]}
    proposal = dict(proposal_id='p',anchor_id='p1',before_html='word',after=[{'kind':'math','value':'x'}],reason='PDF comparison')
    corrected = apply_inline_repair(baseline,bundle,1,proposal)
    review = dict(schema_version='ac.document.pdf_review.v3',inline_baseline_html=baseline,
                  inline_repairs=[dict(page_number=1,proposal=proposal,verified=True)])
    with pytest.raises(PDFSourceBundleError):
        validate_inline_revision(original,corrected,bundle,review)


def test_inline_replay_rejects_second_repair_in_same_paragraph():
    from ac_document.pdf_inline import validate_inline_revision
    proposal = dict(proposal_id='p',anchor_id='p1',before_html='a',after=[{'kind':'text','value':'ab'}],reason='PDF comparison')
    second = {**proposal,'proposal_id':'q','before_html':'ab','after':[{'kind':'text','value':'Z'}]}
    review = dict(schema_version='ac.document.pdf_review.v3',inline_baseline_html='<p id="p1">ab</p>',
                  inline_repairs=[dict(page_number=1,proposal=p,verified=True) for p in (proposal,second)])
    with pytest.raises(PDFSourceBundleError):
        validate_inline_revision('<p id="p1">ab</p>','<p id="p1">Zb</p>',
                                 {'entries':[{'source_ids':['p1'],'page_number':1}]},review)
