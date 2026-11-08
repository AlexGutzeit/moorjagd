#!/usr/bin/env python3
"""Bestenliste und Mehrspieler für Moorjagd, gedacht für den Betrieb hinter einem Reverse-Proxy (z. B. Caddy).

Bestenliste: kleine JSON-Schnittstelle mit SQLite. Gespeichert werden Name, Punkte, Treffer, Schüsse
und Zeitpunkt, aber keine IP-Adressen. Die Mengenbegrenzung hält Adressen nur als Prüfsumme im Arbeitsspeicher.

Mehrspieler: WebSocket unter /api/moorjagd/mehr. Räume, Anfragen und Runden leben nur im Arbeitsspeicher.
Der Server bestimmt, welche Hühner wann auftauchen, und entscheidet, wer ein Ziel zuerst getroffen hat.

Verwaltung auf dem Server:
    moorjagd-strecken.py --liste
    moorjagd-strecken.py --loeschen ID
"""
import argparse
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import os
import random
import re
import secrets
import socket
import sqlite3
import struct
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ORDNER = os.environ.get("STATE_DIRECTORY", "/var/lib/moorjagd")
DB_DATEI = os.path.join(ORDNER, "strecken.db")
GEHEIM_DATEI = os.path.join(ORDNER, "geheim")
ADRESSE = os.environ.get("MOORJAGD_ADRESSE", "127.0.0.1")
PORT = int(os.environ.get("MOORJAGD_PORT", "8097"))
# Nur der Reverse-Proxy darf fragen, sonst ließe sich X-Forwarded-For fälschen
VERTRAUT = os.environ.get("MOORJAGD_VERTRAUT", "127.0.0.1")
# Origin der Seite, kommagetrennt
HERKUNFT = set(os.environ.get("MOORJAGD_HERKUNFT", "http://localhost:8000,http://127.0.0.1:8000").split(","))

MINDESTDAUER = int(os.environ.get("MOORJAGD_MINDESTDAUER", "85"))  # Eine Runde dauert 90 s
HOECHSTALTER = 6 * 3600
MAX_KOERPER = 1024
MAX_PUNKTE = 25000
MAX_SCHUESSE = 1000
PUNKTE_JE_TREFFER = 50
LISTENLAENGE = 10
BEHALTEN = 2000
GRENZEN = {"runde": (120, 3600), "eintrag": (20, 3600), "verbindung": (120, 600), "raum": (30, 3600)}
TAGESGRENZE = 500

zaehler_sperre = threading.Lock()
zaehler = {}
tag_zaehler = {"tag": "", "n": 0}
salz = secrets.token_bytes(16)


def verbinden():
    db = sqlite3.connect(DB_DATEI, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def einrichten():
    with verbinden() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS strecken (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            punkte INTEGER NOT NULL,
            treffer INTEGER NOT NULL,
            schuesse INTEGER NOT NULL,
            am TEXT NOT NULL,
            runde TEXT NOT NULL UNIQUE)""")
        db.execute("CREATE INDEX IF NOT EXISTS strecken_punkte ON strecken (punkte DESC, id)")
    db.close()


def geheimnis_laden():
    try:
        with open(GEHEIM_DATEI, "rb") as f:
            return f.read()
    except FileNotFoundError:
        neu = secrets.token_bytes(32)
        fd = os.open(GEHEIM_DATEI, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(neu)
        return neu


def marke_signatur(geheim, inhalt):
    return hmac.new(geheim, inhalt.encode(), hashlib.sha256).hexdigest()[:32]


def marke_ausstellen(geheim):
    inhalt = f"{int(time.time())}.{secrets.token_hex(12)}"
    return f"{inhalt}.{marke_signatur(geheim, inhalt)}"


def marke_pruefen(geheim, marke):
    """Liefert die Runden-Kennung oder den Grund der Ablehnung."""
    if not isinstance(marke, str) or not re.fullmatch(r"\d{1,12}\.[0-9a-f]{24}\.[0-9a-f]{32}", marke):
        return None, "marke"
    zeit, kennung, signatur = marke.split(".")
    if not hmac.compare_digest(signatur, marke_signatur(geheim, f"{zeit}.{kennung}")):
        return None, "marke"
    alter = time.time() - int(zeit)
    if alter < MINDESTDAUER:
        return None, "zu_frueh"
    if alter > HOECHSTALTER:
        return None, "zu_alt"
    return kennung, None


def name_bereinigen(roh, laenge=16):
    if not isinstance(roh, str):
        return "Unbekannt"
    text = unicodedata.normalize("NFC", roh)
    # Steuer- und Formatzeichen raus (auch solche, die die Schreibrichtung umkehren)
    text = "".join(z for z in text if not unicodedata.category(z).startswith("C"))
    text = " ".join(text.split())[:laenge].strip()
    return text or "Unbekannt"


def ist_ganzzahl(w, a, b):
    return isinstance(w, int) and not isinstance(w, bool) and a <= w <= b


def werte_pruefen(d):
    if not (ist_ganzzahl(d.get("punkte"), 1, MAX_PUNKTE)
            and ist_ganzzahl(d.get("schuesse"), 1, MAX_SCHUESSE)
            and ist_ganzzahl(d.get("treffer"), 1, MAX_SCHUESSE)):
        return False
    return d["treffer"] <= d["schuesse"] and d["punkte"] <= d["treffer"] * PUNKTE_JE_TREFFER


def absender_schluessel(handler):
    roh = (handler.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
    try:
        ip = ipaddress.ip_address(roh)
    except ValueError:
        return "unbekannt"
    # Bei IPv6 zählt das ganze /64, weil ein Anschluss viele Adressen daraus nutzen kann
    netz = ipaddress.ip_network(f"{ip}/64", strict=False) if ip.version == 6 else ip
    return hashlib.sha256(salz + str(netz).encode()).hexdigest()[:16]


def begrenzt(art, schluessel):
    menge, fenster = GRENZEN[art]
    jetzt = time.time()
    with zaehler_sperre:
        if len(zaehler) > 20000:
            zaehler.clear()
        k = (art, schluessel)
        liste = [t for t in zaehler.get(k, []) if jetzt - t < fenster]
        if len(liste) >= menge:
            zaehler[k] = liste
            return True
        liste.append(jetzt)
        zaehler[k] = liste
    return False


def tagesgrenze_erreicht():
    heute = time.strftime("%Y-%m-%d")
    with zaehler_sperre:
        if tag_zaehler["tag"] != heute:
            tag_zaehler.update(tag=heute, n=0)
        if tag_zaehler["n"] >= TAGESGRENZE:
            return True
        tag_zaehler["n"] += 1
    return False


def liste_lesen(db):
    zeilen = db.execute("SELECT id, name, punkte FROM strecken ORDER BY punkte DESC, id LIMIT ?", (LISTENLAENGE,))
    return [{"id": str(z["id"]), "name": z["name"], "punkte": z["punkte"]} for z in zeilen]


# ---------- Mehrspieler ----------
# Weltkoordinate X: die Stelle der nahen Ebene, die in Bildmitte steht, wenn man genau auf das Ziel schaut.
# Jedes Gerät rechnet daraus seine eigene Ebenenposition (x = S/2 + p·(X − S/2), S = sichtbare Breite).
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_NACHRICHT = 2048
MAX_VERBINDUNGEN = 60
MAX_RAEUME = 20
MAX_JAEGER = 8
MAX_ANFRAGEN = 10
RUNDE = float(os.environ.get("MOORJAGD_RUNDE", "90"))
COUNTDOWN = 4.0  # zeigt 3, 2, 1, 0
WIEDERKEHR = 30  # so lange darf eine abgerissene Verbindung zurückkommen
WELT = 2400
SICHT_NORMAL = 1067
# p, Größe, Punkte, Tempo, Flugband – wie im Spiel
LAGEN = [(.3, 46, 25, (55, 90), (150, 305)), (.55, 78, 10, (90, 150), (90, 350)), (1, 130, 5, (150, 230), (70, 400))]
LAGE_NAMEN = ["fern", "mittel", "nah"]
MAGAZIN = 8
LADEKOSTEN = 10  # eine Schachtel Patronen, Reste im Magazin sind verloren
ORTE = [("eiche", 20), ("heu", 15), ("brunnen", 15), ("muehle", 25)]

welt = threading.RLock()
start_uhr = time.monotonic()
spieler = {}
raeume = {}
verbindungen = [0]


def uhr():
    return time.monotonic() - start_uhr


def zufall(a, b):
    return random.uniform(a, b)


def klemme(w, a, b):
    return max(a, min(b, w))


def rahmen(op, daten):
    n = len(daten)
    if n < 126:
        kopf = struct.pack("!BB", 0x80 | op, n)
    elif n < 65536:
        kopf = struct.pack("!BBH", 0x80 | op, 126, n)
    else:
        kopf = struct.pack("!BBQ", 0x80 | op, 127, n)
    return kopf + daten


def rahmen_lesen(rfile):
    """Liest einen WebSocket-Rahmen vom Browser. Fragmentierte oder zu große Nachrichten beenden die Verbindung."""
    kopf = rfile.read(2)
    if len(kopf) < 2:
        raise EOFError
    fin, op, maskiert, n = kopf[0] & 0x80, kopf[0] & 0x0F, kopf[1] & 0x80, kopf[1] & 0x7F
    if not fin or not maskiert:
        raise ValueError("Rahmen")
    if n == 126:
        n = struct.unpack("!H", rfile.read(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", rfile.read(8))[0]
    if n > WS_MAX_NACHRICHT:
        raise ValueError("zu groß")
    maske = rfile.read(4)
    daten = rfile.read(n)
    if len(maske) < 4 or len(daten) < n:
        raise EOFError
    kette = (maske * (n // 4 + 1))[:n]
    return op, (int.from_bytes(daten, "big") ^ int.from_bytes(kette, "big")).to_bytes(n, "big")


class Verbindung:
    """Eigener Schreib-Faden je Verbindung, damit ein langsames Gerät niemanden aufhält."""

    def __init__(self, sock):
        self.sock = sock
        self.schlange = collections.deque()
        self.bedingung = threading.Condition()
        self.offen = True
        threading.Thread(target=self.schreiben, daemon=True).start()

    def senden(self, roh, op=1):
        with self.bedingung:
            if not self.offen:
                return
            if len(self.schlange) > 300:
                self.offen = False
            else:
                self.schlange.append(rahmen(op, roh))
            self.bedingung.notify()

    def schliessen(self):
        with self.bedingung:
            self.offen = False
            self.bedingung.notify()

    def schreiben(self):
        while True:
            with self.bedingung:
                while self.offen and not self.schlange:
                    self.bedingung.wait()
                teile = list(self.schlange)
                self.schlange.clear()
                ende = not self.offen
            try:
                if teile:
                    self.sock.sendall(b"".join(teile))
            except OSError:
                ende = True
            if ende:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return


class Spieler:
    def __init__(self, name):
        self.id = secrets.token_hex(4)
        self.schluessel = secrets.token_hex(16)
        self.name = name
        self.verbindung = None
        self.weg_seit = None
        self.raum = None
        self.anfrage = None
        self.farbe = None
        self.ziel = None
        self.ziel_zeit = 0
        self.mitte = WELT / 2
        self.sicht = SICHT_NORMAL
        self.schuss_zeiten = collections.deque()
        self.scheuche_sperre = 0
        self.wertung_leeren()

    def wertung_leeren(self):
        self.punkte = self.treffer = self.schuesse = 0
        self.munition = MAGAZIN
        self.strecke = {"fern": 0, "mittel": 0, "nah": 0, "versteckt": 0, "ballon": 0, "schild": 0, "scheuche": 0, "laden": 0}

    def kurz(self):
        return {"id": self.id, "name": self.name, "farbe": self.farbe, "weg": self.verbindung is None}


class Raum:
    def __init__(self, name, oberjaeger):
        self.id = secrets.token_hex(4)
        self.name = name
        self.oberjaeger = oberjaeger
        self.jaeger = []
        self.anfragen = []
        self.phase = "warten"
        self.start = 0
        self.ergebnis = None
        self.rundenzaehler = 0
        self.leeren()

    def leeren(self):
        self.huehner = {}
        self.naechste_id = 1
        self.naechstes = 0
        self.orte = []
        self.schilder = 3
        self.ballon = None
        self.ballon_plan = []
        self.takt = 0


def an(sp, daten):
    if sp.verbindung:
        daten["u"] = round(uhr(), 3)
        sp.verbindung.senden(json.dumps(daten, ensure_ascii=False, separators=(",", ":")).encode())


def an_raum(raum, daten):
    roh = None
    for sp in raum.jaeger:
        if sp.verbindung:
            if roh is None:
                daten["u"] = round(uhr(), 3)
                roh = json.dumps(daten, ensure_ascii=False, separators=(",", ":")).encode()
            sp.verbindung.senden(roh)


def raum_liste():
    jetzt = uhr()
    return [{"id": r.id, "name": r.name, "oj": r.oberjaeger.name, "n": len(r.jaeger), "max": MAX_JAEGER,
             "phase": r.phase, "rest": max(0, round(r.start + RUNDE - jetzt)) if r.phase == "jagd" else None}
            for r in raeume.values()]


def lobby_melden():
    liste = raum_liste()
    for sp in spieler.values():
        if sp.raum is None and sp.verbindung:
            an(sp, {"typ": "raeume", "liste": liste})


def raum_melden(raum):
    for sp in raum.jaeger:
        an(sp, {"typ": "raum", "id": raum.id, "name": raum.name, "oj": raum.oberjaeger.id, "ich": sp.id,
                "phase": raum.phase, "start": round(raum.start, 3), "dauer": RUNDE,
                "jaeger": [j.kurz() for j in raum.jaeger],
                "anfragen": [{"id": a.id, "name": a.name} for a in raum.anfragen] if sp is raum.oberjaeger else []})


def freie_farbe(raum):
    belegt = {j.farbe for j in raum.jaeger}
    return next(i for i in range(MAX_JAEGER) if i not in belegt)


def raum_betreten(raum, sp):
    sp.anfrage = None
    sp.raum = raum
    sp.farbe = freie_farbe(raum)
    sp.wertung_leeren()
    raum.jaeger.append(sp)


def anfrage_beenden(sp, mitteilen=None):
    raum = sp.anfrage
    if raum is None:
        return
    sp.anfrage = None
    if sp in raum.anfragen:
        raum.anfragen.remove(sp)
        raum_melden(raum)
    if mitteilen:
        an(sp, {"typ": "abgelehnt", "raum": raum.name, "grund": mitteilen})


def raum_verlassen(sp, grund="verlassen"):
    raum = sp.raum
    if raum is None:
        return
    raum.jaeger.remove(sp)
    sp.raum = None
    sp.farbe = None
    if grund != "verlassen":
        an(sp, {"typ": "draussen", "grund": grund})
    if not raum.jaeger:
        del raeume[raum.id]
        for a in list(raum.anfragen):
            anfrage_beenden(a, "aufgeloest")
    else:
        if raum.oberjaeger is sp:
            # Wer am längsten dabei und erreichbar ist, übernimmt
            raum.oberjaeger = next((j for j in raum.jaeger if j.verbindung), raum.jaeger[0])
            an_raum(raum, {"typ": "meldung", "text": f"{raum.oberjaeger.name} ist jetzt Oberjäger."})
        raum_melden(raum)
    lobby_melden()


def entfernen(sp):
    anfrage_beenden(sp)
    raum_verlassen(sp, "weg")
    spieler.pop(sp.id, None)


def ebene_breite(p, sicht):
    return sicht + max(0, WELT - sicht) * p


def lokal_zu_welt(x, p, sicht):
    return sicht / 2 + (x - sicht / 2) / p


def welt_grenzen(p, rand, sicht):
    return lokal_zu_welt(-rand, p, sicht), lokal_zu_welt(ebene_breite(p, sicht) + rand, p, sicht)


def beobachter(raum):
    """Ein zufälliger Jäger, an dessen Blickfeld neue Ziele auftauchen."""
    da = [j for j in raum.jaeger if j.verbindung] or raum.jaeger
    return random.choice(da)


def groesste_sicht(raum):
    return max([j.sicht for j in raum.jaeger] + [SICHT_NORMAL])


def huhn_spawnen(raum, jetzt, alter=0.0):
    w = random.random()
    i = 0 if w < .4 else 1 if w < .75 else 2
    p, groesse, _, tempo, band = LAGEN[i]
    b = beobachter(raum)
    s, mitte = b.sicht, b.mitte
    richtung = 1 if random.random() < .5 else -1
    if random.random() < .72:
        x = mitte - richtung * (s / (2 * p) + groesse / p)
    else:
        lokal = -groesse if richtung > 0 else ebene_breite(p, s) + groesse
        x = lokal_zu_welt(lokal, p, s)
    v = richtung * zufall(*tempo) * klemme(s / SICHT_NORMAL, .7, 1) / p
    y = zufall(max(band[0], 70), band[1])
    links, rechts = welt_grenzen(p, groesse + 40, groesste_sicht(raum))
    u0 = jetzt - alter
    ende = u0 + max(0.5, ((rechts if v > 0 else links) - x) / v)
    h = {"id": raum.naechste_id, "l": i, "x": round(x, 1), "y": round(y, 1), "v": round(v, 2), "u0": round(u0, 3)}
    raum.naechste_id += 1
    raum.huehner[h["id"]] = dict(h, ende=ende, getroffen=False)
    an_raum(raum, dict(h, typ="huhn"))


def ballon_starten(raum, jetzt):
    p = LAGEN[0][0]
    b = beobachter(raum)
    s = b.sicht
    breite = ebene_breite(p, s)
    links = (b.mitte - s / 2) * p
    rechts_mehr = links < (breite - s) / 2
    lokal = min(breite - 30, links + s + 240) if rechts_mehr else max(30, links - 240)
    raum.ballon = {"id": raum.naechste_id, "x": round(lokal_zu_welt(lokal, p, s), 1), "y": round(zufall(120, 210), 1),
                   "v": round((-1 if rechts_mehr else 1) * zufall(16, 26) / p, 2), "u0": round(jetzt, 3), "weg": False}
    raum.naechste_id += 1
    an_raum(raum, dict({k: w for k, w in raum.ballon.items() if k != "weg"}, typ="ballon"))


def runde_vorbereiten(raum):
    raum.leeren()
    raum.phase = "countdown"
    raum.start = uhr() + COUNTDOWN
    raum.rundenzaehler += 1
    raum.ergebnis = None
    raum.orte = [{"bis": raum.start + zufall(1, 6), "oben": None, "unten": 0} for _ in ORTE]
    raum.ballon_plan = sorted([raum.start + RUNDE - zufall(62, 72) * RUNDE / 90, raum.start + RUNDE - zufall(22, 34) * RUNDE / 90])
    for j in raum.jaeger:
        j.wertung_leeren()
    raum_melden(raum)
    lobby_melden()


def jagd_zustand(raum):
    """Alles, was ein Gerät braucht, um mitten in eine laufende Runde einzusteigen."""
    jetzt = uhr()
    return {"typ": "jagd", "start": round(raum.start, 3),
            "huehner": [{k: h[k] for k in ("id", "l", "x", "y", "v", "u0")} for h in raum.huehner.values()
                        if not h["getroffen"] and h["ende"] > jetzt],
            "orte": [{"i": i, "ab": round(o["oben"], 3), "halten": o["halten"]} for i, o in enumerate(raum.orte)
                     if o["oben"] is not None and o["unten"] > jetzt],
            "ballon": {k: w for k, w in raum.ballon.items() if k != "weg"} if raum.ballon and not raum.ballon["weg"] else None,
            "schilder": raum.schilder, "punkte": {j.id: j.punkte for j in raum.jaeger}}


def runde_beenden(raum):
    reihe = sorted(raum.jaeger, key=lambda j: (-j.punkte, -j.treffer, j.schuesse))
    raum.ergebnis = [{"id": j.id, "name": j.name, "farbe": j.farbe, "punkte": j.punkte, "treffer": j.treffer,
                      "schuesse": j.schuesse, "strecke": dict(j.strecke)} for j in reihe]
    raum.phase = "warten"
    raum.leeren()
    an_raum(raum, {"typ": "ende", "ergebnis": raum.ergebnis})
    raum_melden(raum)
    lobby_melden()


def raum_takt(raum, jetzt):
    if raum.phase == "countdown" and jetzt >= raum.start:
        raum.phase = "jagd"
        raum_melden(raum)
        lobby_melden()
        for _ in range(5):
            huhn_spawnen(raum, jetzt, zufall(1, 5))
    if raum.phase != "jagd":
        return
    if jetzt >= raum.start + RUNDE:
        return runde_beenden(raum)
    for k in [k for k, h in raum.huehner.items() if h["getroffen"] or h["ende"] + 1 < jetzt]:
        del raum.huehner[k]
    n = max(1, sum(1 for j in raum.jaeger if j.verbindung))
    raum.naechstes -= jetzt - raum.takt if raum.takt else 0
    raum.takt = jetzt
    if raum.naechstes <= 0:
        rest = raum.start + RUNDE - jetzt
        raum.naechstes = (zufall(.42, .62) - .12 * (1 - rest / RUNDE)) / (1 + .3 * (n - 1))
        if sum(1 for h in raum.huehner.values() if not h["getroffen"]) < 20 + 5 * (n - 1):
            huhn_spawnen(raum, jetzt)
    for i, o in enumerate(raum.orte):
        if o["oben"] is None and jetzt >= o["bis"]:
            o["oben"], o["halten"] = jetzt, round(zufall(1.5, 2.6), 2)
            o["unten"] = jetzt + .24 + o["halten"] + .22
            an_raum(raum, {"typ": "ort", "i": i, "halten": o["halten"]})
        elif o["oben"] is not None and jetzt >= o["unten"]:
            o["oben"], o["bis"] = None, jetzt + zufall(2.5, 7)
    if raum.ballon_plan and jetzt >= raum.ballon_plan[0]:
        raum.ballon_plan.pop(0)
        ballon_starten(raum, jetzt)


def stand_melden(raum, jetzt):
    z = [[j.id, j.ziel[0], j.ziel[1]] for j in raum.jaeger if j.ziel and jetzt - j.ziel_zeit < 4]
    if z:
        an_raum(raum, {"typ": "stand", "z": z})


def taktgeber():
    zaehler = 0
    while True:
        time.sleep(.05)
        zaehler += 1
        with welt:
            jetzt = uhr()
            for raum in list(raeume.values()):
                raum_takt(raum, jetzt)
                if zaehler % 2 == 0 and raum.phase in ("jagd", "countdown"):
                    stand_melden(raum, jetzt)
            if zaehler % 20 == 0:
                for sp in [s for s in spieler.values() if s.weg_seit and jetzt - s.weg_seit > WIEDERKEHR]:
                    entfernen(sp)
                if zaehler % 100 == 0 and any(r.phase == "jagd" for r in raeume.values()):
                    lobby_melden()  # Restzeit der laufenden Runden


def ganz(w, a, b):
    return isinstance(w, int) and not isinstance(w, bool) and a <= w <= b


def zahl(w, a, b):
    return isinstance(w, (int, float)) and not isinstance(w, bool) and a <= w <= b


def schuss_auswerten(raum, sp, ziel, jetzt):
    """Liefert (Art, Punkte, Zusatz) bei einem gültigen Treffer, sonst None. Wer zuerst kommt, bekommt das Ziel."""
    if not isinstance(ziel, dict):
        return None
    art = ziel.get("art")
    if art == "huhn":
        h = raum.huehner.get(ziel.get("id")) if ganz(ziel.get("id"), 1, 10 ** 9) else None
        if not h or h["getroffen"] or h["ende"] + .5 < jetzt:
            return None
        h["getroffen"] = True
        name = LAGE_NAMEN[h["l"]]
        sp.strecke[name] += 1
        return "huhn", LAGEN[h["l"]][2], {"id": h["id"]}
    if art == "ort":
        i = ziel.get("i")
        if not ganz(i, 0, len(ORTE) - 1):
            return None
        o = raum.orte[i]
        if o["oben"] is None or not (o["oben"] + .05 <= jetzt <= o["unten"] + .3):
            return None
        o["oben"], o["bis"] = None, jetzt + zufall(5, 10)
        sp.strecke["versteckt"] += 1
        return "ort", ORTE[i][1], {"i": i}
    if art == "ballon":
        b = raum.ballon
        if not b or b["weg"] or jetzt - b["u0"] > 150:
            return None
        b["weg"] = True
        sp.strecke["ballon"] += 1
        return "ballon", 50, {"id": b["id"]}
    if art == "schild":
        if raum.schilder <= 0:
            return None
        raum.schilder -= 1
        sp.strecke["schild"] += 1
        return "schild", 10, {"schilder": raum.schilder}
    if art == "scheuche":
        if jetzt < sp.scheuche_sperre:
            return None
        sp.scheuche_sperre = jetzt + 1
        sp.strecke["scheuche"] += 1
        return "scheuche", -25, {}
    return None


def name_kurz(roh, laenge=16):
    return name_bereinigen(roh, laenge)


def verarbeiten(sp, d, adresse):
    typ = d.get("typ")
    raum = sp.raum
    jetzt = uhr()
    if typ == "ping":
        return an(sp, {"typ": "pong"})
    if typ == "ziel":
        if raum and zahl(d.get("x"), -5000, 8000) and zahl(d.get("y"), -2000, 700):
            sp.ziel, sp.ziel_zeit = [round(d["x"], 1), round(d["y"], 1)], jetzt
            if zahl(d.get("c"), -5000, 8000):
                sp.mitte = float(d["c"])
            if zahl(d.get("s"), 300, 6000):
                sp.sicht = float(d["s"])
        return
    if typ == "schuss":
        if not raum or raum.phase != "jagd" or not raum.start <= jetzt <= raum.start + RUNDE + .5:
            return
        zeiten = sp.schuss_zeiten
        while zeiten and jetzt - zeiten[0] > 1:
            zeiten.popleft()
        if len(zeiten) >= 12:
            return
        zeiten.append(jetzt)
        if sp.munition <= 0:
            return
        sp.munition -= 1
        sp.schuesse += 1
        if zahl(d.get("x"), -5000, 8000) and zahl(d.get("y"), -2000, 700):
            sp.ziel, sp.ziel_zeit = [round(d["x"], 1), round(d["y"], 1)], jetzt
        ergebnis = schuss_auswerten(raum, sp, d.get("ziel"), jetzt)
        antwort = {"typ": "schuss", "wer": sp.id}
        if ergebnis:
            art, punkte, zusatz = ergebnis
            sp.punkte += punkte
            if art != "scheuche":
                sp.treffer += 1
            antwort.update(zusatz, art=art, punkte=punkte, summe=sp.punkte)
        elif isinstance(d.get("ziel"), dict) and d["ziel"].get("art") in ("huhn", "ort", "ballon", "schild"):
            antwort["zuspaet"] = d["ziel"]["art"]
        return an_raum(raum, antwort)
    if typ == "laden":
        if raum and raum.phase == "jagd" and sp.munition < MAGAZIN:
            sp.munition = MAGAZIN
            sp.punkte -= LADEKOSTEN
            sp.strecke["laden"] += 1
            an_raum(raum, {"typ": "laden", "wer": sp.id, "summe": sp.punkte})
        return
    if typ == "name":
        sp.name = name_kurz(d.get("name"))
        if raum:
            raum_melden(raum)
        return
    if typ == "raum_neu":
        if "name" in d:
            sp.name = name_kurz(d.get("name"))
        anfrage_beenden(sp)
        raum_verlassen(sp)
        if len(raeume) >= MAX_RAEUME:
            return an(sp, {"typ": "fehler", "code": "zu_viele_raeume"})
        if begrenzt("raum", adresse):
            return an(sp, {"typ": "fehler", "code": "zu_viele"})
        neu = Raum(name_kurz(d.get("raum") or f"Jagd von {sp.name}", 28), sp)
        raeume[neu.id] = neu
        raum_betreten(neu, sp)
        raum_melden(neu)
        return lobby_melden()
    if typ == "anfragen":
        if "name" in d:
            sp.name = name_kurz(d.get("name"))
        ziel = raeume.get(d.get("raum")) if isinstance(d.get("raum"), str) else None
        if not ziel:
            return an(sp, {"typ": "fehler", "code": "raum_weg"})
        if raum is ziel:
            return
        if len(ziel.jaeger) >= MAX_JAEGER:
            return an(sp, {"typ": "fehler", "code": "raum_voll"})
        if len(ziel.anfragen) >= MAX_ANFRAGEN:
            return an(sp, {"typ": "fehler", "code": "zu_viele"})
        anfrage_beenden(sp)
        raum_verlassen(sp)
        sp.anfrage = ziel
        ziel.anfragen.append(sp)
        an(sp, {"typ": "anfrage", "raum": ziel.id, "name": ziel.name, "oj": ziel.oberjaeger.name})
        return raum_melden(ziel)
    if typ == "zurueckziehen":
        return anfrage_beenden(sp)
    if typ == "verlassen":
        anfrage_beenden(sp)
        raum_verlassen(sp)
        return an(sp, {"typ": "raeume", "liste": raum_liste()})
    # Ab hier nur der Oberjäger
    if not raum or raum.oberjaeger is not sp:
        return
    wen = next((s for s in raum.anfragen + raum.jaeger if s.id == d.get("spieler")), None)
    if typ == "annehmen" and wen in raum.anfragen:
        if len(raum.jaeger) >= MAX_JAEGER:
            return an(sp, {"typ": "fehler", "code": "raum_voll"})
        raum.anfragen.remove(wen)
        raum_betreten(raum, wen)
        raum_melden(raum)
        if raum.phase == "jagd":
            wen.munition = MAGAZIN
            an(wen, jagd_zustand(raum))
        return lobby_melden()
    if typ == "ablehnen" and wen in raum.anfragen:
        return anfrage_beenden(wen, "abgelehnt")
    if typ == "rauswerfen" and wen in raum.jaeger and wen is not sp:
        return raum_verlassen(wen, "rausgeworfen")
    if typ == "start" and raum.phase == "warten":
        return runde_vorbereiten(raum)


def ws_sitzung(handler, v, adresse):
    """Läuft im Faden der HTTP-Anfrage, bis die Verbindung endet."""
    sp = None
    budget, zuletzt = 60.0, time.monotonic()
    try:
        while True:
            op, daten = rahmen_lesen(handler.rfile)
            if op == 8:
                v.senden(daten[:2], 8)
                break
            if op == 9:
                v.senden(daten, 10)
                continue
            if op == 10:
                continue
            if op != 1:
                break
            # Höchstens etwa 40 Nachrichten je Sekunde, kurze Spitzen erlaubt
            jetzt = time.monotonic()
            budget = min(60.0, budget + (jetzt - zuletzt) * 40)
            zuletzt = jetzt
            budget -= 1
            if budget < 0:
                break
            try:
                d = json.loads(daten)
            except ValueError:
                break
            if not isinstance(d, dict):
                break
            with welt:
                if sp is None:
                    if d.get("typ") != "hallo":
                        break
                    alt = d.get("schluessel")
                    sp = next((s for s in spieler.values() if isinstance(alt, str) and hmac.compare_digest(s.schluessel, alt)), None)
                    if sp and sp.verbindung:
                        sp.verbindung.schliessen()  # dieselbe Seite neu geladen
                    if sp is None:
                        sp = Spieler(name_kurz(d.get("name")))
                        spieler[sp.id] = sp
                    sp.verbindung, sp.weg_seit = v, None
                    an(sp, {"typ": "willkommen", "id": sp.id, "schluessel": sp.schluessel, "name": sp.name,
                            "farben": MAX_JAEGER})
                    if sp.raum:
                        raum_melden(sp.raum)
                        if sp.raum.phase == "jagd":
                            sp.munition = MAGAZIN  # die Seite beginnt nach dem Neuladen mit vollem Magazin
                            an(sp, jagd_zustand(sp.raum))
                    else:
                        an(sp, {"typ": "raeume", "liste": raum_liste()})
                        if sp.anfrage:
                            an(sp, {"typ": "anfrage", "raum": sp.anfrage.id, "name": sp.anfrage.name, "oj": sp.anfrage.oberjaeger.name})
                    continue
                verarbeiten(sp, d, adresse)
    except (EOFError, ValueError, OSError, struct.error):
        pass
    finally:
        v.schliessen()
        with welt:
            verbindungen[0] -= 1
            if sp and sp.verbindung is v:
                sp.verbindung, sp.weg_seit = None, uhr()
                if sp.raum:
                    raum_melden(sp.raum)


class Bearbeiter(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 10
    server_version = "moorjagd"
    sys_version = ""
    geheim = b""

    def log_message(self, *_):
        pass  # Kein Zugriffsprotokoll, damit keine Adressen im Journal landen

    def antwort(self, status, daten):
        koerper = json.dumps(daten, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(koerper)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(koerper)

    def fehler(self, status, code):
        self.antwort(status, {"fehler": code})

    def zugelassen(self):
        if self.client_address[0] == VERTRAUT:
            return True
        self.close_connection = True
        self.fehler(403, "verboten")
        return False

    def do_GET(self):
        if not self.zugelassen():
            return
        if self.path == "/api/moorjagd/mehr":
            return self.mehrspieler()
        if self.path != "/api/moorjagd/strecken":
            return self.fehler(404, "unbekannt")
        db = verbinden()
        try:
            gesamt = db.execute("SELECT COUNT(*) FROM strecken").fetchone()[0]
            self.antwort(200, {"eintraege": liste_lesen(db), "gesamt": gesamt})
        finally:
            db.close()

    def mehrspieler(self):
        self.close_connection = True
        if self.headers.get("Origin") not in HERKUNFT:
            return self.fehler(403, "herkunft")
        schluessel = self.headers.get("Sec-WebSocket-Key") or ""
        if ((self.headers.get("Upgrade") or "").lower() != "websocket"
                or self.headers.get("Sec-WebSocket-Version") != "13"
                or not re.fullmatch(r"[A-Za-z0-9+/]{22}==", schluessel)):
            return self.fehler(400, "format")
        adresse = absender_schluessel(self)
        if begrenzt("verbindung", adresse):
            return self.fehler(429, "zu_viele")
        with welt:
            if verbindungen[0] >= MAX_VERBINDUNGEN:
                return self.fehler(503, "voll")
            verbindungen[0] += 1
        try:
            antwort = base64.b64encode(hashlib.sha1((schluessel + WS_GUID).encode()).digest()).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", antwort)
            self.end_headers()
            self.connection.settimeout(75)  # Die Seite meldet sich alle 20 s
        except OSError:
            with welt:
                verbindungen[0] -= 1
            return
        ws_sitzung(self, Verbindung(self.connection), adresse)

    def koerper_lesen(self):
        try:
            laenge = int(self.headers.get("Content-Length", ""))
        except ValueError:
            laenge = -1
        if laenge < 0 or laenge > MAX_KOERPER:
            self.close_connection = True
            self.fehler(413, "zu_gross")
            return None
        roh = self.rfile.read(laenge)
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self.fehler(415, "format")
            return None
        try:
            d = json.loads(roh)
        except ValueError:
            self.fehler(400, "format")
            return None
        if not isinstance(d, dict):
            self.fehler(400, "format")
            return None
        return d

    def do_POST(self):
        if not self.zugelassen():
            return
        d = self.koerper_lesen()
        if d is None:
            return
        if self.headers.get("Origin") not in HERKUNFT:
            return self.fehler(403, "herkunft")
        schluessel = absender_schluessel(self)
        if self.path == "/api/moorjagd/runde":
            if begrenzt("runde", schluessel):
                return self.fehler(429, "zu_viele")
            return self.antwort(201, {"runde": marke_ausstellen(self.geheim)})
        if self.path == "/api/moorjagd/strecken":
            return self.eintragen(d, schluessel)
        self.fehler(404, "unbekannt")

    def eintragen(self, d, schluessel):
        if not werte_pruefen(d):
            return self.fehler(422, "werte")
        kennung, grund = marke_pruefen(self.geheim, d.get("runde"))
        if grund:
            return self.fehler(403, grund)
        if begrenzt("eintrag", schluessel) or tagesgrenze_erreicht():
            return self.fehler(429, "zu_viele")
        am = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db = verbinden()
        try:
            with db:
                z = db.execute(
                    "INSERT INTO strecken (name, punkte, treffer, schuesse, am, runde) VALUES (?, ?, ?, ?, ?, ?)",
                    (name_bereinigen(d.get("name")), d["punkte"], d["treffer"], d["schuesse"], am, kennung))
                neu = z.lastrowid
                db.execute("""DELETE FROM strecken WHERE id NOT IN
                    (SELECT id FROM strecken ORDER BY punkte DESC, id LIMIT ?)""", (BEHALTEN,))
            # Bei gleichen Punkten steht der ältere Eintrag vorn, genau wie in der Liste
            rang = db.execute(
                "SELECT COUNT(*) FROM strecken WHERE punkte > ? OR (punkte = ? AND id < ?)",
                (d["punkte"], d["punkte"], neu)).fetchone()[0] + 1
            gesamt = db.execute("SELECT COUNT(*) FROM strecken").fetchone()[0]
            self.antwort(201, {"id": str(neu), "rang": rang, "gesamt": gesamt, "eintraege": liste_lesen(db)})
        except sqlite3.IntegrityError:
            self.fehler(409, "schon_eingetragen")
        finally:
            db.close()


def verwalten(args):
    db = verbinden()
    try:
        if args.liste:
            for z in db.execute("SELECT id, name, punkte, treffer, schuesse, am FROM strecken ORDER BY punkte DESC, id"):
                print(f"{z['id']:>5}  {z['punkte']:>6}  {z['treffer']:>4}/{z['schuesse']:<4}  {z['am']}  {z['name']}")
        else:
            with db:
                n = db.execute("DELETE FROM strecken WHERE id = ?", (args.loeschen,)).rowcount
            print(f"{n} Eintrag gelöscht")
    finally:
        db.close()


def main():
    teil = argparse.ArgumentParser(description="Bestenliste für Moorjagd")
    teil.add_argument("--liste", action="store_true", help="alle Einträge ausgeben")
    teil.add_argument("--loeschen", type=int, metavar="ID", help="einen Eintrag löschen")
    args = teil.parse_args()
    if args.liste or args.loeschen is not None:
        return verwalten(args)
    einrichten()
    Bearbeiter.geheim = geheimnis_laden()
    threading.Thread(target=taktgeber, daemon=True).start()
    server = ThreadingHTTPServer((ADRESSE, PORT), Bearbeiter)
    server.daemon_threads = True
    print(f"Moorjagd-Bestenliste auf {ADRESSE}:{PORT}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
