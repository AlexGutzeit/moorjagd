#!/usr/bin/env python3
"""Baut aus moorjagd.html (Fassung fürs Artefakt) die eigenständige Seite für den eigenen Server:
vollständiges HTML-Gerüst, Schriften eingebettet, keine Anfragen an fremde Server."""
import base64
import os
import re

HIER = os.path.dirname(os.path.abspath(__file__))
SCHRIFTEN = os.path.join(HIER, "schriften")

with open(os.path.join(HIER, "moorjagd.html"), encoding="utf-8") as f:
    quelle = f.read()
with open(os.path.join(SCHRIFTEN, "google.css"), encoding="utf-8") as f:
    css = f.read()

# Nur der Latin-Block der Google-CSS (enthält ä ö ü ß × −); gleiche Datei + Familie = variable Schrift
faces = {}
for block in re.finditer(r"/\* latin \*/\s*@font-face\s*\{(.*?)\}", css, re.S):
    b = block.group(1)
    familie = re.search(r"font-family:\s*'([^']+)'", b).group(1)
    gewicht = int(re.search(r"font-weight:\s*(\d+)", b).group(1))
    datei = re.search(r"url\((https:[^)]+)\)", b).group(1).rsplit("/", 1)[1]
    faces.setdefault((familie, datei), []).append(gewicht)
if not faces:
    raise SystemExit("Keine Schriften gefunden")

regeln = []
for (familie, datei), gewichte in sorted(faces.items()):
    with open(os.path.join(SCHRIFTEN, datei), "rb") as f:
        roh = f.read()
    if roh[:4] != b"wOF2":
        raise SystemExit(f"{datei} ist keine woff2-Datei")
    g = str(gewichte[0]) if len(gewichte) == 1 else f"{min(gewichte)} {max(gewichte)}"
    regeln.append(
        f'@font-face {{ font-family: "{familie}"; font-weight: {g}; font-display: swap; '
        f'src: url(data:font/woff2;base64,{base64.b64encode(roh).decode()}) format("woff2"); }}'
    )

# Kopfzeilen des Artefakts entfernen, sie stehen unten im eigenen <head>
rumpf = re.sub(r'^<title>.*?</title>\n', "", quelle, flags=re.M)
rumpf = re.sub(r'^<meta name="claude:[^\n]*\n', "", rumpf, flags=re.M)
rumpf = re.sub(r'^<link [^\n]*\n', "", rumpf, flags=re.M)
if "googleapis" in rumpf or "gstatic" in rumpf:
    raise SystemExit("Fremde Schriftadresse in der Seite übrig")

teile = re.match(r"\s*<style>(.*?)</style>\s*(.*)", rumpf, re.S)
if not teile:
    raise SystemExit("Stilblock nicht gefunden")
stil, inhalt = teile.groups()

ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
        "%3Ccircle cx='16' cy='16' r='11' fill='none' stroke='%23ff6a13' stroke-width='3'/%3E"
        "%3Cpath d='M16 1v9M16 22v9M1 16h9M22 16h9' stroke='%23ff6a13' stroke-width='3'/%3E%3C/svg%3E")

seite = f"""<!doctype html>
<html lang="de" data-api="/api/moorjagd">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Moorjagd</title>
<meta name="description" content="Hühnerjagd im herbstlichen Moor: 90 Sekunden, 8 Patronen, spielbar mit Maus oder Touch.">
<meta name="theme-color" content="#1b1f13">
<link rel="icon" href="{ICON}">
<style>
:root {{ padding-top: env(safe-area-inset-top, 0px); padding-bottom: env(safe-area-inset-bottom, 0px); }}
body {{ margin: 0; }}
[hidden] {{ display: none !important; }}
/* Schriften Grenze und Grenze Gotisch: Omnibus-Type, SIL Open Font License 1.1 */
{chr(10).join(regeln)}
{stil}</style>
</head>
<body>
{inhalt}</body>
</html>
"""

ziel = os.path.join(HIER, "dist", "moorjagd.html")
os.makedirs(os.path.dirname(ziel), exist_ok=True)
with open(ziel, "w", encoding="utf-8") as f:
    f.write(seite)
print(f"{ziel}: {len(seite.encode()) // 1024} KB, {len(regeln)} Schriftdateien, Titel bei Byte {seite.index('<title>')}")
