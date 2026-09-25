"""Paper metadata from the web: arXiv's API, DOI content negotiation, citation meta tags.

Three sources, tried in this order, stdlib only (`dependencies = []`):

  arxiv  `export.arxiv.org/api/query` (Atom). Saved as a `preprint` carrying arXiv's own
         DOI, 10.48550/arXiv.<id> -- which is what makes a second save of the same paper
         a DOI match that `create_item`'s duplicate gate BLOCKS, not a title warning.
  doi    `https://doi.org/<doi>` with `Accept: application/vnd.citationstyles.csl+json`,
         answered by Crossref/DataCite for any registered DOI.
  meta   the `citation_*` <meta> tags (Highwire/Google Scholar) on the landing page, which
         PMLR, OpenReview, ACL, CVF and most publishers emit. A page that carries a
         `citation_doi` is resolved through `doi` instead, for the richer record.

A page with none of these is REFUSED (`paper_unresolved`), and so is a bot wall. ZotLink's
generic scraper fell back to the HTML <title>, which is how "Client Challenge" and
"Verifying your browser | OpenReview" were saved to the library as papers.
"""

from __future__ import annotations

import json
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from zotero_core.domain.errors import Reason, WriteBlocked
from zotero_core.domain.ports.paper_resolver import ResolvedPaper

USER_AGENT = "zotero-core (+https://github.com/adamtrnguyen/zotero-core)"
TIMEOUT = 30.0
MAX_PDF_BYTES = 200 * 1024 * 1024

_ARXIV_ID = re.compile(
    r"arxiv\.org/(?:abs|pdf|html)/(?P<id>(?:\d{4}\.\d{4,5})|(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?",
    re.IGNORECASE,
)
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>?#]+)", re.IGNORECASE)

# Publishers whose article URL IS the DOI suffix, so the landing page -- and the bot wall
# in front of it (nature.com answers "Client Challenge") -- never has to be fetched.
_URL_TO_DOI = (
    (re.compile(r"nature\.com/articles/(?P<suffix>[^/?#.]+)", re.IGNORECASE), "10.1038/"),
)
_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# Titles a bot wall or an interstitial serves instead of the page. Matched case-insensitively
# against the HTML <title>; every one of the first three was saved as a "paper" by ZotLink.
_BOT_WALL = re.compile(
    r"client challenge|verifying (?:you are human|your browser)|redirecting|just a moment|"
    r"attention required|access denied|are you a robot|captcha",
    re.IGNORECASE,
)

# CSL-JSON type -> Zotero item type. Anything unlisted is saved as a journal article, the
# type Crossref's own Zotero translator falls back to.
_CSL_TYPES = {
    "article-journal": "journalArticle",
    "paper-conference": "conferencePaper",
    "proceedings-article": "conferencePaper",
    "book": "book",
    "chapter": "bookSection",
    "posted-content": "preprint",
    "report": "report",
    "thesis": "thesis",
    "dataset": "dataset",
}

# Where each item type keeps the name of the venue.
_VENUE_FIELD = {
    "journalArticle": "publicationTitle",
    "conferencePaper": "proceedingsTitle",
    "bookSection": "bookTitle",
    "preprint": "repository",
}


class WebPaperResolver:
    def __init__(self, *, timeout: float = TIMEOUT, download_dir: str | None = None):
        self.timeout = timeout
        self.download_dir = download_dir

    # ------------------------------------------------------------------ resolve

    def resolve(self, url: str) -> ResolvedPaper:
        url = url.strip()
        arxiv = _ARXIV_ID.search(url)
        if arxiv:
            return self._arxiv(arxiv.group("id"))
        doi = doi_in(url)
        if doi:
            return self._doi(doi, landing_url=url)
        return self._meta(url)

    def _arxiv(self, arxiv_id: str) -> ResolvedPaper:
        query = urllib.parse.urlencode({"id_list": arxiv_id})
        xml = self._get(
            f"https://export.arxiv.org/api/query?{query}", accept="application/atom+xml"
        )
        return parse_arxiv_atom(xml, arxiv_id)

    def _doi(self, doi: str, *, landing_url: str | None = None) -> ResolvedPaper:
        body = self._get(
            f"https://doi.org/{urllib.parse.quote(doi, safe='/')}",
            accept="application/vnd.citationstyles.csl+json",
        )
        try:
            csl = json.loads(body)
        except json.JSONDecodeError as exc:
            raise WriteBlocked(
                Reason.PAPER_UNRESOLVED, f"doi.org returned no metadata for {doi}", {"doi": doi}
            ) from exc
        return from_csl(csl, doi=doi, landing_url=landing_url)

    def _meta(self, url: str) -> ResolvedPaper:
        html = self._get(url, accept="text/html,application/xhtml+xml")
        tags = parse_meta_tags(html)
        title = tags.get("title_tag", [""])[0]
        if _BOT_WALL.search(title) and not tags.get("citation_title"):
            raise WriteBlocked(
                Reason.PAPER_UNRESOLVED,
                f"the site answered with a bot check ({title!r}), not the paper",
                {"url": url, "page_title": title},
            )
        doi = (tags.get("citation_doi") or [None])[0]
        if doi:
            paper = self._doi(doi_in(doi) or doi, landing_url=url)
            pdf = (tags.get("citation_pdf_url") or [None])[0]
            if pdf and not paper.pdf_url:
                paper = ResolvedPaper(
                    paper.source, paper.item_type, paper.fields, paper.creators, pdf, paper.notes
                )
            return paper
        return from_meta_tags(tags, url)

    # ------------------------------------------------------------------ PDFs

    def download_pdf(self, pdf_url: str) -> str:
        request = urllib.request.Request(
            pdf_url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(MAX_PDF_BYTES + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"PDF download failed: {exc}") from exc
        if len(data) > MAX_PDF_BYTES:
            raise RuntimeError(f"PDF larger than {MAX_PDF_BYTES} bytes; not downloaded")
        if not data.startswith(b"%PDF"):
            raise RuntimeError(f"{pdf_url} did not return a PDF (got {data[:15]!r})")
        handle = tempfile.NamedTemporaryFile(
            prefix="zotero-core-paper-", suffix=".pdf", dir=self.download_dir, delete=False
        )
        with handle:
            handle.write(data)
        return handle.name

    # ------------------------------------------------------------------ transport

    def _get(self, url: str, *, accept: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            raise WriteBlocked(
                Reason.PAPER_UNRESOLVED,
                f"{url} answered HTTP {exc.code}"
                + (
                    " -- likely a bot wall; try the paper's DOI or arXiv URL"
                    if exc.code in (401, 403, 429)
                    else ""
                ),
                {"url": url, "status": exc.code},
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise WriteBlocked(
                Reason.PAPER_UNRESOLVED, f"could not reach {url}: {exc}", {"url": url}
            ) from exc


# ---------------------------------------------------------------------- pure parsing
# Module-level and side-effect free, so the tests exercise them on canned responses.


def doi_in(text: str) -> str | None:
    match = _DOI.search(urllib.parse.unquote(text))
    if match:
        return match.group(1).rstrip(".,;)")
    for pattern, prefix in _URL_TO_DOI:
        publisher = pattern.search(text)
        if publisher:
            return prefix + publisher.group("suffix")
    return None


def _person(name: str) -> dict[str, str]:
    """'Last, First' or 'First Last' -> a Zotero creator."""
    name = " ".join(name.split())
    if "," in name:
        last, first = (part.strip() for part in name.split(",", 1))
    else:
        first, _, last = name.rpartition(" ")
    if not first:
        return {"creatorType": "author", "name": last}
    return {"creatorType": "author", "firstName": first, "lastName": last}


def parse_arxiv_atom(xml: str, arxiv_id: str) -> ResolvedPaper:
    root = ET.fromstring(xml)
    entry = root.find("a:entry", _ATOM)
    title = entry.findtext("a:title", default="", namespaces=_ATOM) if entry is not None else ""
    if entry is None or not title.strip() or title.strip() == "Error":
        raise WriteBlocked(
            Reason.PAPER_UNRESOLVED, f"arXiv has no paper {arxiv_id}", {"arxiv_id": arxiv_id}
        )

    def text(tag: str) -> str:
        return " ".join((entry.findtext(tag, default="", namespaces=_ATOM) or "").split())

    fields = {
        "title": " ".join(title.split()),
        "abstractNote": text("a:summary"),
        "date": text("a:published")[:10],
        "repository": "arXiv",
        "archiveID": f"arXiv:{arxiv_id}",
        "DOI": f"10.48550/arXiv.{arxiv_id}",
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "libraryCatalog": "arXiv.org",
    }
    extra = []
    published_doi = text("arxiv:doi")
    if published_doi:
        extra.append(f"Published DOI: {published_doi}")
    journal_ref = text("arxiv:journal_ref")
    if journal_ref:
        extra.append(f"Journal reference: {journal_ref}")
    comment = text("arxiv:comment")
    if comment:
        extra.append(f"Comment: {comment}")
    if extra:
        fields["extra"] = "\n".join(extra)

    creators = tuple(
        _person(" ".join((author.findtext("a:name", default="", namespaces=_ATOM)).split()))
        for author in entry.findall("a:author", _ATOM)
    )
    return ResolvedPaper(
        source="arxiv",
        item_type="preprint",
        fields={k: v for k, v in fields.items() if v},
        creators=creators,
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
    )


def _csl_date(csl: dict) -> str:
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (csl.get(key) or {}).get("date-parts") or [[]]
        if parts and parts[0] and parts[0][0]:
            return "-".join(f"{int(p):02d}" if i else str(p) for i, p in enumerate(parts[0]))
    return ""


def from_csl(csl: dict, *, doi: str, landing_url: str | None = None) -> ResolvedPaper:
    title = csl.get("title")
    title = title[0] if isinstance(title, list) and title else title
    if not title:
        raise WriteBlocked(
            Reason.PAPER_UNRESOLVED, f"the DOI record for {doi} has no title", {"doi": doi}
        )
    item_type = _CSL_TYPES.get(csl.get("type", ""), "journalArticle")
    venue = csl.get("container-title")
    venue = venue[0] if isinstance(venue, list) and venue else venue
    abstract = re.sub(r"<[^>]+>", "", csl.get("abstract") or "")
    fields = {
        "title": " ".join(str(title).split()),
        "date": _csl_date(csl),
        "DOI": csl.get("DOI") or doi,
        "url": csl.get("URL") or landing_url or f"https://doi.org/{doi}",
        "abstractNote": " ".join(abstract.split()),
    }
    if venue and item_type in _VENUE_FIELD:
        fields[_VENUE_FIELD[item_type]] = str(venue)
    if item_type in ("journalArticle", "conferencePaper", "bookSection"):
        fields.update({"volume": str(csl.get("volume") or ""), "pages": str(csl.get("page") or "")})
    if item_type == "journalArticle":
        fields["issue"] = str(csl.get("issue") or "")
    if item_type in ("book", "bookSection", "conferencePaper", "report", "thesis"):
        fields["publisher" if item_type != "thesis" else "university"] = str(
            csl.get("publisher") or ""
        )

    creators = []
    for role, people in (("author", csl.get("author")), ("editor", csl.get("editor"))):
        for person in people or []:
            if person.get("family"):
                creators.append(
                    {
                        "creatorType": role,
                        "firstName": person.get("given", ""),
                        "lastName": person["family"],
                    }
                )
            elif person.get("literal") or person.get("name"):
                creators.append(
                    {"creatorType": role, "name": person.get("literal") or person["name"]}
                )

    pdf_url = next(
        (
            link.get("URL")
            for link in csl.get("link") or []
            if "pdf" in (link.get("content-type") or "")
        ),
        None,
    )
    return ResolvedPaper(
        source="doi",
        item_type=item_type,
        fields={k: v for k, v in fields.items() if v},
        creators=tuple(creators),
        pdf_url=pdf_url,
    )


class _MetaTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, list[str]] = {}
        self._in_title = False
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag != "meta":
            return
        attr = dict(attrs)
        name = (attr.get("name") or attr.get("property") or "").lower()
        content = attr.get("content")
        if name.startswith("citation_") and content:
            self.tags.setdefault(name, []).append(" ".join(content.split()))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)

    def result(self) -> dict[str, list[str]]:
        return {**self.tags, "title_tag": [" ".join("".join(self._title).split())]}


def parse_meta_tags(html: str) -> dict[str, list[str]]:
    parser = _MetaTags()
    parser.feed(html)
    return parser.result()


def from_meta_tags(tags: dict[str, list[str]], url: str) -> ResolvedPaper:
    def first(name: str) -> str:
        return (tags.get(name) or [""])[0]

    title = first("citation_title")
    if not title:
        raise WriteBlocked(
            Reason.PAPER_UNRESOLVED,
            "the page carries no scholarly metadata (no citation_title); refusing to save "
            "it under its HTML title",
            {"url": url, "page_title": first("title_tag")},
        )
    if first("citation_conference_title"):
        item_type, venue = "conferencePaper", first("citation_conference_title")
    elif first("citation_journal_title"):
        item_type, venue = "journalArticle", first("citation_journal_title")
    else:
        item_type, venue = "preprint", first("citation_publisher")
    date = (
        first("citation_publication_date")
        or first("citation_date")
        or first("citation_online_date")
    )
    fields = {
        "title": title,
        "date": date.replace("/", "-"),
        "url": first("citation_abstract_html_url") or url,
        "abstractNote": first("citation_abstract"),
        "volume": first("citation_volume") if item_type != "preprint" else "",
        "pages": "-".join(p for p in (first("citation_firstpage"), first("citation_lastpage")) if p)
        if item_type != "preprint"
        else "",
    }
    if venue:
        fields[_VENUE_FIELD[item_type]] = venue
    if item_type == "journalArticle":
        fields["issue"] = first("citation_issue")
    return ResolvedPaper(
        source="meta",
        item_type=item_type,
        fields={k: v for k, v in fields.items() if v},
        creators=tuple(_person(a) for a in tags.get("citation_author", [])),
        pdf_url=first("citation_pdf_url") or None,
    )
