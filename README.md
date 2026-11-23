# pdf2md-for-rag
Batch-convert scientific PDFs to structured Markdown for RAG ingestion, with optional figure export and DOI metadata.

The converter uses Docling to extract document structure, tables, and formulas. It writes one Markdown file per PDF and adds a YAML metadata block with the source filename, article title and DOI when available, and conversion status. Figures are not exported by default; enable figure export when they matter to your downstream workflow.

## Why pre-convert PDFs for RAG?

A scientific PDF is designed to be read as a page. A retrieval-augmented
generation (RAG) pipeline needs a different representation: text it can
split into meaningful chunks and retrieve when a question is asked.
Converting a paper to Markdown ahead of time creates a reusable copy for
that workflow while leaving the original PDF unchanged.

- **Reuse the conversion.** Convert each PDF once, then reuse the
  Markdown when experimenting with chunk sizes, changing embedding
  models, or rebuilding an index. This avoids repeating PDF parsing on
  every rebuild. Conversion itself takes time, so this benefit matters
  most when the same corpus is processed more than once.
- **Preserve table structure for downstream parsing.** Structured
  conversion can preserve row boundaries and column headers that a
  plain-text extractor may flatten. This helps keep numerical values
  associated with the right sample and units, but complex tables still
  need a spot check.
- **Remove extraction noise without flattening formulas.** Markdown
  post-processing removes some layout artifacts and consecutive duplicate
  paragraphs. LaTeX notation makes symbols, indices, and fractions explicit
  and can improve readability even when it uses more characters or tokens
  than a flattened equation. The goal is cleaner text, not the shortest
  possible output at the expense of scientific meaning.
- **Keep a compact working corpus.** For text-only retrieval, the
  default mode does not export separate figure files. If microscopy
  images, plots, or other figures matter to the workflow, use
  `--images` and account for the figure files in the storage comparison.
- **Measure tokens where they are used.** A smaller file on disk does
  not necessarily contain fewer tokens. Measure the text passed to
  the embedding model separately from the retrieved context passed
  to the answer model. The converter prepares the documents; the
  downstream pipeline determines what is indexed and retrieved.

## Worked example: one scientific paper

This example compares `Volochaev2026.pdf`, its extracted plain text, and
its converted Markdown (`Volochaev2026.md`).

| Representation | Reported size on disk (bytes) | Characters (approx.) | Words (approx.) | Estimated text tokens |
| --- | ---: | ---: | ---: | ---: |
| Original PDF | 2,658,304 | — | — | Not applicable |
| Plain text extracted from the PDF | Not reported | 65,000 | 9,800 | 17,000–18,000 |
| Converted Markdown | 167,936 | 62,000 | 9,500 | 15,500–16,500 |

The Markdown file occupies 93.7% less disk space than the PDF
(15.8× smaller, a difference of 2,490,368 bytes). This compares the two
files, not their combined footprint when both are retained. If you export
figures, count the `*_artifacts/` directory as part of the working copy.

The midpoints of the estimated token ranges are 17,500 for extracted text
and 16,000 for Markdown: about 1,500 fewer tokens (8.6%). The plain-text
baseline included page markers and repeated headers; the Markdown retained
structured tables and formulas. LaTeX can expand individual equations even
when the document as a whole contains fewer tokens.

These token counts are estimates. The tokenizer encoding and version,
plain-text extraction settings, and whether the count includes YAML front
matter were not reported. This is one paper, not a corpus-wide benchmark.
It does not measure conversion time, downstream table-parsing accuracy,
or tokens sent to a model per query.

## Quick start

The commands below are for Linux or a compatible shell. Run them from the project directory containing `batch_convert.py`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install docling pypdf PyYAML
mkdir -p Articles
```

Copy a few PDFs into `Articles/`, then run:

```bash
python batch_convert.py
```

The script processes `Articles/*.pdf` without scanning subdirectories. For example, `Articles/article.pdf` produces `Articles/article.md`. Start with one or two representative papers and inspect their tables, formulas, and metadata before processing the full collection.

## Conversion options

| Command | Behavior |
| --- | --- |
| `python batch_convert.py` | Write Markdown without exporting figure files. Skip PDFs that already have a corresponding `.md` file. |
| `python batch_convert.py --images` | Save extracted figures in a sibling `*_artifacts/` directory and reference them from the Markdown. Existing `.md` files are still skipped. |
| `python batch_convert.py --force` | Reprocess PDFs and overwrite existing Markdown files. |
| `python batch_convert.py --images --force` | Reprocess PDFs and export figures, including for files previously converted without images. |

Check `conversion.log` in the project directory for conversion errors and DOI conflicts.

## Optional: GROBID for article metadata

Without GROBID, the script can still convert PDFs. It looks for DOI candidates in the first page's text layer and uses a Markdown heading as a fallback for the title. If the GROBID request fails, the diagnostic is recorded in the Markdown metadata.

To enable GROBID metadata extraction, make sure `curl` is installed and start a local GROBID service on port `8070`. For example, with Docker, run this in a separate terminal:

```bash
docker run --rm --init --ulimit core=0 \
  -p 127.0.0.1:8070:8070 lfoppiano/grobid:0.8.2
```

Check that the service is ready before running the converter:

```bash
curl http://localhost:8070/api/isalive
```

When both GROBID and the PDF text layer provide the same DOI, the script records it as `matched`. A DOI found in only one source is marked `single_source`. If candidates disagree, the script leaves the DOI unset, records the candidates, and marks the result `conflict` rather than guessing. If no candidate is found, it records `missing`. These statuses describe agreement between extraction sources, not independent verification of the DOI.

## Before indexing

- Spot-check papers with multi-column layouts, complex tables, equations, or scanned pages.
- If you use `--images`, keep each Markdown file with its `*_artifacts/` directory. Check whether your downstream system follows local image references or indexes only the text.
- Keep the original PDFs separately. The Markdown is a representation for retrieval, not a replacement for the published document.
