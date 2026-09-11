# ac-document

Host-independent document infrastructure for AC Foundation: immutable source storage,
deterministic parsing, rich-document contracts, cached search, document
structure, and terminology workflows. It contains no paper-provider behavior.

`AcDocumentService` accepts local files and repository artifacts. Academic
identifiers and providers belong to a consumer package, not `ac-document`.

HTML MathML TeX projections are normalized before entering a `RichDocument`.
LaTeXML-only line-breaking hints, redundant default-black color commands, and
empty `array` option lists are removed while semantic TeX is preserved.
LaTeXML equation tables without visible equation numbers retain one logical
equation unit per authored table row instead of emitting each MathML cell as a
separate display equation.

## Resilient optional HTML projections

HTML parses record `metadata.document_diagnostics` with schema
`ac.document.document_diagnostics.v1`. It is a codec-validated local ledger
for optional source presentation, Figure/Table layout, source-target
navigation, front matter, and notes. Each entry has a stable category, source
scope and locator, `exact`, `neutral`, or `unavailable` status, a fallback
action, and bounded evidence. Its visible-content accounting reconciles every
visible source unit to emitted content, a documented exclusion, or a safe
plain fallback; `unaccounted` is always zero.

Ownership is enumerated from the DOM before RichDocument projection: nested
and repeated wrappers receive one non-overlapping unit, article-internal
navigation is a documented exclusion, and visible siblings outside selected
article roots are recorded as source chrome rather than guessed as paper body.

Exact projection validators remain fail-closed: malformed optional layout,
target, or presentation data is never admitted as authoritative. The parser
instead keeps safe core blocks where possible, uses a neutral projection or
source-preserving plain block where it is not, and records the degradation.
An HTML structure that degrades to a plain paragraph still publishes that
paragraph's exact plain rich field, so one local fallback cannot invalidate
otherwise valid presentation metadata for the rest of the document.
Source identity, UTF-8 decoding, source digest, unsafe input, and core
`RichBlock`/codec contract failures remain hard errors. Existing v2/v3
documents and Markdown/TeX canonical output remain compatible when this
optional key is absent.

Explicit Markdown, TeX, and HTML keyword/index metadata is normalized before
terminology workflows consume it. A value made only of Unicode dash punctuation
is an empty publisher placeholder and is omitted; an authored Keywords section
remains ordinary document content and can still supply the real term list.
Model-proposed terms enter the public keyword result only when the term or one
of its aliases has a literal source occurrence. Ungrounded semantic guesses
remain durable model evidence but do not become glossary entries or Reader
omission warnings. Cached legacy inventories receive the same grounded result
projection.

```bash
ac-document --help
ac-document export-rich-document source.md --output-dir publication
ac-document acquire-html-bundle https://example.org/document.html --output-dir document-bundle
python -m pytest packages/ac-document/tests
```

## PDF source bundles from MinerU

Import an existing **MinerU 3.4.5 pipeline** result into a portable document
source. This offline adapter needs `pdftotext` to read the original PDF's page
count; it accepts scanned PDFs with empty text layers. Run MinerU separately
before importing its content list, middle JSON, and adjacent image files.
HTML conversion decodes MinerU's Markdown text escapes while preserving LaTeX
formula payloads, code, and the original provider evidence. Inline formulas in
text surrounding table grids remain source math after table sanitization.

```bash
ac-document import-mineru-bundle book.pdf \
  --content-list local/mineru/book/auto/book_content_list.json \
  --middle-json local/mineru/book/auto/book_middle.json \
  --output-dir local/book-source
ac-document verify-pdf-source-bundle local/book-source/manifest.json
ac-document export-rich-document local/book-source/source.html \
  --pdf-source-manifest local/book-source/manifest.json \
  --output-dir local/book-publication
```

The public Python functions are `import_mineru_bundle()` and
`verify_pdf_source_bundle()`. `AcDocumentService.export_rich_document()` accepts
the same explicit `pdf_source_manifest` argument. Both command operations are
registered with the arbitrary-local-path effect; they require no Agent or Web
application.

`restore_mineru_page_items(items, middle)` returns a deep-copied list of ordinary
content-list dictionaries for page-local text consumers. For text merge chains,
it checks the original `preproc_blocks` line geometry, span text, reading order,
and cross-page markers before splitting the merged text and filling deleted
placeholders. Inline formulas remain formulas, and a word split across pages
retains the original page-end hyphen. Other items and their order are unchanged;
neither input is modified. Missing, ambiguous, inconsistent, or unsupported merge
evidence raises `PDFSourceBundleError` with code `mineru_page_restore_unavailable`.
Callers must not treat the unmodified merged text as page-local after this error.
Import uses this helper and retains changed items as the additional hashed
`evidence/page-content-list.json`, alongside both original provider JSON files.
Cross-page table reconstruction is not supported: marked merged tables are
rejected, while unmarked tables retain the provider's reported page association.

The source directory must be new. It is published atomically and contains
`source.html`, `original.pdf`, the original provider JSON under `evidence/`,
content-addressed images under `assets/`, and `manifest.json` with schema
`ac.document.pdf_source_bundle.v1`. Every stored file has a byte count and SHA-256;
verification works after moving the whole directory and rejects changed or
missing files. Raw equation/table crops are retained as evidence without
adding duplicate figures to the rendered source. Missing or unsupported images
leave available captions and text with explicit coverage warnings. Resource
paths must stay inside the input result directory. Supported images are PNG,
JPEG, and WebP; original PDFs have no fixed byte-size ceiling; imports are bounded to
50 MiB per JSON, 25 MiB
per image and 200 MiB of images in total.

Normalization preserves tables, captions, formulas, lists, code, and page footnotes
from the structured result. Page headers, footers and page-number blocks are
recorded as excluded furniture. Unknown content types retain readable text and
produce a warning. Unsupported versions/backends and invalid page indices are
rejected before publication. The original PDF and OCR result are paired by the
caller; matching page counts do not establish that the recognized text is correct.

The manifest accounts for every original page. `parsed` means content was
emitted; `partial` means available content accompanies an extraction gap;
`empty` means the provider's middle data reports no readable content; `unavailable`
means no usable content is available for that page. An empty recognized text
region is an extraction gap, not evidence of a truly blank page. Original
provider order is retained within each page, including its footnotes. The middle
data's readable/visual block count (`expected_entries`) is a lower-bound coverage
check against the content list. Missing inventory or fewer reported entries
produces a gap, never a verified empty page. This count cannot detect every
semantic omission. Empty table grids retain available crops/captions with a gap
warning; they do not count as successfully extracted tables.

Use the manifest with the exact bundled HTML when exporting. The resulting
RichDocument carries its normal `page_map` and codec-validated
`metadata.pdf_source` (`ac.document.pdf_source_provenance.v1`): original PDF and
normalized source hashes, bundle identity, page coverage, and per-block provider
entry/bounding-box provenance. Coordinates use MinerU's normalized 0–1000 page
space; absent coordinates remain null. `proofread` is always false. This binding
records extraction provenance and is separate from the optional PDF text
validator; `--validator` and `--pdf-source-manifest` cannot be combined.
Ordinary exports without the manifest keep their existing behavior. Downstream
callers must pass this manifest explicitly to preserve the PDF mapping; simply
opening the generated HTML does not bind it automatically.

## Explicit HTML bundle acquisition

`ac-document acquire-html-bundle` is the only URL-fetching document command.
It performs a bounded public-HTTPS acquisition, stores the original HTML plus
available same-origin image dependencies in the AC document cache, and emits an
`ac.document.html_source_export.v1` `manifest.json` into the required output
directory. Its nested `bundle` is an `ac.document.html_source_bundle.v1` document.
Local import, parse, and export
operations remain network-free. Dependencies that cannot be fetched safely are
represented as structured bundle warnings; their bytes are never fabricated.
Dependencies are same-final-origin by default. A caller can set
`same_origin_dependencies=False` only with a nonempty explicit `allowed_origins` policy
that includes every additional public HTTPS origin it intends to acquire from.

The public `HTMLSourceAcquisitionService.acquire_dependencies()` API accepts a
caller-verified HTML primary and a storage adapter, so consumers can keep their
own cache/archive implementation. `materialize_html_source_bundle()` publishes
an atomically verified local export. Safe relative authored targets retain the
original primary HTML bytes; only targets that cannot be safely materialized in
place are rewritten and recorded in the export manifest.
`verify_html_source_bundle_export()` lets an offline consumer reload that
manifest and verify the staged `source.html` and each referenced resource before
use.

`HTMLSourceAcquisitionService.fetch_resource()` exposes the same public-HTTPS,
DNS-pinning, redirect and byte-limit checks for an explicitly acquired document
resource. The caller validates its media type and owns format routing; fetching
does not infer that a DOI landing page is full text. An already fetched HTML
response can pass through `materialize_response()` to acquire dependencies and
cache its source bundle without fetching the primary again. Local parsing still
does not perform network acquisition.

## RichDocument list ancestry

RichDocument v3 keeps authored list content as flat, independently addressable
blocks. Each block inside an HTML list item carries an ordered `list_path`.
Entries preserve deterministic container/item identities, authored IDs and
selectors when present, item index/count, nesting depth, ordered semantics, and
an exact segment index. `continuation` is true precisely after segment zero, so
consumers can draw one marker per authored item without inspecting block text.

Construction and codec decoding reject conflicting owners, duplicate authored
IDs, invalid nesting/indexes, discontinuous segments, section mismatches, and
source-target alias collisions. Declared item counts must equal the bounded
emitted item coverage and are never expanded as an untrusted numeric range.
Existing v2 documents remain decodable and
round-trip as v2 with no `list_path`; reparsing is required to migrate them to
v3 and changes the document digest, so document-bound derived artifacts must be
rebuilt.

LaTeXML HTML author groups remain structured when preceded by a subtitle.
An explicit email after an unresolved `\\corrauth` marker is separated from the
name; this does not infer corresponding-author status. Unresolved markup and
empty email fields produce parsing warnings. Native tables and LaTeXML
`span.ltx_tabular` grids inside table figures retain cells, math, captions and
source targets. Visible tables with unsupported geometry retain plain content and diagnostics;
uncovered visible content remains a parsing error. Acknowledgement
wrapper links resolve to their represented child content.

## Authored front matter and notes

HTML RichDocuments may expose two independently versioned metadata contracts:
`source_front_matter` (`ac.document.source_front_matter.v1`) and
`source_notes` (`ac.document.source_notes.v1`). Front matter preserves the
exact insertion point, locator, ordered authors, authored markers, ORCIDs,
contacts, and affiliations. Notes preserve one marker in owner content plus a
separate rich body, exact note and owner locators, final owner block ID,
validated paragraph/list/table marker anchor, and source order. Note bodies
retain inline links and math without duplicating the
body or LaTeXML's nested marker markup inside paragraph/table payloads.
LaTeXML `ltx_role_footnotemark` nodes that contain only a marker and the
generated `footnotemark:` accessibility label are not source notes: their
visible marker remains in the owner content, while no empty standalone note is
invented. Separate authored Table-note paragraphs remain part of the Table.
Known LaTeXML publication-note containers adjacent to the byline are excluded
from the primary title and ordinary body flow. Empty contact scaffolding is
omitted, while a creator-owned ORCID link remains attached to its normalized
author even when LaTeXML nests it beside the person name.
Consumers bind a note only through `owner_block_id` plus `anchor`.
`owner_locator` is immutable source provenance, not a routing or binding key;
serialized locator changes are covered by the RichDocument digest.
The current anchor contract covers paragraph text, LIST items, and Table
headers/cells. A note in a heading, Figure caption, or Table caption cannot
claim an exact note projection: the parser emits its safe plain body fallback
and records an unavailable `source_notes` diagnostic rather than dropping
visible source content or rejecting the whole document.

Each authored front-matter entry also carries an exact `creator_flow` for
source-faithful presentation. Ordered creator groups reference one normalized
author and contain ordered typed slots: the author occurrence, normalized
contacts by stable per-author index, and normalized affiliations by identity.
Slots keep deterministic identities and source locators but do not copy names,
emails, ORCIDs, or affiliation text. The same affiliation identity may occur in
several creator groups, including a trailing registry inside the final source
creator. Such repetition records presentation occurrence only; semantic
author-to-affiliation association remains exclusively the normalized author
markers plus affiliation registry.

Consumers render the source front matter by creator-flow group/slot order, but
use normalized authors/contacts/affiliations for lookup and association. A
translation surface may reuse the source grouping while leaving person names,
emails, ORCIDs, and other identifiers untranslated, and it must not infer
affiliation ownership from the group containing an occurrence. Responsive
layout and pixel styling remain consumer concerns. The producer recognizes
only direct LaTeXML/ar5iv `ltx_creator ltx_role_author` structure; it never
groups creators by names, marker text, institution similarity, coordinates, or
adjacency.

Both contracts have exact nested field sets and are validated during
RichDocument construction and decoding. Documents without these optional keys,
including existing v2 and v3 documents, retain their original codec behavior.
An absent key is the legacy case; an explicitly present `null` value is invalid.
The earlier unpublished `source_front_matter.v1` draft lacked creator flow and
is intentionally rejected when present; producer and consumer artifacts from
that draft must be reparsed/rebuilt together.
Consumers must reparse and rebuild document-bound artifacts to acquire the new
metadata. HTML parsing requires every visible article flow event to emit
content, emit structured front matter, use a documented structural exclusion,
or receive a safe plain fallback; the diagnostics ledger rejects a nonzero
unaccounted count. Figure, Table, and panel order remains the authored HTML
order.

## Authored source presentation

HTML RichDocuments may also expose `metadata.source_presentation` with schema
`ac.document.source_presentation.v1`. When present, it is the authoritative
rich view of otherwise plain block fields: heading and paragraph text, LIST
items, Figure and Table captions, and every Table header/cell. Typed link and
math spans reconstruct the unchanged plain value; independent `strong` and
`emphasis` ranges preserve source-authored marks even when they overlap a link
or math span. Closed semantic heading roles distinguish `abstract`,
`classification`, and `acknowledgements` from a bare HTML heading level without
inspecting displayed text.

For exact LaTeXML/ar5iv abstract and acknowledgements conventions, the heading
block's plain `payload.level` is the semantic document level rather than the
presentational `h1`...`h6` tag number. A document-front-matter abstract is a
level-2 child of the document title. A root acknowledgement is also level 2;
an acknowledgement under an authored HTML `section` is one level below that
section's unique preceding direct heading. A parent already at level 6,
conflicting abstract/acknowledgement conventions, repeated nested convention
ancestors, or a missing/ambiguous section parent prevents that optional
semantic projection from being admitted. Ordinary h6
elements, literal `Abstract`/`Acknowledgements` text, and unknown classes retain
their authored numeric tag level and receive no semantic role. Classification
headings retain their authored level and remain outside the outline. Consumers
use the semantic block level for outlines and Markdown; they must not recover
roles or levels from heading text, neighboring blocks, or the raw HTML tag.

An exact `classifications` relation binds one classification heading to its
ordered value blocks and declares inline composition. The `": "` separator is
only declared for the LaTeXML/ar5iv semantic pair `ltx_classification` plus
`ltx_title ltx_title_classification`; it comes from that stylesheet profile's
title `:after` rule, not from displayed text. Unknown, missing, nested, or
ambiguous structures expose no relation, so consumers must not merge adjacent
blocks heuristically. The exact provenance token is
`latexml_ar5iv_classification_after`; the producer does not apply the separator
to arbitrary HTML headings or parse remote CSS. Classification headings remain
outside the outline.

The unified `captions` registry covers every visible Figure and Table caption.
It preserves `before_content`, `after_content`, or Table-only `embedded`
placement and a nullable logical alignment. Alignment is authoritative only
when backed by exact semantic `text-align:start|center|end` style or LaTeXML
`ltx_centering`/`ltx_align_center` class tokens; unknown evidence is explicit
neutral metadata and conflicting evidence degrades only that optional
presentation projection. Translation consumers
reuse source placement/alignment while keeping translated caption content
independent. Table entries separately preserve ordered authored cell origins,
including `rowspan`/`colspan`, source cell kind, and locator. Each origin also
carries a nullable horizontal alignment with exact class/style evidence and an
ordered set of authored physical rule edges. Alignment preserves physical
`left`/`right`, logical `start`/`end`, and `center` without converting between
them. Rule edges preserve physical `top`/`right`/`bottom`/`left` provenance;
covered span positions never receive independent style.

Consumers treat present Table-cell metadata as authoritative: start with no
synthetic grid, then draw only declared rule edges and apply only declared
alignment. The producer recognizes a closed set of LaTeXML alignment/border
classes and safe `text-align` keywords. Unknown alignment remains neutral;
recognized conflicts, duplicate physical edges, unsupported inline border CSS,
and unknown `ltx_border_*` classes are kept out of an exact presentation
projection. Table raw padding, arbitrary
style, pixel dimensions, and TeX lengths are deliberately outside the Table
contract, so consumers retain their safe default cell padding. Authored span
coverage is aggregate-bounded before grid expansion to reject hostile or
accidentally enormous rectangular spans.

LaTeXML transformed Tables that use one outer `span.ltx_tabular` with
`span.ltx_tr` and direct `span.ltx_td` children are normalized through the
same bounded grid contract as native HTML Tables. Nested `ltx_tabular`
structures remain cell content; multiple independent outer grids, malformed
rows, or unsafe spans retain the existing visible plain-text fallback.

The ordered `figures` registry covers every Figure that has an authoritative
source-target panel manifest. Each descriptor joins by final Figure `block_id`;
its panels join the existing asset/status manifest by contiguous `panel_index`
and exact authored `source_id`. An exact LaTeXML/ar5iv single
`img|object.ltx_graphics` is a one-column `single` layout. The graphic may be
directly owned by the Figure or nested through a pure single-child `p`/`span`
wrapper chain; wrappers with authored text, extra elements, or other layout
structure remain ambiguous and produce a neutral Figure projection with a
diagnostic. An exact direct
`ltx_flex_figure` preserves ordered rows, exact `ltx_flex_break` positions,
and each row's authored `ltx_flex_size_1|2|3` source. Cells within one row
must use one size, while explicitly separated rows may use different sizes;
the root `column_source` is null for that mixed-row case and `column_count`
is the maximum authored row capacity. The
producer never derives columns from panel count, filenames, captions, or Figure
numbers. Addressable generic HTML Figures outside that closed profile receive a
`neutral` descriptor with no row/column claim; Figures with no exact target
alias remain outside the registry.
If one authored Figure wrapper owns multiple direct captions, the producer
does not guess a caption-to-panel association. It emits every media and caption
inline flow in original DOM order as source-preserving blocks and records a
`figure_layout` diagnostic; caption math, links, and marks remain structured.

A flex cell may also wrap its single graphic in a captionless
`figure.ltx_figure.ltx_figure_panel`. The graphic retains its own ID and dimensions;
the wrapper must contain exactly that graphic and no visible prose or caption.
This accommodates LaTeXML panel containers without flattening separate captions.

Recognized Figure panels preserve positive bounded integer `width`/`height`
attributes and a reduced positive `style:aspect-ratio` pair with closed
provenance tokens. Either source may be absent and is then explicitly null; if
both dimensions and aspect ratio exist they must agree. Unknown or nested flex
structure, multiple flex roots, mixed size classes within one row, unknown
size classes, empty row breaks,
cells without exactly one direct panel graphic, malformed dimensions, and
conflicting aspect ratios are not admitted as exact layout. Consumers treat a
present exact layout
as authoritative, but use their own responsive sizing policy; raw remote CSS,
arbitrary style, pixel typography, and acquisition behavior are not part of
this contract.

Construction and codec decoding reject unknown or duplicate fields,
view/plain reconstruction mismatches, invalid spans or marks, missing block
fields, classification binding/order/separator errors, caption identity/order/
evidence conflicts, Table-cell presentation conflicts, and overlapping or
out-of-bounds cell geometry. Figure validation additionally rejects registry
coverage/order errors, panel-manifest mismatches, incomplete/overlapping grid
placement, invalid row breaks, dimensions, ratios, or provenance. Existing
v2/v3 documents without this optional metadata remain valid; consumers must not
heuristically reconstruct absent presentation. The unpublished earlier v1
drafts stored Table placement inside `tables` and later omitted `figures`;
those shapes are intentionally rejected now, with placement owned only by
`captions` and Figure layout owned only by `figures`. Producer and consumer
artifacts from those drafts must be reparsed/rebuilt together. Reparsing changes
Figure block IDs and the document digest, so translations and other
document-bound artifacts must also be rebuilt.
As with the other optional source contracts, explicit `null` is invalid rather
than equivalent to absence.

## Authoritative source targets

HTML RichDocuments may expose `metadata.source_target_manifest` with schema
`ac.document.source_target_manifest.v1`. Each exact authored alias maps to an
existing canonical block and a validated half-open block range. Section aliases
map to declared outline ranges without rewriting heading locators. Figure
targets may include ordered panel descriptors whose status is `available`,
`missing`, or `unsupported`; a compound wrapper always targets its parent
Figure block rather than panel zero.
An exact internal link may also target a uniquely owned descendant of one
emitted block, such as an authored note paragraph inside a Table wrapper. The
manifest binds that descendant alias to its containing block; ambiguous split
ownership is omitted rather than inferred. Source-note IDs remain governed by
`source_notes` and are not duplicated into this registry.

Consumers should prefer a present, valid manifest and fail closed on conflicts,
unknown kinds, missing blocks, invalid ranges, or inconsistent panels. Parser
admission drops a malformed optional manifest and records an unavailable
diagnostic while preserving core blocks. Documents without the metadata remain
compatible with a unique exact-locator fallback.
An explicitly present `null` manifest is invalid; only an absent key selects
the fallback.

## Running MinerU

The optional execution API uses the same PDF source bundle for a local
**MinerU 3.4.5 pipeline** installation and an explicitly selected
**MinerU 3.4.5 FastAPI protocol 2** service. It does not install MinerU or its
models into Foundation, ALC or an agent plugin. Install the runtime separately
using the upstream installation instructions and review the runtime/model
licenses; MinerU is not distributed as part of this package.

```sh
ac-document doctor-mineru --executable /path/to/mineru
ac-document parse-pdf-mineru input.pdf --executable /path/to/mineru \
  --job-dir ./ocr-job --language en --timeout-seconds 900

ac-document doctor-mineru --api-url https://ocr.example.org
ac-document parse-pdf-mineru input.pdf --api-url https://ocr.example.org \
  --token-env MINERU_SERVICE_TOKEN --job-dir ./remote-ocr-job --language ch

ac-document export-rich-document ./ocr-job/bundle/source.html \
  --pdf-source-manifest ./ocr-job/bundle/manifest.json --output-dir ./publication
```

`doctor_mineru` and `parse_pdf_mineru` are also public Python APIs. Both commands
require exactly one executable or service URL. Language currently supports `en`
and `ch`; parsing uses `auto`, with formula and table extraction enabled.
`doctor` checks version/health, not model completeness or OCR accuracy. Run a
small PDF to check inference. Local MinerU inherits the caller's runtime/model
configuration and may download missing models according to that configuration.
The local timeout stops the owned process group, allowing up to 15 seconds
for cleanup; it never stops a user's existing service. Local execution currently
requires macOS or Linux. Use service mode on Windows. Local child processes
bypass environment proxies so temporary loopback service traffic stays local;
pre-download models if your network requires a download proxy.

A service call uploads the PDF to the explicitly selected service. Remote URLs
require HTTPS; loopback development servers may use HTTP. Credentials in URLs,
query parameters and redirects are rejected. Optional bearer authentication
uses the named environment variable; only its name is saved, never its value.
Environment proxies are disabled. The service needs to support the upstream
`/health`, `/tasks`, `/tasks/{id}` and `/tasks/{id}/result` endpoints, including
ZIP results. Returned URLs and server filesystem paths are never followed.
This is not the hosted MinerU API or an arbitrary OCR endpoint adapter.

A job directory belongs to one PDF digest and one configuration. Invoke the
same command to reuse a completed verified bundle or resume polling/downloading
a saved service task. A timeout does not cancel remote work. Interrupted or
ambiguous submissions without a saved task ID are not resubmitted automatically.
A missing/expired server task requires inspecting the service before explicitly
starting a new job directory. Local failures similarly require a new job after
inspection; local inference checkpoints are not resumable. A downloaded result
can be imported again locally after interruption without another upload.
Keep private job directories on a trusted local filesystem; they contain the
original PDF and raw OCR result. Do not edit `job.json` to force retries.

Original PDF inputs have no fixed byte-size ceiling; processing still depends
on available memory, disk and the selected service. Downloads and total extracted bytes are limited
to 512 MiB with at most 10,000 archive entries. Traversal paths, links, duplicate
members and encrypted archives are rejected before importing results. The
source-bundle limits and coverage warnings above apply as well. No automatic
fallback to another machine or provider occurs. Web settings, plugin workflow
integration, shared credential storage and Mathpix remain separate integrations.

### Project configuration and workflow integration

`configure-mineru --config-path <project>/.ac/mineru.json` saves one of the
same executable/service configurations, plus `--language en|ch` and optional
`--token-env`. The file contains no credential values. It can be read before
MinerU is installed; `doctor-configured-mineru --config-path ...` checks runtime
availability. `parse-pdf-configured-mineru PDF --config-path ... --job-dir ...`
uses that configuration. ALC Web and the agent plugin use this same project
path. An existing job still requires its original configuration when resumed;
changing a profile is not a migration or authorization to upload a file.

Python consumers can call `AcDocumentService.parse_pdf_source(source,
manifest=...)` to obtain the same verified provenance-bound RichDocument used
by export, without creating a publication directory. `parse_pdf_mineru` also
accepts an optional `checkpoint` callback for caller-controlled pause/cancel;
raising stops owned local execution, while already submitted remote work is
preserved for subsequent polling. A network operation may take up to its
current bounded request timeout to yield to a stop request.

An explicitly approved OCR text revision can be published with
`ac_document.pdf_revision.publish_reviewed_pdf_source()`. The caller owns visual
proofreading and user approval; Foundation only verifies the source-bound
receipt and preserves document structure and assets. Publication creates a new
bundle with the original manifest/source and approval receipt as hashed
evidence. PDF bytes, provider, resources, entries and page mappings remain
bound to the original manifest. Rich-document provenance sets `proofread=true`
only for this verified revision and includes its review identity. This records
that review occurred; it does not guarantee perfect recognition.
An existing output is reusable only for the same source bundle, candidate,
reviewed bytes, and complete review receipt. A different receipt, including a
model-to-human approval change, requires a new output directory; a conflicting
retry is rejected without changing the existing bundle.

Revision receipts v2 distinguish model-only adoption (`reviewer=model`,
`approved=false`, nonnegative uncertainty count) from human approval
(`reviewer=user`, `approved=true`, zero unresolved uncertainties). Both preserve
source integrity. Model-only provenance keeps `proofread=false` and records
`proofreading.review_mode=model`; it must not be displayed as human-reviewed.
Human revisions record `review_mode=human`. Optional string maps retain manual
edits and uncertainty dispositions in the hashed receipt. Existing v1 human
receipts remain valid. These records express review status, not guaranteed OCR
accuracy or exact visual reproduction of the PDF.

MinerU normalization conservatively retains a unique, short discarded header when
its geometry places it immediately above a small image beside a level-one title.
It preserves the exact OCR text and the image; it does not infer chapter numbers
from images. Repeated headers, numeric page furniture, and ambiguous layouts stay
excluded. Original provider evidence and normalized page items are both retained;
restored labels also increase expected-entry counts so they cannot mask missing
body entries.

Before generating source HTML and section structure, MinerU import also filters
misclassified running section headers automatically, independently of any later
proofreading step. A short numbered title in the top margin must have a matching
body heading and repeated page-position evidence. A single margin occurrence
requires the body heading on the same page and a pattern established by other
repeated section headers. Ambiguous cases remain included. Inferred headers are
recorded as excluded entries; normalized evidence records their original type and
classification reason, while raw provider files remain unchanged. Existing bundles
are immutable and must be imported again to apply this filtering.

Local OCR retains the last 64 KiB of subprocess output in the private
`local-ocr.log` file and records `exit_code` and a diagnostic category in
`local-ocr.json`. Nonzero exits persist as `failed`, including on subsequent
inspection; timeout, memory and signal evidence are distinguished from an
unspecified provider failure. Missing exception detail is not treated as proof
of a timeout or resource shortage. Known
credential environment values and Bearer credentials are redacted. Failed
local executions are not automatically repeated. Page restoration also
supports MinerU `index` blocks exported as text, using the same exact original
line and geometry checks as ordinary paragraphs.
