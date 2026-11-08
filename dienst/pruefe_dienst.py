#!/usr/bin/env python3
"""Prüft moorjagd-strecken.py lokal mit einer Wegwerf-Datenbank."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HIER = os.path.dirname(os.path.abspath(__file__))
PORT = 18097
BASIS = f"http://127.0.0.1:{PORT}/api/moorjagd"
HERKUNFT = "https://spiel.example.org"
fehler = 0


def ruf(pfad, daten=None, ip="2001:db8:1:2::5", origin=HERKUNFT, roh=None, typ="application/json"):
    kopf = {"X-Forwarded-For": ip}
    if origin:
        kopf["Origin"] = origin
    koerper = None
    if roh is not None or daten is not None:
        koerper = roh if roh is not None else json.dumps(daten).encode()
        kopf["Content-Type"] = typ
    a = urllib.request.Request(BASIS + pfad, data=koerper, headers=kopf, method="POST" if koerper is not None else "GET")
    try:
        with urllib.request.urlopen(a, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def pruefe(name, ok, rest=""):
    global fehler
    fehler += 0 if ok else 1
    print(("OK     " if ok else "FEHLER ") + name, rest)


def eintrag(marke, name="Anna", punkte=500, treffer=20, schuesse=40, **kw):
    return ruf("/strecken", {"runde": marke, "name": name, "punkte": punkte, "treffer": treffer, "schuesse": schuesse}, **kw)


def marke(ip="2001:db8:1:2::5"):
    return ruf("/runde", {}, ip=ip)[1]["runde"]


with tempfile.TemporaryDirectory() as ordner:
    umgebung = dict(os.environ, STATE_DIRECTORY=ordner, MOORJAGD_ADRESSE="127.0.0.1", MOORJAGD_PORT=str(PORT),
                    MOORJAGD_VERTRAUT="127.0.0.1", MOORJAGD_HERKUNFT=HERKUNFT, MOORJAGD_MINDESTDAUER="2")
    dienst = subprocess.Popen([sys.executable, "-I", os.path.join(HIER, "moorjagd-strecken.py")], env=umgebung)
    try:
        time.sleep(.6)
        s, d = ruf("/strecken")
        pruefe("leere Liste", s == 200 and d == {"eintraege": [], "gesamt": 0}, d)

        s, d = ruf("/runde", {}, origin="https://boese.example")
        pruefe("fremde Herkunft abgelehnt", s == 403 and d["fehler"] == "herkunft")
        s, d = ruf("/runde", {}, origin=None)
        pruefe("fehlende Herkunft abgelehnt", s == 403)
        s, d = ruf("/runde", roh=b"x" * 2000)
        pruefe("zu großer Körper abgelehnt", s == 413)
        s, d = ruf("/runde", {}, typ="text/plain")
        pruefe("falscher Inhaltstyp abgelehnt", s == 415)

        m1 = marke()
        s, d = eintrag(m1)
        pruefe("zu frühe Meldung abgelehnt", s == 403 and d["fehler"] == "zu_frueh", d)
        s, d = eintrag("1.aaaaaaaaaaaaaaaaaaaaaaaa." + "b" * 32)
        pruefe("gefälschte Marke abgelehnt", s == 403 and d["fehler"] == "marke")
        teile = m1.split(".")
        s, d = eintrag(f"{int(teile[0]) - 100}.{teile[1]}.{teile[2]}")
        pruefe("zurückdatierte Marke abgelehnt", s == 403 and d["fehler"] == "marke")

        m2, m3, m4 = marke(), marke(), marke()
        time.sleep(2.2)
        for name, werte in [("zu viele Punkte je Treffer", dict(punkte=500, treffer=2)),
                            ("mehr Treffer als Schüsse", dict(treffer=50, schuesse=10)),
                            ("Punkte als Text", dict(punkte="500")),
                            ("Punkte als Wahrheitswert", dict(punkte=True)),
                            ("keine Punkte", dict(punkte=0)),
                            ("unmöglich viele Punkte", dict(punkte=999999, treffer=999, schuesse=999))]:
            s, d = eintrag(m1, **werte)
            pruefe(f"{name} abgelehnt", s == 422, d)

        s, d = eintrag(m1)
        pruefe("gültiger Eintrag", s == 201 and d["rang"] == 1 and d["gesamt"] == 1, d)
        s, d = eintrag(m1, name="Nochmal")
        pruefe("Marke gilt nur einmal", s == 409 and d["fehler"] == "schon_eingetragen")

        s, d = eintrag(m2, name="  <b>Be‮rt\u0000 </b>  zu langer Name hier  ", punkte=900, treffer=20)
        pruefe("Name bereinigt und gekürzt", s == 201 and d["eintraege"][0]["name"] == "<b>Bert </b> zu", d["eintraege"][0])
        pruefe("Sortierung nach Punkten", [e["punkte"] for e in d["eintraege"]] == [900, 500])
        s, d = eintrag(m3, name="", punkte=500)
        pruefe("leerer Name wird Unbekannt, Gleichstand hinten", s == 201 and d["rang"] == 3 and d["eintraege"][2]["name"] == "Unbekannt", d["rang"])
        s, d = ruf("/strecken")
        pruefe("Liste öffentlich lesbar", s == 200 and d["gesamt"] == 3)

        s, d = ruf("/unbekannt")
        pruefe("unbekannter Pfad 404", s == 404)

        # Mengenbegrenzung: gleiche /64, andere Endadresse zählt mit
        grenze = None
        for i in range(125):
            s, d = ruf("/runde", {}, ip=f"2001:db8:9:9::{i + 1:x}")
            if s == 429:
                grenze = i
                break
        pruefe("Mengenbegrenzung greift je /64", grenze == 120, grenze)
        s, d = ruf("/runde", {}, ip="2001:db8:9:a::1")
        pruefe("anderes /64 nicht betroffen", s == 201)

        with open(os.path.join(ordner, "strecken.db"), "rb") as f:
            inhalt = f.read()
        pruefe("keine IP-Adresse in der Datenbank", b"2001:db8" not in inhalt)
        pruefe("Geheimnis nur für den Dienst lesbar", os.stat(os.path.join(ordner, "geheim")).st_mode & 0o077 == 0)

        ausgabe = subprocess.run([sys.executable, "-I", os.path.join(HIER, "moorjagd-strecken.py"), "--liste"], env=umgebung, capture_output=True, text=True).stdout
        pruefe("Verwaltung listet", len(ausgabe.strip().splitlines()) == 3)
        erste_id = ausgabe.split()[0]
        subprocess.run([sys.executable, "-I", os.path.join(HIER, "moorjagd-strecken.py"), "--loeschen", erste_id], env=umgebung, capture_output=True)
        pruefe("Verwaltung löscht", ruf("/strecken")[1]["gesamt"] == 2)
    finally:
        dienst.terminate()
        dienst.wait(5)

    # Fremde Quelle: Dienst läuft, vertraut aber einer anderen Adresse
    umgebung["MOORJAGD_VERTRAUT"] = "192.0.2.1"
    dienst = subprocess.Popen([sys.executable, "-I", os.path.join(HIER, "moorjagd-strecken.py")], env=umgebung)
    try:
        time.sleep(.6)
        s, d = ruf("/strecken")
        pruefe("direkter Zugriff ohne Caddy abgelehnt", s == 403, d)
    finally:
        dienst.terminate()
        dienst.wait(5)

print("Fehler:", fehler)
sys.exit(1 if fehler else 0)
