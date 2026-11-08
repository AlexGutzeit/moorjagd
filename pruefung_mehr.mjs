// Mehrspieler-Prüflauf: Dienst lokal, zwei Firefox-Fenster (Desktop mit Maus, Telefon hochkant mit Finger) spielen gegeneinander
// Aufruf: PRUEF_AUSGABE=<leerer Ordner für Bilder und Profil> node pruefung_mehr.mjs
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { setTimeout as warte } from 'node:timers/promises';
import { dirname, join } from 'node:path';

const quelle = dirname(new URL(import.meta.url).pathname);
const hier = process.env.PRUEF_AUSGABE || mkdtempSync('/tmp/moorjagd-mehr-');
const API = 18099, SEITE = 18100;
const seitenOrdner = mkdtempSync(join(hier, 'seite-'));
writeFileSync(join(seitenOrdner, 'moorjagd.html'),
  readFileSync(join(quelle, 'dist', 'moorjagd.html'), 'utf8').replace('data-api="/api/moorjagd"', `data-api="http://127.0.0.1:${API}/api/moorjagd"`));
const zustandOrdner = mkdtempSync(join(hier, 'zustand-'));
const dienst = spawn('python3', ['-I', join(quelle, 'dienst', 'moorjagd-strecken.py')], {
  stdio: ['ignore', 'inherit', 'inherit'],
  env: { ...process.env, STATE_DIRECTORY: zustandOrdner, MOORJAGD_ADRESSE: '127.0.0.1', MOORJAGD_PORT: String(API), MOORJAGD_VERTRAUT: '127.0.0.1',
    MOORJAGD_HERKUNFT: `http://127.0.0.1:${SEITE}`, MOORJAGD_RUNDE: '16' },
});
const statisch = spawn('python3', ['-m', 'http.server', String(SEITE), '--bind', '127.0.0.1', '--directory', seitenOrdner], { stdio: 'ignore' });
const profil = mkdtempSync(join(hier, 'ffprofil-'));
const ff = spawn('firefox', ['--headless', '--remote-debugging-port=9334', '--profile', profil, '--no-remote'], { stdio: 'ignore' });
const aufraeumen = () => { for (const p of [ff, dienst, statisch]) try { p.kill(); } catch {} };

let ws;
for (let i = 0; i < 40 && !ws; i++) {
  await warte(500);
  try {
    const v = new WebSocket('ws://127.0.0.1:9334/session');
    await new Promise((ok, no) => { v.onopen = ok; v.onerror = no; });
    ws = v;
  } catch {}
}
if (!ws) { aufraeumen(); throw new Error('Firefox antwortet nicht'); }
let nr = 0; const offen = new Map(); const fehler = [];
ws.onmessage = m => {
  const d = JSON.parse(m.data);
  if (d.id && offen.has(d.id)) { const { ok, no } = offen.get(d.id); offen.delete(d.id); d.type === 'error' ? no(new Error(d.error + ': ' + d.message)) : ok(d.result); }
  else if (d.method === 'log.entryAdded' && d.params.level === 'error') fehler.push(d.params.text);
};
const ruf = (method, params = {}) => new Promise((ok, no) => { const id = ++nr; offen.set(id, { ok, no }); ws.send(JSON.stringify({ id, method, params })); });
await ruf('session.new', { capabilities: {} });
await ruf('session.subscribe', { events: ['log.entryAdded'] });

const ergebnisse = [];
const pruefe = (name, ok, rest = '') => { ergebnisse.push(`${ok ? 'OK ' : 'FEHLER'}  ${name} ${ok ? '' : rest}`); };

function fenster(context, name) {
  const aus = async ausdruck => {
    const r = await ruf('script.evaluate', { expression: ausdruck, target: { context }, awaitPromise: true });
    if (r.type === 'exception') throw new Error(name + ': ' + r.exceptionDetails.text);
    return r.result.value;
  };
  return {
    name, aus,
    bild: async datei => { const r = await ruf('browsingContext.captureScreenshot', { context }); writeFileSync(join(hier, datei), Buffer.from(r.data, 'base64')); },
    klick: id => aus(`document.getElementById(${JSON.stringify(id)}).click()`),
    taste: wert => ruf('input.performActions', { context, actions: [{ type: 'key', id: 'tasten' + name, actions: [{ type: 'keyDown', value: wert }, { type: 'keyUp', value: wert }] }] }),
    sichtbar: id => aus(`!document.getElementById(${JSON.stringify(id)}).hidden`),
    tipp: (typ, x, y) => ruf('input.performActions', { context, actions: [{ type: 'pointer', id: typ + name, parameters: { pointerType: typ }, actions: [
      { type: 'pointerMove', x: Math.round(x), y: Math.round(y), duration: 0 }, { type: 'pointerDown', button: 0 }, { type: 'pointerUp', button: 0 }] }] }),
    zeige: (typ, x, y) => ruf('input.performActions', { context, actions: [{ type: 'pointer', id: typ + name, parameters: { pointerType: typ }, actions: [
      { type: 'pointerMove', x: Math.round(x), y: Math.round(y), duration: 0 }] }] }),
    async warteBis(ausdruck, ms = 5000) {
      const ende = Date.now() + ms;
      while (Date.now() < ende) { if (await aus(ausdruck)) return true; await warte(100); }
      return false;
    },
  };
}
const url = `http://127.0.0.1:${SEITE}/moorjagd.html#pruefung`;
const { context: ca } = await ruf('browsingContext.create', { type: 'tab' });
const { context: cb } = await ruf('browsingContext.create', { type: 'window' });
await ruf('browsingContext.setViewport', { context: ca, viewport: { width: 1280, height: 800 } });
await ruf('browsingContext.setViewport', { context: cb, viewport: { width: 390, height: 844 } });
const A = fenster(ca, 'A'), B = fenster(cb, 'B');
for (const [c] of [[ca], [cb]]) await ruf('browsingContext.navigate', { context: c, url, wait: 'complete' });
await warte(800);

const zielHuhn = `(()=>{const M=__moorjagd,G=M.G;let b=null;for(const h of M.huehner){if(h.zustand!=='flug'||!h.mp)continue;const l=M.LAGE[h.lage];const x=(h.x-G.kamera*l.p)*M.massstab,y=(h.y+M.obenY)*M.massstab;if(x>60&&x<innerWidth-60&&y>130&&y<innerHeight-120){if(!b||h.lage>b.lage)b={x,y,id:h.id}}}return b?JSON.stringify(b):''})()`;

try {
  pruefe('Mehrspieler-Knopf sichtbar', await A.sichtbar('mehr-knopf'));
  pruefe('Alleinspiel-Knopf heißt „Allein jagen“', (await A.aus(`document.getElementById('los').textContent`)) === 'Allein jagen');
  await A.bild('a-menue.png');

  // A erstellt einen Raum
  await A.klick('mehr-knopf');
  pruefe('A: Lobby offen und verbunden', await A.warteBis(`!document.getElementById('lobby').hidden && __moorjagd.MP.ich`));
  await A.aus(`document.getElementById('mp-name').value='';document.querySelector('#raum-neu button').click()`);
  pruefe('A: ohne Namen kein Raum, Hinweis', await A.warteBis(`document.getElementById('mp-status').textContent.includes('Namen')`, 1500));
  await A.aus(`document.getElementById('mp-name').value='Anna';document.getElementById('raum-name').value='Testjagd';document.querySelector('#raum-neu button').click()`);
  pruefe('A: Raum offen, A ist Oberjäger', await A.warteBis(`!document.getElementById('raum').hidden && document.getElementById('raum-titel').textContent==='Testjagd' && !document.getElementById('raum-start').hidden`));

  // B fragt an
  await B.klick('mehr-knopf');
  await B.aus(`document.getElementById('mp-name').value='Bert'`);
  pruefe('B: sieht den Raum von Anna', await B.warteBis(`document.querySelector('#raeume li')?.textContent.includes('Oberjäger Anna')`));
  await B.bild('b-lobby.png');
  await B.aus(`document.querySelector('#raeume li button').click()`);
  pruefe('B: Anfrage wartet', await B.warteBis(`!document.getElementById('anfrage-box').hidden && document.querySelector('#raeume li button').disabled`));
  await B.bild('b-anfrage.png');
  pruefe('A: sieht Anfrage von Bert', await A.warteBis(`!document.getElementById('anfragen-box').hidden && document.querySelector('#anfragen li .name')?.textContent==='Bert'`));
  await A.bild('a-anfrage.png');
  await A.aus(`document.querySelector('#anfragen li .auftakt').click()`);
  pruefe('B: ist im Raum', await B.warteBis(`!document.getElementById('raum').hidden && document.querySelectorAll('#jaeger li').length===2`));
  pruefe('B: kein Start-Knopf für Mitjäger', !(await B.sichtbar('raum-start')));
  const farben = await A.aus(`JSON.stringify([...document.querySelectorAll('#jaeger li small')].map(e=>e.textContent))`);
  pruefe('Jeder hat eine eigene Farbe', farben.includes('Rot') && farben.includes('Blau'), farben);
  await A.bild('a-raum.png');
  await B.bild('b-raum.png');

  // Start mit Countdown
  await A.klick('raum-start');
  const gesehen = { A: new Set(), B: new Set() };
  const ende = Date.now() + 6500;
  while (Date.now() < ende) {
    for (const F of [A, B]) {
      const t = await F.aus(`document.getElementById('countdown').hidden?'':document.getElementById('countdown').textContent`);
      if (t !== '') gesehen[F.name].add(t);
    }
    if (gesehen.A.has('3') && gesehen.A.size === 1) await A.bild('a-countdown.png');
    if ((await A.aus(`__moorjagd.G.phase`)) === 'jagd' && (await B.aus(`__moorjagd.G.phase`)) === 'jagd') break;
    await warte(120);
  }
  pruefe('Countdown 3–2–1–0 bei A', ['3', '2', '1', '0'].every(z => gesehen.A.has(z)), [...gesehen.A].join(','));
  pruefe('Countdown 3–2–1–0 bei B', ['3', '2', '1', '0'].every(z => gesehen.B.has(z)), [...gesehen.B].join(','));
  pruefe('Beide jagen', (await A.aus(`__moorjagd.G.phase`)) === 'jagd' && (await B.aus(`__moorjagd.G.phase`)) === 'jagd');
  await warte(600);
  const idsA = JSON.parse(await A.aus(`JSON.stringify(__moorjagd.huehner.filter(h=>h.mp).map(h=>h.id))`));
  const idsB = JSON.parse(await B.aus(`JSON.stringify(__moorjagd.huehner.filter(h=>h.mp).map(h=>h.id))`));
  pruefe('Dieselben Hühner bei beiden', idsA.length >= 5 && idsA.filter(i => idsB.includes(i)).length >= 5, `${idsA} | ${idsB}`);
  const lokal = await A.aus(`__moorjagd.huehner.filter(h=>!h.mp && h.zustand==='flug').length`);
  pruefe('Keine eigenen Zufallshühner im Mehrspieler', lokal === 0, lokal);

  // B zielt (Finger) in die Mitte; A sieht Berts Fadenkreuz
  await B.tipp('touch', 195, 200);
  await A.zeige('mouse', 640, 380);
  pruefe('A sieht das Fadenkreuz von B', await A.warteBis(`__moorjagd.MP.andere.size===1`, 3000));
  pruefe('B sieht das Fadenkreuz von A', await B.warteBis(`__moorjagd.MP.andere.size===1`, 3000));

  // A schießt Hühner, Punkte kommen vom Server und stehen bei beiden gleich
  let getroffen = false;
  for (let i = 0; i < 12 && !getroffen; i++) {
    const roh = await A.aus(zielHuhn);
    if (roh) {
      const z = JSON.parse(roh);
      await A.tipp('mouse', z.x, z.y);
      await warte(250);
      getroffen = (await A.aus(`__moorjagd.G.treffer`)) > 0;
    } else await warte(400);
    if ((await A.aus(`__moorjagd.G.munition`)) === 0) { await A.aus(`document.getElementById('laden').click()`); await warte(700); }
  }
  const pA = await A.aus(`__moorjagd.G.punkte`);
  pruefe('A trifft, Server gibt Punkte', getroffen && pA !== 0, pA);
  const annaId = await A.aus(`__moorjagd.MP.ich`);
  pruefe('B sieht Annas Punkte', await B.warteBis(`__moorjagd.MP.punkte[${JSON.stringify(annaId)}]===${pA}`, 2000));
  pruefe('Rangliste bei B zeigt beide', (await B.aus(`document.querySelectorAll('#rangliste li').length`)) === 2 && await B.sichtbar('rangliste'));
  // Beide schauen auf die Bildmitte; Bert zielt ins Moor, Anna etwas daneben
  await A.aus(`__moorjagd.G.kameraV=0`);
  await B.aus(`__moorjagd.G.kameraV=0`);
  const mitteA = await A.aus(`__moorjagd.G.kamera+innerWidth/__moorjagd.massstab/2`);
  await B.aus(`(()=>{const M=__moorjagd;M.G.kamera=${mitteA}-innerWidth/M.massstab/2})()`);
  await B.tipp('touch', 230, 560);
  await A.zeige('mouse', 520, 330);
  await warte(700);
  const kreuzB = JSON.parse(await A.aus(`(()=>{const M=__moorjagd;const a=[...M.MP.andere.values()][0];return JSON.stringify(a?{x:(a.x-M.G.kamera)*M.massstab,y:(a.y+M.obenY)*M.massstab}:null)})()`));
  pruefe('Berts Fadenkreuz liegt sichtbar in Annas Bild', kreuzB && kreuzB.x > 20 && kreuzB.x < 1260 && kreuzB.y > 40 && kreuzB.y < 780, JSON.stringify(kreuzB));
  await A.bild('a-jagd.png');
  await B.bild('b-zwei-kreuze.png');

  // B schießt per Finger
  let bTreffer = false;
  for (let i = 0; i < 12 && !bTreffer; i++) {
    const roh = await B.aus(zielHuhn);
    if (roh) { const z = JSON.parse(roh); await B.tipp('touch', z.x, z.y); await warte(250); bTreffer = (await B.aus(`__moorjagd.G.treffer`)) > 0; }
    else await warte(400);
    if ((await B.aus(`__moorjagd.G.munition`)) === 0) { await B.aus(`document.getElementById('laden').click()`); await warte(700); }
  }
  pruefe('B trifft per Finger', bTreffer, await B.aus(`__moorjagd.G.punkte`));
  await B.bild('b-jagd.png');

  // Esc: Rast-Tafel, das Spiel läuft weiter; nochmal Esc: weiter
  const ESC = '\uE00C';
  const z0 = await A.aus(`__moorjagd.G.zeit`);
  await A.taste(ESC);
  pruefe('A: Esc öffnet die Rast-Tafel', await A.warteBis(`!document.getElementById('rast').hidden`, 1500));
  await warte(1200);
  await A.bild('a-rast.png');
  pruefe('A: Runde läuft während der Rast weiter', (await A.aus(`__moorjagd.G.phase`)) === 'jagd' && (await A.aus(`__moorjagd.G.zeit`)) < z0 - 1, `${z0} → ${await A.aus(`__moorjagd.G.zeit`)}`);
  await A.taste(ESC);
  pruefe('A: nochmal Esc, zurück in der Jagd', await A.warteBis(`document.getElementById('rast').hidden && !document.getElementById('hud').hidden`, 1500));
  pruefe('B: Pause-Knopf im Mehrspieler sichtbar', await B.sichtbar('pause-knopf'));

  // Rundenende und Siegerehrung
  pruefe('A: Siegerehrung erscheint', await A.warteBis(`!document.getElementById('ehrung').hidden`, 20000));
  pruefe('B: Siegerehrung erscheint', await B.warteBis(`!document.getElementById('ehrung').hidden`, 3000));
  const podest = await A.aus(`[...document.querySelectorAll('#podest .platz .name')].map(e=>e.textContent).join(',')`);
  pruefe('Podest zeigt beide', podest.includes('Anna') && podest.includes('Bert'), podest);
  pruefe('Tabelle mit beiden', (await B.aus(`document.querySelectorAll('#ehrung-tabelle tr').length`)) === 2);
  // Eigene Strecke als Tabelle: Posten mit Punkten, die zusammen die Summe ergeben
  const streckeOk = await A.aus(`(() => { const z = [...document.querySelectorAll('#ehrung-strecke tr')]; if (z.length < 4) return false;
    const w = z.map(tr => +tr.lastChild.textContent); return w.slice(0, -1).reduce((a, b) => a + b, 0) === w.at(-1); })()`);
  pruefe('Eigene Strecke mit Punkten, Summe stimmt', streckeOk);
  pruefe('Nur der Oberjäger hat „Nächste Runde“', await A.sichtbar('ehrung-weiter') && !(await B.sichtbar('ehrung-weiter')));
  await warte(1600);
  await A.bild('a-ehrung.png');
  await B.bild('b-ehrung.png');
  const ueber = await B.aus(`document.documentElement.scrollWidth>innerWidth||document.getElementById('ehrung').scrollWidth>document.getElementById('ehrung').clientWidth`);
  pruefe('Telefon: kein Querüberlauf in der Siegerehrung', !ueber);

  // Zweite Runde direkt aus der Siegerehrung; Bert steigt per Handy-Knopf mitten in der Runde aus
  await A.klick('ehrung-weiter');
  pruefe('Nächste Runde holt beide aus der Siegerehrung', await A.warteBis(`__moorjagd.G.phase==='jagd'`, 7000) && await B.warteBis(`__moorjagd.G.phase==='jagd'`, 2000));
  await B.klick('pause-knopf');
  pruefe('B: Rast-Tafel über den Knopf', await B.warteBis(`!document.getElementById('rast').hidden`, 1500));
  await B.bild('b-rast.png');
  await B.klick('rast-verlassen');
  pruefe('B: Runde verlassen führt in die Lobby', await B.warteBis(`!document.getElementById('lobby').hidden && !__moorjagd.MP.raum`, 3000));
  pruefe('A: jagt allein weiter, Bert sofort weg', await A.warteBis(`__moorjagd.G.phase==='jagd' && __moorjagd.MP.raum.jaeger.length===1 && document.querySelectorAll('#rangliste li').length===1`, 3000));
  // Anna (Oberjägerin) verlässt per Esc
  await A.taste(ESC);
  await A.klick('rast-verlassen');
  pruefe('A: Oberjägerin verlässt, Raum aufgelöst', await A.warteBis(`!document.getElementById('lobby').hidden`, 3000) && await B.warteBis(`document.querySelectorAll('#raeume li').length===0`, 3000));
  await B.klick('lobby-zurueck');
  pruefe('B: Hauptmenü', await B.warteBis(`!document.getElementById('menue').hidden`));
  // Alleinspiel funktioniert danach wie vorher
  await B.klick('los');
  await warte(1500);
  pruefe('B: Alleinspiel startet mit eigenen Hühnern', (await B.aus(`__moorjagd.G.modus==='solo' && __moorjagd.G.phase==='jagd' && __moorjagd.huehner.some(h=>!h.mp)`)));
} catch (f) {
  pruefe('Ablauf ohne Ausnahme', false, f.message);
}
console.log(ergebnisse.join('\n'));
console.log('Browser-Fehler:', fehler.length ? fehler.join(' | ') : 'keine');
console.log('Bilder in', hier);
await ruf('session.end').catch(() => {});
aufraeumen();
process.exit(0);
