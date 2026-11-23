from __future__ import annotations

import argparse
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ImageRefMode
from pypdf import PdfReader


ARTICLES_DIR = Path("Articles")
LOG_FILE = Path("conversion.log")
GROBID_URL = "http://localhost:8070/api/processHeaderDocument"
GROBID_TIMEOUT = 15
PDF_TEXT_LIMIT = 20000
CITATION_WINDOW = 3000
DOI_SUFFIX_LIMIT = 120

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}

DOI_PATTERN = re.compile(
    r"10\.[0-9]{4,9}/[-._;()/:A-Z0-9]+",
    re.IGNORECASE | re.ASCII,
)
DOI_LABEL = re.compile(
    r"https?://(?:dx\.)?doi\.org/|(?:dx\.)?doi\.org/|\bdoi\s*[:\-]\s*",
    re.IGNORECASE,
)
DOI_TOKEN = re.compile(
    r"10\.[0-9]{4,9}/[A-Za-z0-9._;()/:\-]{1,%d}" % DOI_SUFFIX_LIMIT,
    re.IGNORECASE | re.ASCII,
)
TOKEN_CUT = re.compile(r"(?i)https?://|www\.|doi\.org")
DOI_JOIN = re.compile(
    r"(10\.[0-9]{4,9}/[A-Za-z0-9._;()/:\-]*)\s*\n\s*(?=[a-z0-9])"
)
REMNANT_OK = re.compile(r"[0-9\-_.]*")
SUPPLEMENT_SUFFIX = re.compile(
    r"/(?:s\d+|suppl(?:ement(?:ary)?)?|mmc\d+)$",
    re.IGNORECASE,
)
CITATION_RE = re.compile(r"(?i)\bcitation\s*[:\-]")
HEADER_STOP_RE = re.compile(
    r"(?im)^\s*(?:references|bibliography|literature cited)\s*$"
)
WINDOW_STOP_RE = re.compile(
    r"(?i)\b(?:abstract|keywords|introduction|references)\b"
)
BIBTEX_DOI = re.compile(r"(?im)\bdoi\s*=\s*[{\"]\s*([^}\"]+?)\s*[}\"]")
BIBTEX_TITLE = re.compile(r"(?im)\btitle\s*=\s*\{(.+?)\}\s*,")
TITLE_EXCLUSIONS = (
    "applied energy",
    "contents lists",
    "journal homepage",
    "highlights",
    "abstract",
    "article info",
)


def clean_doi(value: str | None) -> str | None:
    """Remove an external label, trim punctuation, reject supplements."""
    if not value:
        return None

    candidate = value.strip().strip("\"'")
    label = DOI_LABEL.match(candidate)
    if label:
        candidate = candidate[label.end():].strip()

    candidate = re.sub(r"\s+", "", candidate)
    candidate = re.split(r"[?#]", candidate, maxsplit=1)[0]
    candidate = candidate.rstrip(".,;:")

    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while (
            candidate.endswith(closing)
            and candidate.count(closing) > candidate.count(opening)
        ):
            candidate = candidate[:-1]

    if not candidate or not DOI_PATTERN.fullmatch(candidate):
        return None
    if candidate.startswith("10.13039/"):
        return None
    if SUPPLEMENT_SUFFIX.search(candidate):
        return None

    return candidate


def _reduce_prefixes(dois: Iterable[str]) -> list[str]:
    """Drop a candidate whose tail is glued page number or site URL junk.

    The text layer of a PDF happily writes 10.1038/x-3 followed by the page
    number, giving 10.1038/x-31. The longer form is not a second identifier,
    it is the same one with debris attached.
    """
    unique = sorted({d for d in dois if d}, key=len)
    kept: list[str] = []

    for candidate in unique:
        lower = candidate.lower()
        glued = any(
            lower.startswith(short.lower())
            and REMNANT_OK.fullmatch(lower[len(short):])
            for short in kept
        )
        if glued:
            continue
        kept.append(candidate)

    return kept


def _dois_from_header(root: ET.Element) -> set[str]:
    """Collect DOI idno anywhere inside teiHeader.

    GROBID nests the article identifier under analytic for some
    publishers, under monogr for others, and deeper still for a few
    templates. The header endpoint returns no reference list, so a
    header-wide scan cannot capture a DOI that belongs to a cited work.
    """
    header = root.find("./tei:teiHeader", TEI_NS)
    if header is None:
        return set()

    candidates: set[str] = set()
    for node in header.iter(f"{{{TEI_NS['tei']}}}idno"):
        if node.get("type", "").strip().upper() != "DOI":
            continue
        doi = clean_doi("".join(node.itertext()))
        if doi:
            candidates.add(doi)

    return set(_reduce_prefixes(candidates))


def _title_from_header(root: ET.Element) -> str | None:
    """Read the article title, never the journal title."""
    for path in (
        ".//tei:analytic/tei:title[@level='a']",
        ".//tei:titleStmt/tei:title[@level='a']",
        ".//tei:analytic/tei:title",
    ):
        for node in root.findall(path, TEI_NS):
            title = " ".join("".join(node.itertext()).split())
            if title:
                return title
    return None


def extract_dois_from_tei(tei: str) -> set[str]:
    """Read article DOI candidates, never DOI values from references."""
    return _dois_from_header(ET.fromstring(tei))


def _dois_from_text(text: str, allow_bare: bool = False) -> list[str]:
    """Collect labelled DOI values, cutting glued tails."""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        cut = TOKEN_CUT.search(raw)
        if cut:
            raw = raw[:cut.start()]
        doi = clean_doi(raw)
        if doi and doi.lower() not in seen:
            seen.add(doi.lower())
            found.append(doi)

    for label in DOI_LABEL.finditer(text):
        window = text[label.end(): label.end() + 300]
        stop = WINDOW_STOP_RE.search(window)
        if stop:
            window = window[:stop.start()]
        window = window.lstrip(" \t\r\n\"'<([{")
        match = DOI_TOKEN.match(window)
        if match:
            add(match.group(0))

    if allow_bare:
        for match in DOI_TOKEN.finditer(text):
            add(match.group(0))

    return _reduce_prefixes(found)


def _citation_window(text: str) -> str:
    """Return the publisher Citation block, bounded by Abstract or Keywords."""
    match = CITATION_RE.search(text)
    if not match:
        return ""

    tail = text[match.end():]
    stop = re.search(r"(?i)\b(?:abstract|keywords|introduction)\b", tail)
    end = match.end() + stop.start() if stop else match.end() + CITATION_WINDOW
    end = min(end, match.start() + CITATION_WINDOW)
    return text[match.start():end]


def _join_broken_dois(text: str) -> str:
    """Glue a line break that splits a DOI, and nothing else."""
    return DOI_JOIN.sub(r"\1", text)


def _page_text(page: Any) -> str:
    """Prefer layout mode, fall back to the plain extractor."""
    for kwargs in ({"extraction_mode": "layout"}, {}):
        try:
            text = page.extract_text(**kwargs) or ""
        except Exception:
            continue
        if text.strip():
            return text
    return ""


def extract_dois_from_pdf_header(pdf_path: Path) -> set[str]:
    """Read labelled DOI candidates from page one, preferring Citation."""
    with pdf_path.open("rb") as stream:
        reader = PdfReader(stream)
        if len(reader.pages) == 0:
            return set()
        text = _page_text(reader.pages[0])

    text = _join_broken_dois(text)[:PDF_TEXT_LIMIT]
    stop = HEADER_STOP_RE.search(text)
    if stop:
        text = text[:stop.start()]

    citation = _citation_window(text)
    candidates = _dois_from_text(citation, allow_bare=True)
    if not candidates:
        candidates = _dois_from_text(text)

    return set(candidates)


def _dois_from_bibtex(text: str) -> set[str]:
    found: set[str] = set()
    for raw in BIBTEX_DOI.findall(text):
        for part in re.split(r"[,;]\s*", raw):
            doi = clean_doi(part)
            if doi:
                found.add(doi)
    return found


def _title_from_bibtex(text: str) -> str | None:
    match = BIBTEX_TITLE.search(text)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or None


def resolve_doi(
    grobid_dois: set[str],
    pdf_dois: set[str],
) -> tuple[str | None, str, str | None]:
    grobid_list = _reduce_prefixes(grobid_dois)
    pdf_list = _reduce_prefixes(pdf_dois)
    grobid_keys = {doi.lower() for doi in grobid_list}
    pdf_keys = {doi.lower() for doi in pdf_list}
    keys = grobid_keys | pdf_keys

    if not keys:
        return None, "missing", None
    if len(keys) > 1:
        return None, "conflict", None
    if grobid_keys and pdf_keys:
        return sorted(pdf_list)[0], "matched", "grobid+pdf_header"
    if pdf_keys:
        return sorted(pdf_list)[0], "single_source", "pdf_header"
    return sorted(grobid_list)[0], "single_source", "grobid"


def post_process_markdown(md_text: str) -> str:
    """Apply the existing formula cleanup and remove adjacent duplicates."""
    md_text = re.sub(r"&\s*&\s*\(\s*\d+[a-z]?\s*\)\s*&\s*", "", md_text)
    md_text = re.sub(r"&\s*&\s*(1[a-z])\s*&\s*", r"(\1)", md_text)
    md_text = re.sub(r"&\s*&", "", md_text)

    fragments = r"(Rec|distribu|as one|large\-sc|Tabl|function)"
    md_text = re.sub(
        r"\\overset\s*\{\s*\\dots\s*\}\s*\{\s*\\text\s*\{\s*"
        + fragments
        + r"\s*\}\s*\}",
        "",
        md_text,
    )
    md_text = re.sub(r"\\text\s*\{\s*" + fragments + r"\s*\}", "", md_text)

    cleaned_paragraphs: list[str] = []
    previous = ""

    for paragraph in md_text.split("\n\n"):
        stripped = paragraph.strip()
        if not stripped or (stripped == previous and len(stripped) > 20):
            continue
        cleaned_paragraphs.append(paragraph)
        previous = stripped

    return "\n\n".join(cleaned_paragraphs)


def extract_from_text(md_body: str) -> dict[str, str | None]:
    """Find a fallback title in Markdown; never search Markdown for DOI."""
    for line in md_body.splitlines()[:50]:
        line = line.strip()
        if not line.startswith("#") or len(line) <= 15:
            continue

        title = line.lstrip("#").strip()
        if title and not any(word in title.lower() for word in TITLE_EXCLUSIONS):
            return {"title": title}

    return {"title": None}


def extract_from_grobid(
    pdf_path: Path,
) -> tuple[set[str], str | None, str | None]:
    """Return DOI candidates, article title, and a diagnostic if any."""
    payload = ""
    try:
        payload = subprocess.check_output(
            [
                "curl",
                "--silent",
                "--show-error",
                "--fail",
                "--max-time",
                str(GROBID_TIMEOUT),
                "-H",
                "Accept: application/xml",
                "-F",
                f"input=@{pdf_path}",
                "--form",
                "consolidateHeader=2",
                GROBID_URL,
            ],
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )

        payload = payload.lstrip("\ufeff \t\r\n")
        if not payload:
            return set(), None, "Empty response"

        if payload.startswith("@"):
            dois = _dois_from_bibtex(payload)
            title = _title_from_bibtex(payload)
            if not dois and not title:
                return set(), None, "BibTeX response without doi and title"
            return dois, title, None

        if not payload.startswith("<"):
            return set(), None, f"Unsupported response: {payload[:160]!r}"

        root = ET.fromstring(payload)
        if root.tag != f"{{{TEI_NS['tei']}}}TEI":
            return set(), None, f"Unexpected root element {root.tag!r}"
        if root.find("./tei:teiHeader", TEI_NS) is None:
            return set(), None, "Response has no TEI header"

        return _dois_from_header(root), _title_from_header(root), None

    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        return set(), None, detail or f"curl exit code {exc.returncode}"
    except ET.ParseError as exc:
        snippet = payload[:300].replace("\n", "\\n")
        return set(), None, f"ParseError: {exc}; response starts with {snippet!r}"
    except (OSError, UnicodeError) as exc:
        return set(), None, f"{type(exc).__name__}: {exc}"


def log_message(message: str) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Пакетная конвертация PDF в Markdown.")
    parser.add_argument(
        "--images",
        action="store_true",
        help="Сохранять рисунки в папках *_artifacts.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Перезаписывать уже существующие Markdown-файлы.",
    )
    args = parser.parse_args()

    if not ARTICLES_DIR.is_dir():
        print(f"Папка {ARTICLES_DIR} не найдена.")
        return

    pdfs = sorted(ARTICLES_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"В папке {ARTICLES_DIR} нет PDF-файлов.")
        return

    pipeline = PdfPipelineOptions()
    pipeline.generate_page_images = False
    pipeline.do_formula_enrichment = True
    pipeline.do_table_structure = True
    if args.images:
        pipeline.generate_picture_images = True

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
        },
    )

    mode = "с рисунками" if args.images else "без рисунков"
    print(f"Найдено PDF: {len(pdfs)}. Режим: {mode}.")

    for pdf in pdfs:
        md_path = pdf.with_suffix(".md")
        if md_path.exists() and not args.force:
            print(f"SKIP {pdf.name}: {md_path.name} уже существует.")
            continue

        started = time.monotonic()
        print(f"{pdf.name} ... ", end="", flush=True)

        try:
            result = converter.convert(pdf, raises_on_error=False)
            if result.status == ConversionStatus.FAILURE:
                message = f"FAIL {pdf.name}: Docling: {result.errors}"
                print(message)
                log_message(message)
                continue

            grobid_dois, grobid_title, grobid_error = extract_from_grobid(pdf)
            meta: dict[str, Any] = {
                "source_pdf": pdf.name,
                "conversion_status": result.status.name,
            }

            if grobid_error:
                meta["grobid_error"] = grobid_error

            try:
                pdf_dois = extract_dois_from_pdf_header(pdf)
            except Exception as exc:
                # An unreadable PDF text layer must not discard Docling output.
                pdf_dois = set()
                meta["pdf_header_error"] = f"{type(exc).__name__}: {exc}"

            doi, doi_status, doi_source = resolve_doi(grobid_dois, pdf_dois)
            meta.update(
                doi=doi,
                doi_status=doi_status,
                doi_source=doi_source,
            )

            if doi_status == "conflict":
                meta["doi_candidates"] = {
                    "grobid": sorted(grobid_dois),
                    "pdf_header": sorted(pdf_dois),
                }

            raw_md = result.document.export_to_markdown()
            md_title = extract_from_text(raw_md)["title"]
            title = grobid_title or md_title
            if title and title[0].islower() and md_title:
                title = md_title
            meta["title"] = title

            front_matter = yaml.safe_dump(
                meta,
                allow_unicode=True,
                sort_keys=False,
            )

            if args.images:
                artifacts_dir = pdf.parent / f"{pdf.stem}_artifacts"
                result.document.save_as_markdown(
                    md_path,
                    image_mode=ImageRefMode.REFERENCED,
                    artifacts_dir=artifacts_dir,
                )
                raw_md = md_path.read_text(encoding="utf-8")

            cleaned_md = post_process_markdown(raw_md)
            md_path.write_text(
                f"---\n{front_matter}---\n\n{cleaned_md}",
                encoding="utf-8",
            )

            elapsed = time.monotonic() - started
            print(f"{result.status.name} ({elapsed:.1f} с), DOI: {doi_status}")

            if doi_status == "conflict":
                message = (
                    f"DOI CONFLICT {pdf.name}: "
                    f"GROBID={sorted(grobid_dois)!r}; "
                    f"PDF={sorted(pdf_dois)!r}"
                )
                print(f"  {message}")
                log_message(message)
            elif not doi:
                print("  DOI статьи не установлен.")

            if not title:
                print("  Заголовок статьи не найден.")

        except Exception as exc:
            message = f"ERROR {pdf.name}: {type(exc).__name__}: {exc}"
            print(message)
            log_message(message)


if __name__ == "__main__":
    main()