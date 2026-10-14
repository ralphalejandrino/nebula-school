#!/usr/bin/env python3
"""Render a study markdown file to a readable PDF.

Study sheets are delivered as PDF, never as raw markdown: the .md stays as the
editable source; the PDF is the deliverable.

Route: python-markdown -> styled HTML -> Chromium print-to-PDF via Playwright.
Chromium is already needed by the LMS scrapers, so it is the one renderer
guaranteed to be present.

Usage:
    md2pdf.py INPUT.md [-o OUTPUT.pdf]

Run it with the venv Playwright uses, and with its LD_LIBRARY_PATH set — the same
environment `lms.sh` builds:

    python md2pdf.py in.md -o out.pdf
"""

import argparse
import pathlib
import re
import sys

import markdown

# 🔴 REWSL has NO emoji font — `fc-list | grep -i emoji` returns nothing — so every
# emoji in the source renders as a tofu box in the PDF. The source markdown leans
# on 🔴/🔑/⚠ heavily, so strip them here rather than writing emoji-free source and
# losing the terminal legibility. Arrows and typographic symbols (→ ↔ ≠ · —) are in
# the serif fonts and are deliberately NOT touched.
# ⚠ Every escape here MUST be exactly 8 hex digits after \U. Writing `\U0000E0020`
# (9 digits) silently parses as \U0000E002 + a literal "0", which turned the class
# into the range 0-\U0000E007 and stripped every letter in the document. Caught only
# because the PDF was rasterised and looked at — see [[doc-format-strict-conformance]].
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF"      # pictographs, symbols, transport, supplemental
    "\U00002600-\U000027BF"       # misc symbols + dingbats (✅ ⚠ ❌ …)
    "\U00002B00-\U00002BFF"       # misc symbols and arrows (⬜ …)
    "\U0000FE0F\U0000FE0E"        # variation selectors
    "\U0000200D\U000024C2\U00002139]"
)


def strip_emoji(text: str) -> str:
    """Drop emoji and tidy the whitespace they leave behind."""
    out = EMOJI.sub("", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    # A line that was "### 🔑 Title" must not become "### Title" with a stray space,
    # and a bullet that was "- 🔴 **x**" must not become "-  **x**".
    out = re.sub(r"(?m)^(\s*(?:[-*+]|\d+\.|#{1,6})) +", r"\1 ", out)
    return out

CSS = """
@page { size: A4; margin: 14mm 13mm 15mm 13mm; }
* { box-sizing: border-box; }
body {
  font-family: "Charter", "Bitstream Charter", "Georgia", serif;
  font-size: 10.2pt; line-height: 1.5; color: #16181d; margin: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1 {
  font-size: 19pt; line-height: 1.2; margin: 0 0 2mm; color: #0f1115;
  border-bottom: 2.5px solid #16181d; padding-bottom: 2.5mm;
}
h2 {
  font-size: 13pt; margin: 8mm 0 2.5mm; color: #0f1115;
  border-bottom: 1px solid #c3c8d0; padding-bottom: 1.2mm;
  break-after: avoid; page-break-after: avoid;
}
h3 {
  font-size: 11pt; margin: 5.5mm 0 1.8mm; color: #22262e;
  break-after: avoid; page-break-after: avoid;
}
p { margin: 0 0 2.4mm; }
ul, ol { margin: 0 0 2.8mm; padding-left: 5.5mm; }
li { margin: 0 0 1.1mm; }
strong { color: #000; }
em { color: #33383f; }

/* Tables are the whole point of a cram sheet — they must never split badly. */
table {
  border-collapse: collapse; width: 100%; margin: 2.5mm 0 4mm;
  font-size: 9.3pt; break-inside: avoid; page-break-inside: avoid;
}
th, td { border: 1px solid #b9c0ca; padding: 1.7mm 2.2mm; text-align: left; vertical-align: top; }
th { background: #eceff3; font-weight: 700; }
tr:nth-child(even) td { background: #f7f8fa; }

code {
  font-family: "DejaVu Sans Mono", "Menlo", monospace; font-size: 8.9pt;
  background: #eef0f4; padding: 0.4mm 1.1mm; border-radius: 2px;
}
pre {
  background: #f5f6f8; border: 1px solid #d6dae0; border-left: 3px solid #6a727e;
  padding: 2.5mm 3mm; overflow-x: auto; font-size: 8.7pt; line-height: 1.42;
  break-inside: avoid; page-break-inside: avoid; margin: 2.5mm 0 4mm;
}
/* 2026-10-14: wrap, never clip. A long URL in a build card ran off the page edge and print
   cannot scroll, so the end of the code was silently lost. */
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
pre code { background: none; padding: 0; font-size: inherit; }

blockquote {
  margin: 2.5mm 0; padding: 1.5mm 0 1.5mm 3.5mm;
  border-left: 3px solid #b9c0ca; color: #3a4048;
}
hr { border: 0; border-top: 1px solid #c3c8d0; margin: 6mm 0; }
a { color: #16181d; text-decoration: none; }
"""

HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title><style>{css}</style></head><body>{body}</body></html>"""


def render(src: pathlib.Path, out: pathlib.Path) -> None:
    body = markdown.markdown(
        strip_emoji(src.read_text(encoding="utf-8")),
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
    )
    html = HTML.format(title=src.stem, css=CSS, body=body)

    tmp = out.with_suffix(".render.html")
    tmp.write_text(html, encoding="utf-8")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(tmp.as_uri(), wait_until="load")
            page.pdf(
                path=str(out),
                format="A4",
                print_background=True,
                margin={"top": "14mm", "bottom": "15mm", "left": "13mm", "right": "13mm"},
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=(
                    '<div style="width:100%;font-size:7.5pt;color:#7c838d;'
                    'font-family:Georgia,serif;padding:0 13mm;text-align:right;">'
                    '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'
                ),
            )
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render study markdown to a readable PDF.")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    a = ap.parse_args()

    src = pathlib.Path(a.input).expanduser().resolve()
    if not src.is_file():
        print(f"md2pdf: no such file: {src}", file=sys.stderr)
        return 1
    out = pathlib.Path(a.output).expanduser().resolve() if a.output else src.with_suffix(".pdf")

    render(src, out)
    if not out.is_file() or out.stat().st_size < 1024:
        print(f"md2pdf: 🔴 produced no usable PDF at {out}", file=sys.stderr)
        return 1
    print(f"{out}  ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
