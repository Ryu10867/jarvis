"""Builds the website: packaging/site/page.html (the page's content: title, styles, markup, script)
->  docs/index.html (a complete HTML document, served by GitHub Pages from the docs/ folder).

    python packaging/make_site.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE, DOCS = ROOT / "packaging" / "site" / "page.html", ROOT / "docs"
DESCRIPTION = ("J.A.R.V.I.S.: an offline, Iron-Man-style voice assistant for Windows and macOS. Opens apps, runs your "
               "browser, tracks deadlines, reads your screen and guards your laptop. Free, open source, no cloud.")
ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3CradialGradient "
        "id='g' cx='.38' cy='.36'%3E%3Cstop offset='0' stop-color='%23fff6dc'/%3E%3Cstop offset='.45' stop-color='%23f2b84b'/%3E"
        "%3Cstop offset='1' stop-color='%23a06a12'/%3E%3C/radialGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' "
        "fill='%2306090f'/%3E%3Ccircle cx='32' cy='32' r='17' fill='url(%23g)'/%3E%3Ccircle cx='32' cy='32' r='24' fill='none' "
        "stroke='%23f2b84b' stroke-opacity='.45' stroke-width='2'/%3E%3C/svg%3E")


def main():
    page = PAGE.read_text(encoding="utf-8")
    m = re.search(r"<title>(.*?)</title>", page)
    title = m.group(1) if m else "J.A.R.V.I.S."
    body_at = page.index("</style>") + len("</style>")
    head, body = page[:body_at], page[body_at:]
    head = head.replace(f"<title>{title}</title>", "").replace('<meta name="claude:viewport-fit-sides" content="cover">', "")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title} Offline voice assistant for Windows and macOS</title>
<meta name="description" content="{DESCRIPTION}">
<meta name="theme-color" content="#06090f">
<meta property="og:title" content="{title} Your laptop answers.">
<meta property="og:description" content="{DESCRIPTION}">
<meta property="og:type" content="website">
<link rel="icon" href="{ICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<style>body{{margin:0}}img{{max-width:100%}}[hidden]{{display:none!important}}</style>
{head.strip()}
</head>
<body>
{body.strip()}
</body>
</html>
"""
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(html, encoding="utf-8", newline="\n")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    print(f"  docs/index.html  ({len(html) // 1024} KB)")


if __name__ == "__main__":
    main()
