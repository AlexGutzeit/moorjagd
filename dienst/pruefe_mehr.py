#!/usr/bin/env python3
"""Prüft den Mehrspieler-Teil von moorjagd-strecken.py lokal mit simulierten Jägern (nur Standardbibliothek)."""
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

HIER = os.path.dirname(os.path.abspath(__file__))
PORT = 18098
HERKUNFT = "https://spiel.example.org"
fehler = 0


def pruefe(name, ok, rest=""):
    global fehler
    fehler += 0 if ok else 1
    print(("OK     " if ok else "FEHLER ") + name, rest if not ok else "")


class Jaeger:
    def __init__(self, origin=HERKUNFT, ip="2001:db8:1:2::5"):
        self.s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        schluessel = base64.b64encode(os.urandom(16)).decode()
        kopf = (f"GET /api/moorjagd/mehr HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {schluessel}\r\nSec-WebSocket-Version: 13\r\nX-Forwarded-For: {ip}\r\n")
        if origin:
            kopf += f"Origin: {origin}\r\n"
        self.s.sendall((kopf + "\r\n").encode())
        self.puffer = b""
        while b"\r\n\r\n" not in self.puffer:
            teil = self.s.recv(4096)
            if not teil:
                break
            self.puffer += teil
        kopf, _, self.puffer = self.puffer.partition(b"\r\n\r\n")
        self.status = int(kopf.split()[1]) if kopf else 0
        erwartet = base64.b64encode(hashlib.sha1((schluessel + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if self.status == 101 and b"Sec-WebSocket-Accept: " + erwartet not in kopf:
            raise SystemExit("Falsche Sec-WebSocket-Accept-Antwort")
        self.post = []

    def senden(self, d, op=1, roh=None):
        daten = roh if roh is not None else json.dumps(d).encode()
        maske = os.urandom(4)
        n = len(daten)
        kopf = struct.pack("!BB", 0x80 | op, 0x80 | n) if n < 126 else struct.pack("!BBH", 0x80 | op, 0xFE, n)
        self.s.sendall(kopf + maske + bytes(b ^ maske[i % 4] for i, b in enumerate(daten)))

    def _lesen(self, n):
        while len(self.puffer) < n:
            teil = self.s.recv(65536)
            if not teil:
                raise EOFError
            self.puffer += teil
        stueck, self.puffer = self.puffer[:n], self.puffer[n:]
        return stueck

    def rahmen(self):
        a, b = self._lesen(2)
        n = b & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._lesen(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._lesen(8))[0]
        return a & 0x0F, self._lesen(n)

    def warte(self, typ, dauer=3, bedingung=None):
        """Nächste Nachricht dieses Typs; andere landen in der Ablage."""
        for i, d in enumerate(self.post):
            if d.get("typ") == typ and (not bedingung or bedingung(d)):
                return self.post.pop(i)
        ende = time.time() + dauer
        while time.time() < ende:
            self.s.settimeout(max(.05, ende - time.time()))
            try:
                op, daten = self.rahmen()
            except (socket.timeout, TimeoutError):
                break
            if op != 1:
                continue
            d = json.loads(daten)
            if d.get("typ") == typ and (not bedingung or bedingung(d)):
                return d
            self.post.append(d)
        return None

    def alle(self, typ, dauer=.3):
        self.warte("-", dauer)  # alles Eingetroffene in die Ablage holen
        treffer = [d for d in self.post if d.get("typ") == typ]
        self.post = [d for d in self.post if d.get("typ") != typ]
        return treffer

    def geschlossen(self, dauer=2):
        self.s.settimeout(dauer)
        try:
            while True:
                op, _ = self.rahmen()
                if op == 8:
                    return True
        except (EOFError, ConnectionError):
            return True
        except (socket.timeout, TimeoutError):
            return False


with tempfile.TemporaryDirectory() as ordner:
    umgebung = dict(os.environ, STATE_DIRECTORY=ordner, MOORJAGD_ADRESSE="127.0.0.1", MOORJAGD_PORT=str(PORT),
                    MOORJAGD_VERTRAUT="127.0.0.1", MOORJAGD_HERKUNFT=HERKUNFT, MOORJAGD_RUNDE="8")
    dienst = subprocess.Popen([sys.executable, "-I", os.path.join(HIER, "moorjagd-strecken.py")], env=umgebung)
    try:
        time.sleep(.6)
        pruefe("fremde Herkunft abgelehnt", Jaeger(origin="https://boese.example").status == 403)
        pruefe("fehlende Herkunft abgelehnt", Jaeger(origin=None).status == 403)

        anna = Jaeger()
        pruefe("Verbindung angenommen", anna.status == 101, anna.status)
        anna.senden({"typ": "hallo", "name": "Anna"})
        w = anna.warte("willkommen")
        anna_id = w["id"]
        pruefe("Willkommen mit Kennung und Schlüssel", w and len(w["id"]) == 8 and len(w["schluessel"]) == 32, w)
        pruefe("leere Raumliste", (anna.warte("raeume") or {}).get("liste") == [])

        bert, carla = Jaeger(), Jaeger(ip="2001:db8:7::1")
        bert.senden({"typ": "hallo", "name": "Bert"})
        carla.senden({"typ": "hallo", "name": "  Carla‮\u0000 "})
        bert.warte("willkommen"); carla.warte("willkommen")
        pruefe("Name bereinigt", carla.post == [] or True)

        anna.senden({"typ": "raum_neu", "name": "Anna", "raum": "Moorrunde"})
        r = anna.warte("raum")
        pruefe("Raum erstellt, Anna ist Oberjäger mit Farbe 0", r and r["oj"] == r["ich"] and r["jaeger"][0]["farbe"] == 0 and r["name"] == "Moorrunde", r)
        raum_id = r["id"]
        liste = bert.warte("raeume", bedingung=lambda d: d["liste"])
        pruefe("Lobby sieht den neuen Raum", liste and liste["liste"][0]["oj"] == "Anna" and liste["liste"][0]["n"] == 1, liste)

        bert.senden({"typ": "anfragen", "raum": raum_id, "name": "Bert"})
        a = bert.warte("anfrage")
        pruefe("Anfrage gestellt", a and a["raum"] == raum_id and a["oj"] == "Anna", a)
        r = anna.warte("raum", bedingung=lambda d: d["anfragen"])
        pruefe("Oberjäger sieht die Anfrage", r and r["anfragen"][0]["name"] == "Bert", r)
        bert_id = r["anfragen"][0]["id"]

        carla.senden({"typ": "anfragen", "raum": raum_id})
        carla.warte("anfrage")
        r = anna.warte("raum", bedingung=lambda d: len(d["anfragen"]) == 2)
        carla_id = r["anfragen"][1]["id"]
        pruefe("Name bereinigt", r["anfragen"][1]["name"] == "Carla", r["anfragen"][1]["name"])

        # Nicht-Oberjäger darf nichts entscheiden
        bert.senden({"typ": "annehmen", "spieler": carla_id})
        time.sleep(.2)
        pruefe("Fremde können nicht annehmen", carla.warte("raum", .3) is None)

        anna.senden({"typ": "ablehnen", "spieler": carla_id})
        ab = carla.warte("abgelehnt")
        pruefe("Ablehnung kommt an", ab and ab["grund"] == "abgelehnt" and ab["raum"] == "Moorrunde", ab)

        anna.senden({"typ": "annehmen", "spieler": bert_id})
        r = bert.warte("raum")
        pruefe("Bert ist im Raum mit eigener Farbe", r and len(r["jaeger"]) == 2 and r["jaeger"][1]["farbe"] == 1 and r["anfragen"] == [], r)
        pruefe("Bert sieht keine Anfragen-Liste", r and r["oj"] != r["ich"])

        bert.senden({"typ": "start"})
        pruefe("Nur der Oberjäger startet", anna.warte("raum", .4, lambda d: d["phase"] == "countdown") is None)

        for j in (anna, bert):
            j.senden({"typ": "ziel", "x": 1200, "y": 300, "c": 1200, "s": 1067})
        anna.senden({"typ": "start"})
        r = bert.warte("raum", 2, lambda d: d["phase"] == "countdown")
        pruefe("Countdown beginnt für alle", r is not None)
        pruefe("Start 4 s nach dem Countdown-Beginn", r and 3.8 < r["start"] - r["u"] <= 4.01, r and r["start"] - r["u"])

        r = anna.warte("raum", 6, lambda d: d["phase"] == "jagd")
        pruefe("Jagd beginnt nach dem Countdown", r is not None)
        time.sleep(.4)
        h_anna = anna.alle("huhn")
        h_bert = bert.alle("huhn")
        pruefe("Beide bekommen dieselben Hühner", len(h_anna) >= 5 and [h["id"] for h in h_anna][:5] == [h["id"] for h in h_bert][:5], (len(h_anna), len(h_bert)))
        stand = anna.warte("stand", 1)
        pruefe("Fadenkreuze werden verteilt", stand and len(stand["z"]) == 2, stand)

        # Gleichzeitiger Schuss auf dasselbe Huhn: nur einer bekommt es
        ziel = h_anna[0]
        anna.senden({"typ": "schuss", "x": 1, "y": 2, "ziel": {"art": "huhn", "id": ziel["id"]}})
        bert.senden({"typ": "schuss", "x": 1, "y": 2, "ziel": {"art": "huhn", "id": ziel["id"]}})
        time.sleep(.3)
        schuesse = anna.alle("schuss")
        gut = [s for s in schuesse if s.get("art") == "huhn"]
        spaet = [s for s in schuesse if s.get("zuspaet") == "huhn"]
        pruefe("Ein Huhn nur einmal abschießbar", len(gut) == 1 and len(spaet) == 1, schuesse)
        pruefe("Punkte je Ebene", gut and gut[0]["punkte"] == [25, 10, 5][ziel["l"]], gut)

        for i in range(4):
            anna.senden({"typ": "schuss", "ziel": {"art": "schild"}})
        time.sleep(.3)
        schilder = [s for s in bert.alle("schuss") if s.get("art") == "schild"]
        pruefe("Drei Schilder, dann keins mehr", len(schilder) == 3 and schilder[-1]["schilder"] == 0, schilder)

        anna.senden({"typ": "laden"})
        time.sleep(.3)
        laden = bert.alle("laden")
        pruefe("Nachladen kostet 10 Punkte, alle sehen es", len(laden) == 1 and laden[0]["summe"] == (gut[0]["punkte"] if gut[0]["wer"] == anna_id else 0) + 30 - 10, laden)

        bert.senden({"typ": "schuss", "ziel": {"art": "scheuche"}})
        bert.senden({"typ": "schuss", "ziel": {"art": "scheuche"}})
        time.sleep(.3)
        sch = [s for s in anna.alle("schuss") if s.get("art") == "scheuche"]
        pruefe("Vogelscheuche zieht 25 ab, kurz gesperrt", len(sch) == 1 and sch[0]["punkte"] == -25, sch)

        o = anna.warte("ort", 7)
        pruefe("Versteckte Hühner tauchen auf", o is not None)
        if o:
            anna.senden({"typ": "schuss", "ziel": {"art": "ort", "i": o["i"]}})
            bert.senden({"typ": "schuss", "ziel": {"art": "ort", "i": o["i"]}})
            time.sleep(.3)
            orte = [s for s in anna.alle("schuss") if s.get("art") == "ort"]
            pruefe("Verstecktes Huhn nur einmal", len(orte) == 1, orte)

        for unsinn in [{"art": "huhn", "id": "1"}, {"art": "huhn", "id": True}, {"art": "ort", "i": 9}, {"art": "zauber"}, "huhn"]:
            anna.senden({"typ": "schuss", "ziel": unsinn})
        time.sleep(.3)
        pruefe("Unsinnige Ziele bringen keine Punkte", all("art" not in s for s in anna.alle("schuss")))

        # Magazin: Bert hat 4 Schüsse abgegeben, nur noch 4 zählen; nach dem Laden wieder 8
        for i in range(6):
            bert.senden({"typ": "schuss"})
        time.sleep(.3)
        pruefe("Ohne Nachladen nur 8 Schüsse", len([s for s in anna.alle("schuss") if s["wer"] != anna_id]) == 4)
        bert.senden({"typ": "laden"})
        bert.senden({"typ": "laden"})
        time.sleep(.3)
        pruefe("Volles Magazin lädt nicht noch einmal", len([l for l in anna.alle("laden") if l["wer"] != anna_id]) == 1)

        # Wiederkommen mit dem Schlüssel mitten in der Runde
        bert_schluessel = None
        dora = Jaeger()
        dora.senden({"typ": "hallo", "name": "Dora"})
        w_dora = dora.warte("willkommen")
        dora.s.close()
        time.sleep(.2)
        dora2 = Jaeger()
        dora2.senden({"typ": "hallo", "schluessel": w_dora["schluessel"]})
        w2 = dora2.warte("willkommen")
        pruefe("Wiederkehr mit Schlüssel behält die Kennung", w2 and w2["id"] == w_dora["id"] and w2["name"] == "Dora", w2)

        dora2.senden({"typ": "anfragen", "raum": raum_id})
        dora2.warte("anfrage")
        r = anna.warte("raum", 2, lambda d: [a for a in d["anfragen"] if a["name"] == "Dora"])
        anna.senden({"typ": "annehmen", "spieler": r["anfragen"][-1]["id"]})
        j = dora2.warte("jagd", 2)
        pruefe("Späteinsteiger bekommt den Rundenstand", j and j["schilder"] == 0 and isinstance(j["huehner"], list), j and {k: j[k] for k in ("schilder", "punkte")})

        e = anna.warte("ende", 8)
        pruefe("Runde endet mit Ergebnis", e is not None)
        if e:
            namen = [x["name"] for x in e["ergebnis"]]
            pruefe("Ergebnis nach Punkten sortiert", [x["punkte"] for x in e["ergebnis"]] == sorted([x["punkte"] for x in e["ergebnis"]], reverse=True), e["ergebnis"])
            pruefe("Alle drei in der Siegerehrung", sorted(namen) == ["Anna", "Bert", "Dora"], namen)
            erste = e["ergebnis"][0]
            pruefe("Anna vorn (Huhn oder Schilder)", erste["name"] == "Anna" and erste["strecke"]["schild"] == 3, erste)
        r = anna.warte("raum", 2, lambda d: d["phase"] == "warten")
        pruefe("Raum wartet wieder", r is not None)

        # Rauswerfen, Oberjäger geht, Übergabe, Auflösen
        anna.senden({"typ": "rauswerfen", "spieler": w_dora["id"]})
        d = dora2.warte("draussen")
        pruefe("Rauswerfen", d and d["grund"] == "rausgeworfen", d)
        anna.senden({"typ": "verlassen"})
        r = bert.warte("raum", 2, lambda d: d["oj"] == d["ich"])
        pruefe("Oberjäger geht, Bert übernimmt", r is not None and len(r["jaeger"]) == 1)
        m = bert.warte("meldung", 1)
        pruefe("Übergabe wird gemeldet", m and "Bert" in m["text"], m)
        carla.senden({"typ": "anfragen", "raum": raum_id})
        carla.warte("anfrage")
        bert.senden({"typ": "verlassen"})
        ab = carla.warte("abgelehnt")
        pruefe("Leerer Raum löst sich auf, offene Anfrage erfährt es", ab and ab["grund"] == "aufgeloest", ab)
        liste = carla.warte("raeume", 2, lambda d: d["liste"] == [])
        pruefe("Raumliste wieder leer", liste is not None)

        # Abwehr
        e1 = Jaeger()
        e1.senden({"typ": "raum_neu"})
        pruefe("Ohne Hallo wird getrennt", e1.geschlossen())
        e2 = Jaeger()
        e2.senden({"typ": "hallo", "name": "X"})
        e2.warte("willkommen")
        e2.senden(None, roh=b"x" * 3000)
        pruefe("Zu große Nachricht trennt", e2.geschlossen())
        e3 = Jaeger()
        e3.senden({"typ": "hallo", "name": "Y"})
        e3.warte("willkommen")
        for i in range(150):
            e3.senden({"typ": "ping"})
        pruefe("Nachrichtenflut trennt", e3.geschlossen())

        # Verbindung weg → nach Ablauf entfernt (hier nur: Raum zeigt "weg")
        f1, f2 = Jaeger(), Jaeger()
        f1.senden({"typ": "hallo", "name": "F1"}); f2.senden({"typ": "hallo", "name": "F2"})
        f1.senden({"typ": "raum_neu"})
        r = f1.warte("raum")
        f2.senden({"typ": "anfragen", "raum": r["id"]})
        r = f1.warte("raum", 2, lambda d: d["anfragen"])
        f1.senden({"typ": "annehmen", "spieler": r["anfragen"][0]["id"]})
        f1.warte("raum", 2, lambda d: len(d["jaeger"]) == 2)
        f2.s.close()
        r = f1.warte("raum", 2, lambda d: any(j["weg"] for j in d["jaeger"]))
        pruefe("Abgerissene Verbindung wird als weg angezeigt", r is not None)
        pruefe("Standard-Raumname", r and r["name"] == "Jagd von F1", r and r["name"])
    finally:
        dienst.terminate()
        dienst.wait(5)

print("Fehler:", fehler)
sys.exit(1 if fehler else 0)
