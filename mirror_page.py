#!/usr/bin/env python3
import os
import re
import urllib.parse as urlparse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "asset-mirror/1.0"})

ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".css", ".js",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".ico", ".mp4", ".webm"
)


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def is_absolute_url(u: str) -> bool:
    return bool(urlparse.urlparse(u).scheme)


def should_download_asset(url: str) -> bool:
    parsed = urlparse.urlparse(url)
    # data:, mailto:, javascript: etc. außen vor lassen
    if parsed.scheme in ("data", "mailto", "javascript"):
        return False
    # einfache Heuristik über Dateiendung
    path = parsed.path.lower()
    return path.endswith(ASSET_EXTENSIONS)


def download_file(url: str, target: Path):
    ensure_dir(target.parent)
    resp = SESSION.get(url, timeout=20)
    resp.raise_for_status()
    with open(target, "wb") as f:
        f.write(resp.content)


def asset_local_name(asset_url: str, base_dir: Path) -> Path:
    """
    Wandelt eine Asset-URL in einen lokalen Pfad unterhalb von base_dir um.
    Z.B. https://example.com/static/img/logo.png -> base_dir/static/img/logo.png
    """
    parsed = urlparse.urlparse(asset_url)
    path = parsed.path
    if not path:
        path = "/unnamed"
    # Query raus, aber optional anhängen, um Kollisionen zu vermeiden
    filename = os.path.basename(path)
    if not filename:
        filename = "index"
    ext = os.path.splitext(filename)[1]
    if not ext:
        ext = ".bin"
        filename = filename + ext

    # Wenn Query vorhanden, anhängen, aber säubern
    if parsed.query:
        safe_q = re.sub(r"[^0-9A-Za-z._-]", "_", parsed.query)
        filename = os.path.splitext(filename)[0] + "_" + safe_q + ext

    # Verzeichnisstruktur aus Pfad übernehmen
    dir_part = os.path.dirname(path.lstrip("/"))
    local_path = base_dir / dir_part / filename
    return local_path


def rewrite_css_urls(css_text: str, base_url: str, assets_dir: Path, public_prefix: str):
    """
    Sucht url(...) in CSS, lädt Assets und ersetzt sie durch lokale Pfade.
    public_prefix: z.B. '/assets' – so werden die URLs im CSS geschrieben.
    """
    url_pattern = re.compile(r'url\((.*?)\)', re.IGNORECASE)

    def repl(match):
        raw = match.group(1).strip().strip('\'"')
        if not raw or raw.startswith("data:"):
            return match.group(0)

        full_url = urlparse.urljoin(base_url, raw)
        if not should_download_asset(full_url):
            return f"url({raw})"

        local_path = asset_local_name(full_url, assets_dir)
        if not local_path.exists():
            print(f"[CSS] Lade {full_url}")
            download_file(full_url, local_path)

        # Pfad, wie er vom neuen Server aus erreichbar ist
        rel_for_server = public_prefix + "/" + str(local_path.relative_to(assets_dir)).replace(os.sep, "/")
        return f"url('{rel_for_server}')"

    new_css = url_pattern.sub(repl, css_text)
    return new_css


def process_page(start_url: str, out_dir: str, public_asset_prefix: str = "/assets", html_name: str = "index.html"):
    base_output = Path(out_dir)
    ensure_dir(base_output)
    assets_dir = base_output / "assets"

    print(f"Lade HTML: {start_url}")
    resp = SESSION.get(start_url, timeout=20)
    resp.raise_for_status()

    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    # HTML-Tags mit Asset-Attributen
    tag_attr_pairs = [
        ("img", "src"),
        ("script", "src"),
        ("link", "href"),
        ("source", "src"),
        ("video", "src"),
        ("audio", "src"),
    ]

    # 1) Direkt in HTML referenzierte Assets
    for tag_name, attr in tag_attr_pairs:
        for tag in soup.find_all(tag_name):
            url_val = tag.get(attr)
            if not url_val:
                continue

            full_url = urlparse.urljoin(start_url, url_val)
            if not should_download_asset(full_url):
                continue

            local_path = asset_local_name(full_url, assets_dir)
            if not local_path.exists():
                print(f"[HTML] Lade {full_url}")
                try:
                    download_file(full_url, local_path)
                except Exception as e:
                    print(f"Fehler beim Download {full_url}: {e}")
                    continue

            # neuen Pfad setzen: vom HTML aus gesehen öffentlichen Pfad verwenden
            new_url = public_asset_prefix + "/" + str(local_path.relative_to(assets_dir)).replace(os.sep, "/")
            tag[attr] = new_url

    # 2) Inline-CSS in <style> und style="" Attributen
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            new_css = rewrite_css_urls(style_tag.string, start_url, assets_dir, public_asset_prefix)
            style_tag.string.replace_with(new_css)

    for tag in soup.find_all(style=True):
        css_inline = tag["style"]
        new_css = rewrite_css_urls(css_inline, start_url, assets_dir, public_asset_prefix)
        tag["style"] = new_css

    # 3) Externe CSS-Dateien nachladen und bearbeiten
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = link.get("href")
        if not href:
            continue
        css_url = urlparse.urljoin(start_url, href)
        if not should_download_asset(css_url):
            continue

        local_css_path = asset_local_name(css_url, assets_dir)
        if not local_css_path.exists():
            print(f"[CSS] Lade & verarbeite {css_url}")
            try:
                css_resp = SESSION.get(css_url, timeout=20)
                css_resp.raise_for_status()
                new_css = rewrite_css_urls(css_resp.text, css_url, assets_dir, public_asset_prefix)
                ensure_dir(local_css_path.parent)
                with open(local_css_path, "w", encoding="utf-8") as f:
                    f.write(new_css)
            except Exception as e:
                print(f"Fehler bei CSS {css_url}: {e}")
                continue

        new_href = public_asset_prefix + "/" + str(local_css_path.relative_to(assets_dir)).replace(os.sep, "/")
        link["href"] = new_href

    # 4) HTML speichern
    out_html_path = base_output / html_name
    with open(out_html_path, "w", encoding="utf-8") as f:
        f.write(str(soup))

    print(f"Fertig. HTML: {out_html_path}, Assets unter: {assets_dir}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Mirror eine HTML-Seite mit allen Assets und passe Pfade an.")
    parser.add_argument("url", help="Start-URL der HTML-Seite")
    parser.add_argument("-o", "--out-dir", default="site_mirror", help="Zielverzeichnis")
    parser.add_argument(
        "--asset-prefix",
        default="/assets",
        help="Öffentlicher Prefix, unter dem Assets auf dem neuen Server erreichbar sind (z.B. /static oder /assets)"
    )
    parser.add_argument(
        "--html-name",
        default="index.html",
        help="Dateiname der gespeicherten HTML-Seite"
    )

    args = parser.parse_args()
    process_page(args.url, args.out_dir, args.asset_prefix, args.html_name)

