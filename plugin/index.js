/* Agent Bridge — a resident Premiere Pro UXP panel that lets a terminal agent drive Premiere.
 *
 * Protocol (file-based, because UXP cannot listen on a socket or be invoked from outside):
 *   client writes   <bridgeDir>/cmd-<id>.json      {"id","op","args"}
 *   panel renames → <bridgeDir>/cmd-<id>.running   (claimed; a crash never re-runs it)
 *   panel writes    <bridgeDir>/result-<id>.json   {"id","op","ok","result"|"error","ms","ts"}
 *   panel writes    <bridgeDir>/heartbeat.json     every HEARTBEAT_MS while alive
 *
 * Design rule (Premiere 27.0 beta UXP regression, Aug 2026): this panel is READ-MOSTLY.
 * Assembly is done by importing an FCP7 XML; the few mutations here (bins, markers) are
 * batched inside one executeTransaction. Do not add rapid per-item timeline mutations.
 */

/** @type {import('@adobe/premierepro').premierepro} */
const ppro = require("premierepro");
const uxp = require("uxp");
const fs = require("fs");
const os = require("os");

const VERSION = "0.1.0";
const POLL_MS = 400;
const HEARTBEAT_MS = 2000;
const TICKS_PER_SECOND = 254016000000; // Premiere's tick rate

// ---------------------------------------------------------------- UI plumbing
const $ = (id) => document.getElementById(id);
const logEl = $("log");
function log(msg, cls) {
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = `${new Date().toISOString().slice(11, 19)}  ${msg}`;
  logEl.appendChild(line);
  while (logEl.childNodes.length > 300) logEl.removeChild(logEl.firstChild);
  logEl.scrollTop = logEl.scrollHeight;
}
function setStatus(text, cls) {
  const s = $("status");
  s.textContent = text;
  s.className = cls || "";
}

// ---------------------------------------------------------------- bridge dir
function defaultBridgeDir() {
  try {
    const home = os.homedir();
    if (home) return `${home}/.ppro-bridge`;
  } catch (e) { /* fall through */ }
  return "/Users/samuelwilkinson/.ppro-bridge";
}
let bridgeDir = (() => {
  try { return window.localStorage.getItem("bridgeDir") || defaultBridgeDir(); } catch (e) { return defaultBridgeDir(); }
})();

async function ensureDir(dir) {
  try { await fs.lstat(dir); } catch (e) { await fs.mkdir(dir); }
}
async function readText(p) { return fs.readFile(p, { encoding: "utf-8" }); }
async function writeAtomic(p, text) {
  const tmp = `${p}.tmp`;
  await fs.writeFile(tmp, text, { encoding: "utf-8" });
  try {
    await fs.rename(tmp, p);
  } catch (e) {
    // UXP's fs doc does not promise rename-over-existing; fall back to unlink + rename.
    try { await fs.unlink(p); } catch (e2) { /* ignore */ }
    await fs.rename(tmp, p);
  }
}
async function listDir(dir) {
  return fs.readdir(dir);
}

// ---------------------------------------------------------------- helpers
function tt(tick) {
  // TickTime → {seconds, ticks}
  if (!tick) return null;
  return { seconds: tick.seconds, ticks: tick.ticks };
}
function framesFromTicks(ticksStr, ticksPerFrame) {
  if (!ticksStr || !ticksPerFrame) return null;
  return Math.round(Number(ticksStr) / ticksPerFrame);
}
async function activeProject() {
  const p = await ppro.Project.getActiveProject();
  if (!p) throw new Error("no active project");
  return p;
}
async function findSequence(project, args = {}) {
  const seqs = await project.getSequences();
  if (args.guid) {
    const s = seqs.find((q) => String(q.guid) === String(args.guid));
    if (!s) throw new Error(`no sequence with guid ${args.guid}`);
    return s;
  }
  if (args.name) {
    const matches = seqs.filter((q) => q.name === args.name);
    if (!matches.length) throw new Error(`no sequence named "${args.name}" (have: ${seqs.map((q) => q.name).join(" | ")})`);
    if (matches.length > 1) throw new Error(`${matches.length} sequences named "${args.name}" — pass guid instead: ${matches.map((q) => String(q.guid)).join(", ")}`);
    return matches[0];
  }
  const a = await project.getActiveSequence();
  if (!a) throw new Error("no active sequence and no name/guid given");
  return a;
}
async function sequenceTiming(seq) {
  const timebase = await seq.getTimebase(); // ticks per frame, as a string
  const ticksPerFrame = Number(timebase);
  const fps = ticksPerFrame ? TICKS_PER_SECOND / ticksPerFrame : null;
  const end = await seq.getEndTime();
  return { timebase, ticksPerFrame, fps, endSeconds: end ? end.seconds : null, endFrames: end ? framesFromTicks(end.ticks, ticksPerFrame) : null };
}
async function clipInfo(projectItem) {
  // ProjectItem → {name, mediaPath, offline, isSequence, error}. offline is true/false, or "unknown" when
  // Premiere would not answer — never silently null, because a null would read as "online" downstream.
  const out = { name: projectItem ? projectItem.name : null, mediaPath: null, offline: null, isSequence: null, error: null };
  if (!projectItem) { out.error = "no project item"; out.offline = "unknown"; return out; }
  let clip = null;
  try { clip = ppro.ClipProjectItem.cast(projectItem); } catch (e) { clip = null; }
  if (!clip) return out; // a bin or non-clip: nothing to report
  try { out.isSequence = await clip.isSequence(); } catch (e) { out.isSequence = null; }
  if (out.isSequence) return out;
  try { out.mediaPath = await clip.getMediaFilePath(); } catch (e) { out.error = `getMediaFilePath: ${e.message || e}`; }
  try { out.offline = await clip.isOffline(); } catch (e) { out.offline = "unknown"; out.error = (out.error ? out.error + "; " : "") + `isOffline: ${e.message || e}`; }
  return out;
}
async function trackItemRecord(item, ticksPerFrame, trackIndex, kind) {
  // The five timing getters and the name are load-bearing: let them throw. speed/selected are informational.
  const [name, start, end, inp, outp, dur, disabled] = await Promise.all([
    item.getName(), item.getStartTime(), item.getEndTime(), item.getInPoint(), item.getOutPoint(),
    item.getDuration(), item.isDisabled(),
  ]);
  const speed = await item.getSpeed().catch(() => null);
  const selected = await item.getIsSelected().catch(() => null);
  let pi = null, piError = null;
  try { pi = await item.getProjectItem(); } catch (e) { piError = `getProjectItem: ${e.message || e}`; }
  const clip = await clipInfo(pi);
  return {
    kind, track: trackIndex, name,
    start: tt(start), end: tt(end), in: tt(inp), out: tt(outp), duration: tt(dur),
    startFrame: framesFromTicks(start && start.ticks, ticksPerFrame),
    endFrame: framesFromTicks(end && end.ticks, ticksPerFrame),
    inFrame: framesFromTicks(inp && inp.ticks, ticksPerFrame),
    outFrame: framesFromTicks(outp && outp.ticks, ticksPerFrame),
    disabled, speed, selected,
    projectItem: clip.name, mediaPath: clip.mediaPath, offline: clip.offline, isSequence: clip.isSequence,
    mediaError: piError || clip.error,
  };
}
async function walkBin(folder, path, acc, opts) {
  const items = await folder.getItems();
  for (const it of items) {
    const here = [...path, it.name];
    if (it.type === ppro.ProjectItem.TYPE_BIN) {
      const sub = ppro.FolderItem.cast(it);
      acc.bins.push({ path: here.join("/"), name: it.name });
      await walkBin(sub, here, acc, opts);
    } else {
      const clip = await clipInfo(it);
      acc.items.push({ path: here.join("/"), name: it.name, type: it.type, ...clip });
    }
  }
  return acc;
}
async function readMarkers(seq, ticksPerFrame) {
  // The typings say the marker getters are synchronous; Adobe's own sample awaits them. Awaiting is harmless either way.
  try {
    const m = await ppro.Markers.getMarkers(seq);
    const list = await m.getMarkers();
    const out = [];
    for (const mk of list) {
      const start = await mk.getStart();
      out.push({
        name: await mk.getName(), comments: await mk.getComments(), type: await mk.getType(), colorIndex: await mk.getColorIndex(),
        start: tt(start), duration: tt(await mk.getDuration()),
        startFrame: framesFromTicks(start && start.ticks, ticksPerFrame),
        guid: String(mk.guid),
      });
    }
    return { markers: out, markersError: null };
  } catch (e) {
    return { markers: [], markersError: String(e && e.message ? e.message : e) };
  }
}
function runTransaction(project, label, buildActions) {
  // buildActions(compoundAction) adds Actions; returns boolean success from Premiere.
  let error = null;
  const ok = project.executeTransaction((ca) => {
    try { buildActions(ca); } catch (e) { error = e; }
  }, label);
  if (error) throw error;
  return ok;
}

// ---------------------------------------------------------------- command handlers
const handlers = {
  async ping() {
    let appVersion = null;
    try { appVersion = await ppro.Application.version; } catch (e) { /* ignore */ }
    let project = null, active = null;
    try {
      const p = await ppro.Project.getActiveProject();
      if (p) { project = { name: p.name, path: p.path, guid: String(p.guid) }; const a = await p.getActiveSequence(); if (a) active = { name: a.name, guid: String(a.guid) }; }
    } catch (e) { /* ignore */ }
    return { pluginVersion: VERSION, appVersion, bridgeDir, project, activeSequence: active, evalAllowed: $("allowEval").checked };
  },

  async project() {
    const p = await activeProject();
    const seqs = await p.getSequences();
    const a = await p.getActiveSequence();
    return {
      name: p.name, path: p.path, guid: String(p.guid),
      activeSequence: a ? { name: a.name, guid: String(a.guid) } : null,
      sequences: seqs.map((s) => ({ name: s.name, guid: String(s.guid) })),
    };
  },

  async bins() {
    const p = await activeProject();
    const root = await p.getRootItem();
    return walkBin(root, [], { bins: [], items: [] }, {});
  },

  async sweepOffline() {
    const p = await activeProject();
    const root = await p.getRootItem();
    const acc = await walkBin(root, [], { bins: [], items: [] }, {});
    const clips = acc.items.filter((i) => i.mediaPath !== null || i.offline !== null);
    const offline = clips.filter((i) => i.offline === true);
    const unknown = clips.filter((i) => i.offline === "unknown" || i.error);
    return {
      project: p.name, totalItems: acc.items.length, clips: clips.length, offline: offline.length, unknown: unknown.length,
      offlineItems: offline, unknownItems: unknown, items: clips,
    };
  },

  async dumpSequence(args = {}) {
    const p = await activeProject();
    const seq = await findSequence(p, args);
    const timing = await sequenceTiming(seq);
    const CLIP = ppro.Constants.TrackItemType.CLIP;
    const video = [], audio = [];
    const vCount = await seq.getVideoTrackCount();
    for (let i = 0; i < vCount; i++) {
      const t = await seq.getVideoTrack(i);
      const items = await t.getTrackItems(CLIP, false);
      const recs = [];
      for (const it of items) recs.push(await trackItemRecord(it, timing.ticksPerFrame, i, "video"));
      video.push({ index: i, name: t.name, id: t.id, muted: await t.isMuted().catch(() => null), items: recs });
    }
    if (!args.videoOnly) {
      const aCount = await seq.getAudioTrackCount();
      for (let i = 0; i < aCount; i++) {
        const t = await seq.getAudioTrack(i);
        const items = await t.getTrackItems(CLIP, false);
        const recs = [];
        for (const it of items) recs.push(await trackItemRecord(it, timing.ticksPerFrame, i, "audio"));
        audio.push({ index: i, name: t.name, id: t.id, muted: await t.isMuted().catch(() => null), items: recs });
      }
    }
    const { markers, markersError } = await readMarkers(seq, timing.ticksPerFrame);
    return { sequence: { name: seq.name, guid: String(seq.guid) }, timing, video, audio, markers, markersError };
  },

  async readSelection(args = {}) {
    const p = await activeProject();
    const seq = await findSequence(p, args);
    const timing = await sequenceTiming(seq);
    const sel = await seq.getSelection();
    const items = await sel.getTrackItems();
    const recs = [];
    for (const it of items) {
      const trackIndex = await it.getTrackIndex().catch(() => null);
      recs.push(await trackItemRecord(it, timing.ticksPerFrame, trackIndex, "selected"));
    }
    return { sequence: { name: seq.name, guid: String(seq.guid) }, timing, count: recs.length, items: recs };
  },

  async importFiles(args = {}) {
    const paths = Array.isArray(args.paths) ? args.paths : [args.path];
    if (!paths.length || !paths[0]) throw new Error("importFiles: args.paths required");
    const p = await activeProject();
    const before = (await p.getSequences()).map((s) => String(s.guid));
    let targetBin;
    if (args.bin) {
      const root = await p.getRootItem();
      const findBin = async () => (await root.getItems()).find((i) => i.type === ppro.ProjectItem.TYPE_BIN && i.name === args.bin);
      let bin = await findBin();
      if (!bin) {
        runTransaction(p, `Agent Bridge: create bin ${args.bin}`, (ca) => ca.addAction(root.createBinAction(args.bin, false)));
        bin = await findBin();
      }
      if (!bin) throw new Error(`importFiles: bin "${args.bin}" could not be found or created — refusing to import into root by accident`);
      targetBin = bin;
    }
    const suppressUI = args.suppressUI !== false;
    const ok = await p.importFiles(paths, suppressUI, targetBin, !!args.asNumberedStills);
    // Import completion may be event-driven (Constants.OperationCompleteEvent.IMPORT_MEDIA_COMPLETE); when an
    // xml/otio is imported, give the sequence a few seconds to appear before diffing.
    const expectsSequence = paths.some((x) => /\.(xml|otio)$/i.test(x));
    let newSequences = [];
    for (let attempt = 0; attempt < (expectsSequence ? 15 : 1); attempt++) {
      const after = await p.getSequences();
      newSequences = after.filter((s) => !before.includes(String(s.guid))).map((s) => ({ name: s.name, guid: String(s.guid) }));
      if (newSequences.length || !expectsSequence) break;
      await new Promise((r) => setTimeout(r, 400));
    }
    return { ok, imported: paths, targetBin: args.bin || null, newSequences, expectedSequence: expectsSequence };
  },

  async setActiveSequence(args = {}) {
    const p = await activeProject();
    const seq = await findSequence(p, args);
    const ok = await p.setActiveSequence(seq);
    let opened = null;
    if (args.open) opened = await p.openSequence(seq).catch((e) => String(e));
    return { ok, opened, sequence: { name: seq.name, guid: String(seq.guid) } };
  },

  async save() {
    const p = await activeProject();
    return { ok: await p.save(), path: p.path };
  },

  async addMarkers(args = {}) {
    // args.markers: [{name, seconds, durationSeconds?, comments?, type?, colorIndex?}]
    // Validated up front so a bad entry cannot leave a partial batch committed; one transaction to add,
    // a second (only if any colorIndex was asked for) to colour — Marker objects only exist after commit.
    const p = await activeProject();
    const seq = await findSequence(p, args);
    const list = Array.isArray(args.markers) ? args.markers : [];
    if (!list.length) throw new Error("addMarkers: args.markers required");
    list.forEach((m, i) => {
      if (typeof m !== "object" || m === null) throw new Error(`addMarkers: markers[${i}] is not an object`);
      if (!Number.isFinite(Number(m.seconds))) throw new Error(`addMarkers: markers[${i}].seconds is not a number`);
      if (m.colorIndex !== undefined && !Number.isInteger(Number(m.colorIndex))) throw new Error(`addMarkers: markers[${i}].colorIndex is not an integer`);
    });
    const markers = await ppro.Markers.getMarkers(seq);
    const ok = runTransaction(p, `Agent Bridge: add ${list.length} markers`, (ca) => {
      for (const m of list) {
        const start = ppro.TickTime.createWithSeconds(Number(m.seconds));
        const dur = ppro.TickTime.createWithSeconds(Number(m.durationSeconds || 0));
        ca.addAction(markers.createAddMarkerAction(String(m.name || ""), m.type || "Comment", start, dur, m.comments || ""));
      }
    });
    let coloured = 0, colourErrors = [];
    if (ok && list.some((m) => m.colorIndex !== undefined)) {
      const timing = await sequenceTiming(seq);
      const { markers: live } = await readMarkers(seq, timing.ticksPerFrame);
      const wanted = list.filter((m) => m.colorIndex !== undefined);
      const targets = [];
      for (const m of wanted) {
        const hit = live.find((lm) => lm.name === String(m.name || "") && Math.abs(lm.start.seconds - Number(m.seconds)) < 0.001);
        if (!hit) { colourErrors.push(`no live marker matched "${m.name}" @ ${m.seconds}s`); continue; }
        targets.push({ guid: hit.guid, colorIndex: Number(m.colorIndex) });
      }
      if (targets.length) {
        const all = await (await ppro.Markers.getMarkers(seq)).getMarkers();
        runTransaction(p, `Agent Bridge: colour ${targets.length} markers`, (ca) => {
          for (const t of targets) {
            const mk = all.find((x) => String(x.guid) === t.guid);
            if (mk) { ca.addAction(mk.createSetColorByIndexAction(t.colorIndex)); coloured += 1; }
          }
        });
      }
    }
    return { ok, added: list.length, coloured, colourErrors };
  },

  async eval(args = {}) {
    if (!$("allowEval").checked) throw new Error("eval disabled in the panel (tick 'allow eval')");
    if (typeof args.code !== "string") throw new Error("eval: args.code (string) required");
    const helpers = { activeProject, findSequence, sequenceTiming, clipInfo, trackItemRecord, walkBin, runTransaction, tt, framesFromTicks, TICKS_PER_SECOND };
    // eslint-disable-next-line no-new-func
    const fn = new Function("ppro", "uxp", "fs", "args", "helpers", `return (async () => {\n${args.code}\n})();`);
    return fn(ppro, uxp, fs, args.args || {}, helpers);
  },
};

// ---------------------------------------------------------------- poll loop
let paused = false;
let busy = false;
let processed = 0;

async function processOne(fileName) {
  const id = fileName.slice("cmd-".length, -".json".length);
  const cmdPath = `${bridgeDir}/${fileName}`;
  const runningPath = `${bridgeDir}/cmd-${id}.running`;
  const resultPath = `${bridgeDir}/result-${id}.json`;
  let cmd;
  try {
    await fs.rename(cmdPath, runningPath); // claim it; a crash here never re-runs the command
  } catch (e) {
    log(`skip ${fileName}: ${e.message || e}`, "warn"); // the client withdrew it (or another rename won) — nothing to answer
    return;
  }
  try {
    cmd = JSON.parse(await readText(runningPath));
    if (!cmd || typeof cmd !== "object") throw new Error("command body is not an object");
  } catch (e) {
    // Claimed but unreadable: answer with an error so the client does not wait out its timeout.
    const payload = { id, op: null, ok: false, error: `unreadable command: ${e.message || e}`, ms: 0, ts: new Date().toISOString() };
    try { await writeAtomic(resultPath, JSON.stringify(payload, null, 1)); } catch (e2) { /* ignore */ }
    try { await fs.unlink(runningPath); } catch (e2) { /* ignore */ }
    log(`✗ ${fileName}: unreadable`, "warn");
    return;
  }
  const t0 = Date.now();
  const op = cmd.op;
  let payload;
  try {
    const h = handlers[op];
    if (!h) throw new Error(`unknown op "${op}" (have: ${Object.keys(handlers).join(", ")})`);
    const result = await h(cmd.args || {});
    payload = { id, op, ok: true, result, ms: Date.now() - t0, ts: new Date().toISOString() };
    log(`✓ ${op} (${payload.ms} ms)`);
  } catch (e) {
    payload = { id, op, ok: false, error: String(e && e.message ? e.message : e), stack: e && e.stack ? String(e.stack) : null, ms: Date.now() - t0, ts: new Date().toISOString() };
    log(`✗ ${op}: ${payload.error}`, "warn");
  }
  try {
    await writeAtomic(resultPath, JSON.stringify(payload, null, 1));
  } catch (e) {
    log(`could not write result for ${id}: ${e.message || e}`, "warn");
  }
  try { await fs.unlink(runningPath); } catch (e) { /* ignore */ }
  processed += 1;
}

async function poll() {
  if (paused || busy) return;
  busy = true;
  try {
    const names = (await listDir(bridgeDir)).filter((n) => n.startsWith("cmd-") && n.endsWith(".json")).sort();
    for (const n of names) await processOne(n);
  } catch (e) {
    setStatus(`poll error: ${e.message || e}`, "err");
  } finally {
    busy = false;
  }
}

async function heartbeat() {
  try {
    let project = null, active = null;
    try {
      const p = await ppro.Project.getActiveProject();
      if (p) { project = p.name; const a = await p.getActiveSequence(); if (a) active = a.name; }
    } catch (e) { /* ignore */ }
    await writeAtomic(`${bridgeDir}/heartbeat.json`, JSON.stringify({ ts: new Date().toISOString(), pluginVersion: VERSION, paused, processed, project, activeSequence: active }));
    setStatus(`${paused ? "PAUSED" : "LIVE"} · ${project || "no project"}${active ? " · " + active : ""} · ${processed} cmds`, paused ? "" : "live");
  } catch (e) {
    setStatus(`heartbeat failed: ${e.message || e}`, "err");
  }
}

async function start() {
  $("version").textContent = `v${VERSION}`;
  $("dir").value = bridgeDir;
  try {
    await ensureDir(bridgeDir);
    log(`bridge dir ${bridgeDir}`);
  } catch (e) {
    setStatus(`cannot create ${bridgeDir}: ${e.message || e}`, "err");
    log(String(e), "warn");
  }
  setInterval(poll, POLL_MS);
  setInterval(heartbeat, HEARTBEAT_MS);
  heartbeat();
}

$("toggle").addEventListener("click", () => {
  paused = !paused;
  $("toggle").textContent = paused ? "Resume" : "Pause";
  log(paused ? "paused" : "resumed");
  heartbeat();
});
$("clearBtn").addEventListener("click", () => { logEl.textContent = ""; });
$("pingBtn").addEventListener("click", async () => {
  try { const r = await handlers.ping(); log(`self-test ok: app ${r.appVersion} · project ${r.project ? r.project.name : "none"}`); }
  catch (e) { log(`self-test failed: ${e.message || e}`, "warn"); }
});
$("dir").addEventListener("change", async (ev) => {
  bridgeDir = ev.target.value.trim();
  try { window.localStorage.setItem("bridgeDir", bridgeDir); } catch (e) { /* ignore */ }
  try { await ensureDir(bridgeDir); log(`bridge dir → ${bridgeDir}`); } catch (e) { log(`bad dir: ${e.message || e}`, "warn"); }
});

start();
