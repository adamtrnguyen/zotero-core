"""Paper resolution and `save_paper`: offline, against canned responses and the fakes.

The parsers are tested on the shapes each source actually returns; the verb is tested
through the real `create_item` / `import_attachment` gates with a `FakePaperResolver`.
"""

from __future__ import annotations

import pytest

from zotero_core.application.services.papers import resolve_paper, save_paper
from zotero_core.domain.errors import Reason, WriteBlocked
from zotero_core.domain.ports.paper_resolver import PaperResolver, ResolvedPaper
from zotero_core.infrastructure.http import papers as web
from zotero_core.infrastructure.http.papers import WebPaperResolver

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All
      You Need</title>
    <summary>  The dominant sequence transduction models...  </summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <arxiv:comment>15 pages, 5 figures</arxiv:comment>
    <arxiv:journal_ref>NeurIPS 2017</arxiv:journal_ref>
  </entry>
</feed>"""

ARXIV_MISSING = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>"""


# ---------------------------------------------------------------- URL recognition


@pytest.mark.parametrize(
    ("url", "arxiv_id"),
    [
        ("https://arxiv.org/abs/1706.03762", "1706.03762"),
        ("https://arxiv.org/abs/1706.03762v7", "1706.03762"),
        ("https://arxiv.org/pdf/2401.12345v2.pdf", "2401.12345"),
        ("http://arxiv.org/html/2401.12345v1", "2401.12345"),
        ("https://arxiv.org/abs/hep-th/9901001", "hep-th/9901001"),
    ],
)
def test_arxiv_ids_are_found_in_every_url_shape(url, arxiv_id):
    match = web._ARXIV_ID.search(url)
    assert match is not None
    assert match.group("id") == arxiv_id


@pytest.mark.parametrize(
    ("text", "doi"),
    [
        ("https://doi.org/10.1038/s41586-021-03819-2", "10.1038/s41586-021-03819-2"),
        ("doi:10.1145/3442188.3445922.", "10.1145/3442188.3445922"),
        ("https://dl.acm.org/doi/10.1145%2F3442188.3445922", "10.1145/3442188.3445922"),
        ("https://www.nature.com/articles/s41586-021-03819-2", "10.1038/s41586-021-03819-2"),
        ("https://proceedings.mlr.press/v202/foo23a.html", None),
    ],
)
def test_doi_extraction(text, doi):
    assert web.doi_in(text) == doi


# ---------------------------------------------------------------- arXiv


def test_arxiv_is_a_preprint_carrying_arxivs_own_doi():
    paper = web.parse_arxiv_atom(ARXIV_ATOM, "1706.03762")
    assert paper.source == "arxiv"
    assert paper.item_type == "preprint"
    assert paper.fields["title"] == "Attention Is All You Need"  # whitespace folded
    # The DOI is what makes a repeat save a duplicate BLOCK rather than a title warning.
    assert paper.fields["DOI"] == "10.48550/arXiv.1706.03762"
    assert paper.fields["archiveID"] == "arXiv:1706.03762"
    assert paper.fields["date"] == "2017-06-12"
    assert "Comment: 15 pages, 5 figures" in paper.fields["extra"]
    assert paper.creators[0] == {
        "creatorType": "author",
        "firstName": "Ashish",
        "lastName": "Vaswani",
    }
    assert paper.pdf_url == "https://arxiv.org/pdf/1706.03762"


def test_an_unknown_arxiv_id_is_refused_not_saved_empty():
    with pytest.raises(WriteBlocked) as exc:
        web.parse_arxiv_atom(ARXIV_MISSING, "9999.99999")
    assert exc.value.code == Reason.PAPER_UNRESOLVED


# ---------------------------------------------------------------- DOI (CSL-JSON)


def test_csl_conference_paper_maps_to_zotero_fields():
    csl = {
        "type": "paper-conference",
        "title": "Deep Residual Learning",
        "container-title": "CVPR 2016",
        "author": [{"given": "Kaiming", "family": "He"}, {"literal": "The Consortium"}],
        "issued": {"date-parts": [[2016, 6, 27]]},
        "page": "770-778",
        "publisher": "IEEE",
        "abstract": "<jats:p>Deeper networks are harder.</jats:p>",
        "link": [{"URL": "https://example.org/paper.pdf", "content-type": "application/pdf"}],
    }
    paper = web.from_csl(csl, doi="10.1109/CVPR.2016.90")
    assert paper.item_type == "conferencePaper"
    assert paper.fields["proceedingsTitle"] == "CVPR 2016"
    assert paper.fields["date"] == "2016-06-27"
    assert paper.fields["DOI"] == "10.1109/CVPR.2016.90"
    assert paper.fields["abstractNote"] == "Deeper networks are harder."  # JATS stripped
    assert paper.creators[1] == {"creatorType": "author", "name": "The Consortium"}
    assert paper.pdf_url == "https://example.org/paper.pdf"


def test_csl_without_a_title_is_refused():
    with pytest.raises(WriteBlocked) as exc:
        web.from_csl({"type": "article-journal"}, doi="10.1/x")
    assert exc.value.code == Reason.PAPER_UNRESOLVED


# ---------------------------------------------------------------- citation meta tags

PMLR_HTML = """<html><head><title>Some Paper | PMLR</title>
<meta name="citation_title" content="Some   Paper">
<meta name="citation_author" content="Doe, Jane">
<meta name="citation_author" content="John Smith">
<meta name="citation_conference_title" content="Proceedings of ICML">
<meta name="citation_publication_date" content="2023/07/03">
<meta name="citation_firstpage" content="10"><meta name="citation_lastpage" content="20">
<meta name="citation_pdf_url" content="https://proceedings.mlr.press/v202/doe23a/doe23a.pdf">
</head><body></body></html>"""


def test_meta_tags_resolve_a_conference_paper():
    paper = web.from_meta_tags(web.parse_meta_tags(PMLR_HTML), "https://proceedings.mlr.press/x")
    assert paper.source == "meta"
    assert paper.item_type == "conferencePaper"
    assert paper.fields["title"] == "Some Paper"
    assert paper.fields["proceedingsTitle"] == "Proceedings of ICML"
    assert paper.fields["date"] == "2023-07-03"
    assert paper.fields["pages"] == "10-20"
    assert [c["lastName"] for c in paper.creators] == ["Doe", "Smith"]
    assert paper.pdf_url is not None
    assert paper.pdf_url.endswith("doe23a.pdf")


@pytest.mark.parametrize(
    "title", ["Client Challenge", "Verifying your browser | OpenReview", "Redirecting..."]
)
def test_the_bot_walls_zotlink_saved_as_papers_are_refused(monkeypatch, title):
    """Each of these titles was saved to the library as a "paper" by ZotLink."""
    resolver = WebPaperResolver()
    monkeypatch.setattr(
        resolver, "_get", lambda url, accept: f"<html><title>{title}</title></html>"
    )
    with pytest.raises(WriteBlocked) as exc:
        resolver.resolve("https://openreview.net/forum?id=abc")
    assert exc.value.code == Reason.PAPER_UNRESOLVED
    assert "bot check" in exc.value.reason


def test_a_page_without_scholarly_metadata_is_refused_not_saved_under_its_title(monkeypatch):
    resolver = WebPaperResolver()
    monkeypatch.setattr(resolver, "_get", lambda url, accept: "<html><title>My Blog</title></html>")
    with pytest.raises(WriteBlocked) as exc:
        resolver.resolve("https://example.com/post")
    assert exc.value.code == Reason.PAPER_UNRESOLVED


def test_a_page_with_citation_doi_goes_through_the_doi_record(monkeypatch):
    resolver = WebPaperResolver()
    html = (
        '<html><title>x</title><meta name="citation_title" content="T">'
        '<meta name="citation_doi" content="10.5555/abc">'
        '<meta name="citation_pdf_url" content="https://ex.org/a.pdf"></html>'
    )
    csl = '{"type": "article-journal", "title": "From Crossref", "container-title": "J"}'
    monkeypatch.setattr(resolver, "_get", lambda url, accept: csl if "doi.org" in url else html)
    paper = resolver.resolve("https://publisher.example/article/1")
    assert paper.source == "doi"
    assert paper.fields["title"] == "From Crossref"
    assert paper.pdf_url == "https://ex.org/a.pdf"  # the landing page's PDF is kept


def test_the_web_resolver_satisfies_the_port():
    assert isinstance(WebPaperResolver(), PaperResolver)


# ---------------------------------------------------------------- save_paper

URL = "https://arxiv.org/abs/2401.00001"
PAPER = ResolvedPaper(
    source="arxiv",
    item_type="preprint",
    fields={"title": "A Saved Paper", "DOI": "10.48550/arXiv.2401.00001"},
    creators=({"creatorType": "author", "firstName": "A", "lastName": "Author"},),
    pdf_url="https://arxiv.org/pdf/2401.00001",
)


def test_save_creates_the_item_and_imports_the_pdf(session):
    session.papers.papers[URL] = PAPER
    result = save_paper(URL, session=session)
    assert result["ok"] is True
    assert result["source"] == "arxiv"
    assert result["pdf"]["ok"] is True
    assert result["pdf"]["parent_item_key"] == result["item_key"]
    assert session.papers.downloads == [PAPER.pdf_url]
    assert result["undo_call"] == f"trash_items(['{result['item_key']}'])"


def test_a_repeat_save_is_a_doi_block(session):
    session.papers.papers[URL] = PAPER
    save_paper(URL, attach_pdf=False, session=session)
    with pytest.raises(WriteBlocked) as exc:
        save_paper(URL, attach_pdf=False, session=session)
    assert exc.value.code == Reason.DUPLICATE_ITEM


def test_a_failed_pdf_is_reported_not_raised_because_the_item_exists(session):
    session.papers.papers[URL] = PAPER
    session.papers.pdf_bytes = None
    result = save_paper(URL, session=session)
    assert result["ok"] is True
    assert result["pdf"]["ok"] is False
    assert "fake download failure" in result["pdf"]["error"]


def test_the_download_is_deleted_after_import(session, tmp_path):
    session.papers.papers[URL] = PAPER
    save_paper(URL, session=session)
    assert not list(tmp_path.glob("tmp*.pdf"))


def test_an_unresolvable_url_writes_nothing(session):
    before = len(session.cookjohn.calls) if hasattr(session.cookjohn, "calls") else None
    with pytest.raises(WriteBlocked) as exc:
        save_paper("https://example.com/nothing", session=session)
    assert exc.value.code == Reason.PAPER_UNRESOLVED
    if before is not None:
        assert len(session.cookjohn.calls) == before


def test_resolve_previews_and_checks_duplicates_without_writing(session):
    session.papers.papers[URL] = PAPER
    preview = resolve_paper(URL, session=session)
    assert preview["transport"] == "none"
    assert preview["paper"]["fields"]["title"] == "A Saved Paper"
    assert preview["duplicate_check"]["verdict"] == "ok"
    save_paper(URL, attach_pdf=False, session=session)
    assert resolve_paper(URL, session=session)["duplicate_check"]["verdict"] == "block"
