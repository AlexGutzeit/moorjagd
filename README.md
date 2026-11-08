# Moorjagd

Hühnerjagd im herbstlichen Moor: 90 Sekunden, 8 Patronen und ein Moor voller frecher Hühner.
Ein Browserspiel in einer einzigen HTML-Datei, spielbar mit Maus oder Touch, allein oder mit anderen.

![Jagd im Moor](bilder/jagd.png)

## Spielen

- **Linksklick / Tippen:** schießen
- **Rechtsklick / Leertaste / R:** nachladen (kostet 10 Punkte, Reste im Magazin sind verloren; das erste Magazin ist frei)
- **Bildrand, Mausrad, Pfeiltasten oder A/D:** umsehen
- **Esc / P:** Pause (im Mehrspieler: kurze Rast, die Jagd läuft weiter)

Je weiter weg ein Huhn fliegt, desto mehr Punkte bringt es (5, 10 oder 25). Dazu kommen versteckte Hühner
in Mühle, Brunnen, Eiche und Heuhaufen (15 bis 25), der Ballon (50) und die Wegweiser (10 je Schild).
Wer die Vogelscheuche trifft, verliert 25 Punkte.

**Mehrspieler:** Ein Jäger macht einen Raum auf und nimmt Mitjäger an (bis zu 8). Nach einem Countdown jagen alle
90 Sekunden lang im selben Moor. Der Server lässt die Hühner auftauchen und entscheidet, wer zuerst getroffen hat.
Danach gibt es eine Siegerehrung mit Podest.

![Siegerehrung](bilder/siegerehrung.png)

## Aufbau

| Datei | Zweck |
|---|---|
| `moorjagd.html` | das ganze Spiel (Grafik auf Canvas gezeichnet, Ton per Web Audio, keine Bibliotheken) |
| `baue.py` | baut `dist/moorjagd.html`: vollständiges HTML-Gerüst, Schriften eingebettet, keine Anfragen an fremde Server |
| `schriften/` | Grenze und Grenze Gotisch (SIL Open Font License 1.1) |
| `dienst/moorjagd-strecken.py` | Bestenliste (SQLite) und Mehrspieler (WebSocket), nur Python-Standardbibliothek |
| `dienst/moorjagd-strecken.service` | systemd-Unit mit `DynamicUser` und Härtung |
| `dienst/Caddyfile.beispiel` | Beispiel für den Reverse-Proxy |
| `dienst/pruefe_dienst.py`, `dienst/pruefe_mehr.py` | Prüfungen für Bestenliste und Mehrspieler-Protokoll |
| `pruefung.mjs`, `pruefung_mehr.mjs` | Browser-Prüfungen mit Firefox headless über WebDriver BiDi |

Ohne Server läuft das Spiel allein, die Bestenliste bleibt dann auf dem Gerät (`localStorage`).
Mit gesetztem `data-api` am `<html>`-Element (das macht `baue.py`: `/api/moorjagd`) nutzt es die gemeinsame
Bestenliste und bietet „Mit anderen jagen“ an.

## Selbst betreiben

```sh
python3 baue.py                      # erzeugt dist/moorjagd.html
```

`dist/moorjagd.html` auf einen Webserver legen. Für Bestenliste und Mehrspieler zusätzlich den Dienst:

```sh
install -D -m 644 dienst/moorjagd-strecken.py /usr/local/lib/moorjagd/moorjagd-strecken.py
install -m 644 dienst/moorjagd-strecken.service /etc/systemd/system/
install -m 644 dienst/moorjagd-strecken.env.beispiel /etc/default/moorjagd-strecken   # anpassen
systemctl daemon-reload && systemctl enable --now moorjagd-strecken
```

Der Dienst nimmt nur Anfragen von `MOORJAGD_VERTRAUT` an (dem Reverse-Proxy, der `X-Forwarded-For` setzt)
und nur von den Origins in `MOORJAGD_HERKUNFT`. Der Proxy leitet `/api/moorjagd/*` an den Dienst weiter,
siehe `dienst/Caddyfile.beispiel`. WebSockets gehen bei Caddy ohne weitere Einstellung durch.

Verwaltung der Bestenliste:

```sh
python3 -I /usr/local/lib/moorjagd/moorjagd-strecken.py --liste
python3 -I /usr/local/lib/moorjagd/moorjagd-strecken.py --loeschen ID
```

Gespeichert werden Name, Punkte, Treffer, Schüsse und Zeitpunkt, keine IP-Adressen. Die Mengenbegrenzung
hält Adressen nur als gesalzene Prüfsumme im Arbeitsspeicher.

**Schutz, kein Betrugsschutz:** Rundenmarken vom Server (HMAC, frühestens nach 85 s einlösbar, nur einmal),
höchstens 50 Punkte je Treffer und Mengengrenzen je Anschluss. Im Alleinspiel entstehen die Punkte aber im
Browser. Wer gezielt fälscht, kommt durch.

## Prüfen

```sh
python3 dienst/pruefe_dienst.py      # Bestenliste mit Wegwerf-Datenbank
python3 dienst/pruefe_mehr.py        # Mehrspieler-Protokoll mit simulierten Jägern
PRUEF_AUSGABE=$(mktemp -d) node pruefung_mehr.mjs   # zwei Firefox-Fenster spielen gegeneinander
PRUEF_AUSGABE=$(mktemp -d) node pruefung.mjs <Adresse der Seite>
```

Für die Browser-Prüfungen werden Node.js ab Version 22 und Firefox gebraucht.

## Lizenz

Code unter [MIT](LICENSE). Die Schriften Grenze und Grenze Gotisch von Omnibus-Type stehen unter der
[SIL Open Font License 1.1](schriften/OFL.txt).
