"""Wrap the UI source into a standalone document for static hosting.

`ui/index.html` is authored as a fragment (no doctype, no <head>) because that is
the form the artifact host wraps at publish time. A browser loading that fragment
directly falls into quirks mode and, with no viewport meta, renders at desktop
width on phones. This script emits `public/index.html` — the same page inside a
real document shell — which is what Vercel serves.

    python ui/build.py
"""

import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "ui", "index.html")
OUT_DIR = os.path.join(BASE, "public")
OUT = os.path.join(OUT_DIR, "index.html")

DESCRIPTION = (
    "uniLog turns a six-column supplier export into a 252-column commerce-ready "
    "product catalog, with a provenance trail behind every published value."
)

# Inline SVG favicon: the accent gradient on the page's own near-black ground.
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='1' x2='1' y2='0'%3E"
    "%3Cstop offset='0' stop-color='%2316256B'/%3E"
    "%3Cstop offset='.5' stop-color='%232F7BFF'/%3E"
    "%3Cstop offset='1' stop-color='%236C4DF6'/%3E"
    "%3C/linearGradient%3E%3C/defs%3E"
    "%3Crect width='32' height='32' rx='7' fill='%23050506'/%3E"
    "%3Crect x='9' y='9' width='14' height='14' rx='4' fill='url(%23g)'/%3E"
    "%3C/svg%3E"
)


def build() -> str:
    src = open(SRC, encoding="utf-8").read()

    title_match = re.search(r"<title>(.*?)</title>", src)
    title = title_match.group(1) if title_match else "uniLog"
    body = src.replace(title_match.group(0), "", 1) if title_match else src

    # The font <link> tags belong in <head>, not the body.
    links = re.findall(r'<link rel="(?:preconnect|stylesheet)"[^>]*>', body)
    for link in links:
        body = body.replace(link, "", 1)

    head = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="color-scheme" content="dark">',
            '<meta name="theme-color" content="#050506">',
            '<meta name="description" content="%s">' % DESCRIPTION,
            "<title>%s</title>" % title,
            '<link rel="icon" href="%s">' % FAVICON,
            '<meta property="og:title" content="%s">' % title,
            '<meta property="og:description" content="%s">' % DESCRIPTION,
            '<meta property="og:type" content="website">',
            *["  " + link for link in links],
            "</head>",
            "<body>",
        ]
    )

    return head + "\n" + body.strip() + "\n</body>\n</html>\n"


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    html = build()
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("wrote %s  (%.0f KB)" % (os.path.relpath(OUT, BASE), len(html) / 1024))
