"""Fetch one search page from Idealo / LeDénicheur / PCPartPicker with browser impersonation
and save the HTML, so the parsers can be written against real markup.

Run:  .venv/bin/pip install curl_cffi && .venv/bin/python tools/probe_blocked_sites.py
"""
from pathlib import Path

from curl_cffi import requests as cr

OUT = Path(__file__).resolve().parent.parent / "probe_out"
OUT.mkdir(exist_ok=True)

URLS = {
    "idealo": "https://www.idealo.fr/prechcat.html?q=rtx+5070",
    "ledenicheur": "https://ledenicheur.fr/search?search=rtx%205070",
    "pcpartpicker": "https://fr.pcpartpicker.com/products/video-card/",
}

for name, url in URLS.items():
    for imp in ("chrome", "safari"):
        try:
            r = cr.get(url, impersonate=imp, timeout=25, headers={"Accept-Language": "fr-FR,fr;q=0.9"})
        except Exception as e:
            print(f"{name:<13} {imp:<7} ERROR {e}")
            continue
        print(f"{name:<13} {imp:<7} HTTP {r.status_code}  {len(r.text):>8} bytes")
        if r.status_code == 200:
            (OUT / f"{name}.html").write_text(r.text, encoding="utf-8")
            break
print(f"\nSaved to {OUT}")
