// Prüflauf über WebDriver BiDi, ohne Abhängigkeiten: Firefox headless, Maus und Finger simuliert
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { setTimeout as warte } from 'node:timers/promises';
import { dirname, join } from 'node:path';

// Aufruf: PRUEF_AUSGABE=<leerer Ordner für Bilder und Profil> node pruefung.mjs [Adresse der Seite]
const quelle = dirname(new URL(import.meta.url).pathname);
const hier = process.env.PRUEF_AUSGABE || quelle;
const profil = mkdtempSync(join(hier, 'ffprofil-'));
const ff = spawn('firefox', ['--headless', '--remote-debugging-port=9333', '--profile', profil, '--no-remote'], { stdio: 'ignore' });
let ws;
for (let i = 0; i < 40 && !ws; i++) {
  await warte(500);
  try {
    const v = new WebSocket('ws://127.0.0.1:9333/session');
    await new Promise((ok, no) => { v.onopen = ok; v.onerror = no; });
    ws = v;
  } catch {}
}
if (!ws) { ff.kill(); throw new Error('Firefox antwortet nicht'); }
let nr = 0; const offen = new Map(); const fehler = [];
ws.onmessage = m => {
  const d = JSON.parse(m.data);
  if (d.id && offen.has(d.id)) { const { ok, no } = offen.get(d.id); offen.delete(d.id); d.type === 'error' ? no(new Error(d.error + ': ' + d.message)) : ok(d.result); }
  else if (d.method === 'log.entryAdded' && (d.params.level === 'error')) fehler.push(d.params.text);
};
const ruf = (method, params = {}) => new Promise((ok, no) => { const id = ++nr; offen.set(id, { ok, no }); ws.send(JSON.stringify({ id, method, params })); });

await ruf('session.new', { capabilities: {} });
await ruf('session.subscribe', { events: ['log.entryAdded'] });
const { context } = await ruf('browsingContext.create', { type: 'tab' });
const auswerten = async ausdruck => {
  const r = await ruf('script.evaluate', { expression: ausdruck, target: { context }, awaitPromise: true });
  if (r.type === 'exception') throw new Error(r.exceptionDetails.text);
  return r.result.value;
};
const bildSichern = async name => {
  const r = await ruf('browsingContext.captureScreenshot', { context });
  writeFileSync(join(hier, name), Buffer.from(r.data, 'base64'));
};
const zeiger = (typ, id, schritte) => ruf('input.performActions', { context, actions: [{ type: 'pointer', id, parameters: { pointerType: typ }, actions: schritte }] });
const tipp = (typ, id, x, y, button = 0) => zeiger(typ, id, [
  { type: 'pointerMove', x: Math.round(x), y: Math.round(y), duration: 0 },
  { type: 'pointerDown', button }, { type: 'pointerUp', button },
]);
const url = (process.argv[2] || 'file://' + join(quelle, 'dist', 'moorjagd.html')) + '#pruefung';
const zustand = () => auswerten(`JSON.stringify({p:__moorjagd.G.punkte,m:__moorjagd.G.munition,s:__moorjagd.G.schuesse,t:__moorjagd.G.treffer,k:__moorjagd.G.kamera,ph:__moorjagd.G.phase,z:__moorjagd.G.zeit})`).then(JSON.parse);
// Sichtbares Huhn der nahen Ebene suchen und seine Bildschirmposition liefern
const zielHuhn = `(()=>{const M=__moorjagd,G=M.G;let b=null;for(const h of M.huehner){if(h.zustand!=='flug')continue;const l=M.LAGE[h.lage];const x=(h.x-G.kamera*l.p)*M.massstab,y=(h.y+M.obenY)*M.massstab;if(x>40&&x<innerWidth-40&&y>70&&y<innerHeight-110){if(!b||h.lage>b.lage)b={x,y,lage:h.lage,v:h.v*M.massstab}}}return b?JSON.stringify(b):''})()`;

const ergebnisse = [];
const pruefe = (name, ok, rest = '') => { ergebnisse.push(`${ok ? 'OK ' : 'FEHLER'}  ${name} ${rest}`); };

for (const [name, b, h, typ] of [['desktop', 1280, 800, 'mouse'], ['hochkant', 390, 844, 'touch'], ['quer', 844, 390, 'touch']]) {
  await ruf('browsingContext.setViewport', { context, viewport: { width: b, height: h } });
  // Über about:blank, sonst lädt dieselbe Adresse mit Anker nicht neu; Speicher leeren für einen frischen Start
  await ruf('browsingContext.navigate', { context, url: 'about:blank', wait: 'complete' });
  await ruf('browsingContext.navigate', { context, url, wait: 'complete' });
  await auswerten(`localStorage.clear()`);
  const sichtbar = await auswerten(`!document.getElementById('menue').hidden && document.getElementById('ende').hidden`);
  pruefe(`${name}: frisch geladen, Menü sichtbar`, sichtbar);
  await warte(1200);
  await bildSichern(`${name}-menue.png`);
  const ueberlauf = await auswerten(`document.documentElement.scrollWidth>innerWidth||document.getElementById('menue').scrollWidth>document.getElementById('menue').clientWidth`);
  pruefe(`${name}: kein Querüberlauf im Menü`, !ueberlauf);
  await auswerten(`document.getElementById('los').click()`);
  await warte(1800);
  // Treffer versuchen (bis zu 8 Schüsse)
  let vorher = await zustand(), treffer = false;
  for (let i = 0; i < 6 && !treffer; i++) {
    const roh = await auswerten(zielHuhn);
    if (!roh) { await warte(500); continue; }
    const z = JSON.parse(roh);
    await tipp(typ, typ + 'a', z.x, z.y);
    await warte(120);
    const nun = await zustand();
    if (nun.t > vorher.t) treffer = true;
    vorher = nun;
  }
  const nachSchuss = await zustand();
  pruefe(`${name}: Schuss zählt`, nachSchuss.s > 0, `Schüsse ${nachSchuss.s}, Munition ${nachSchuss.m}`);
  pruefe(`${name}: Huhn getroffen, Punkte steigen`, treffer && nachSchuss.p > 0, `Punkte ${nachSchuss.p}`);
  await warte(220);
  await bildSichern(`${name}-jagd.png`);

  // Umsehen
  const k0 = (await zustand()).k;
  if (typ === 'touch') {
    await zeiger('touch', 'wisch', [
      { type: 'pointerMove', x: Math.round(b * .7), y: Math.round(h * .5), duration: 0 },
      { type: 'pointerDown', button: 0 },
      { type: 'pointerMove', x: Math.round(b * .3), y: Math.round(h * .5), duration: 200 },
      { type: 'pointerUp', button: 0 },
    ]);
    const s0 = nachSchuss.s, n = await zustand();
    pruefe(`${name}: Wischen bewegt die Sicht`, n.k > k0 + 20, `${Math.round(k0)} → ${Math.round(n.k)}`);
    pruefe(`${name}: Wischen löst keinen Schuss aus`, n.s === s0);
  } else {
    await zeiger('mouse', 'mousea', [{ type: 'pointerMove', x: b - 5, y: Math.round(h / 2), duration: 0 }]);
    await warte(500);
    const n = await zustand();
    pruefe(`${name}: Maus am Rand bewegt die Sicht`, n.k > k0 + 20, `${Math.round(k0)} → ${Math.round(n.k)}`);
    await zeiger('mouse', 'mousea', [{ type: 'pointerMove', x: Math.round(b / 2), y: Math.round(h / 2), duration: 0 }]);
  }

  // Magazin leer schießen in den Himmel, Hinweis, nachladen
  const reloadTyp = typ;
  for (let i = 0; i < 10; i++) { await tipp(typ, typ + 'a', b / 2, 90); await warte(30); }
  const leer = await zustand();
  pruefe(`${name}: Magazin wird leer`, leer.m === 0, `Munition ${leer.m}`);
  await warte(100);
  await bildSichern(`${name}-leer.png`);
  if (reloadTyp === 'mouse') await tipp('mouse', 'mousea', b / 2, h / 2, 2);
  else { const r = await auswerten(`JSON.stringify(document.getElementById('laden').getBoundingClientRect())`).then(JSON.parse); await tipp('touch', 'touchb', r.x + r.width / 2, r.y + r.height / 2); }
  await warte(800);
  pruefe(`${name}: Nachladen füllt das Magazin`, (await zustand()).m === 8, `Munition ${(await zustand()).m}`);

  // Rundenende und Eintrag
  await auswerten(`__moorjagd.G.punkte=Math.max(__moorjagd.G.punkte,120);__moorjagd.G.zeit=.05`);
  await warte(400);
  pruefe(`${name}: Runde endet`, (await zustand()).ph === 'ende');
  await auswerten(`(()=>{document.getElementById('name').value='Prüfer';document.getElementById('eintragen-knopf').click()})()`);
  await warte(200);
  const eintrag = await auswerten(`document.querySelector('#strecken-ende .eintrag.neu span')?.textContent||''`);
  // Lokal landet er in der Geräteliste. Live lehnt der Server die künstlich gesetzten Punkte ab und schreibt nichts.
  const hinweis = await auswerten(`document.getElementById('eintragen-hinweis').textContent`);
  pruefe(`${name}: Streckenliste nimmt den Eintrag an oder erklärt die Ablehnung`, eintrag === 'Prüfer' || hinweis === 'Diese Punktzahl nimmt die Liste nicht an.', hinweis);
  const ueberlaufEnde = await auswerten(`document.documentElement.scrollWidth>innerWidth||document.getElementById('ende').scrollWidth>document.getElementById('ende').clientWidth`);
  pruefe(`${name}: kein Querüberlauf am Ende`, !ueberlaufEnde);
  await bildSichern(`${name}-ende.png`);
}
// PRUEF_ECHT=<Name>: eine echte Runde über die volle Zeit spielen und in die gemeinsame Liste eintragen
if (process.env.PRUEF_ECHT) {
  const testname = process.env.PRUEF_ECHT;
  await ruf('browsingContext.setViewport', { context, viewport: { width: 1280, height: 800 } });
  await ruf('browsingContext.navigate', { context, url: 'about:blank', wait: 'complete' });
  await ruf('browsingContext.navigate', { context, url, wait: 'complete' });
  await warte(1500);
  await auswerten(`document.getElementById('los').click()`);
  await warte(1500);
  for (let i = 0; i < 40 && (await zustand()).t < 1; i++) {
    const roh = await auswerten(zielHuhn);
    if (roh) { const z = JSON.parse(roh); await tipp('mouse', 'mousea', z.x, z.y); }
    await warte(300);
    if ((await zustand()).m === 0) await tipp('mouse', 'mousea', 640, 400, 2), await warte(700);
  }
  const start = Date.now();
  while ((await zustand()).ph !== 'ende' && Date.now() - start < 240000) await warte(1000);
  const st = await zustand();
  pruefe('echt: Runde lief bis zum Ende', st.ph === 'ende', `Punkte ${st.p}, nach ${Math.round((Date.now() - start) / 1000)} s`);
  await auswerten(`(()=>{document.getElementById('name').value=${JSON.stringify(testname)};document.getElementById('eintragen-knopf').click()})()`);
  await warte(1500);
  pruefe('echt: Server bestätigt den Platz', /^Platz \d+ von \d+\.$/.test(await auswerten(`document.getElementById('eintragen-hinweis').textContent`)), await auswerten(`document.getElementById('eintragen-hinweis').textContent`));
  pruefe('echt: eigener Eintrag hervorgehoben', (await auswerten(`document.querySelector('#strecken-ende .eintrag.neu span')?.textContent||''`)) === testname);
  pruefe('echt: Quelle ist die gemeinsame Liste', (await auswerten(`document.getElementById('strecken-ende-quelle').textContent`)) === 'Gemeinsame Liste für alle.');
  await bildSichern('echt-ende.png');
  // Zweites, frisches Laden muss den Eintrag vom Server zeigen, auch ohne Gerätespeicher
  await auswerten(`localStorage.clear()`);
  await ruf('browsingContext.navigate', { context, url: 'about:blank', wait: 'complete' });
  await ruf('browsingContext.navigate', { context, url, wait: 'complete' });
  await warte(1500);
  pruefe('echt: Eintrag nach neuem Laden vom Server', (await auswerten(`[...document.querySelectorAll('#strecken .eintrag span')].map(e=>e.textContent).join('|')`)).includes(testname));
  await bildSichern('echt-menue.png');
}
console.log(ergebnisse.join('\n'));
console.log('Browser-Fehler:', fehler.length ? fehler.join(' | ') : 'keine');
await ruf('session.end').catch(() => {});
ff.kill();
process.exit(0);
