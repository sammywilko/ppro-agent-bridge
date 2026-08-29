# ppro-agent-bridge

A tiny resident **UXP panel for Premiere Pro (Beta 27 / 26.x)** plus a Python CLI, so a terminal agent
(Claude Code) can drive Premiere: import an FCP7 XML assembly, sweep the project for offline media,
dump any sequence (tracks → clips → in/out/start/end + media paths + markers), read the editor's
current selection, and — when the panel's *allow eval* box is ticked — run arbitrary UXP code.

Why a panel and not an MCP server: Premiere has **no external invocation path** (no headless mode,
no File > Scripts, AppleScript exposes nothing, ExtendScript/CEP is being removed by Adobe in the
27.0 release window). A UXP panel cannot listen on a socket either, so the bridge is **file-based**:
the panel polls a folder. See `~/MAC_SHARE/projects/bay-tree-trailer-finish/PREMIERE-BRIDGE-RESEARCH-2026-08-29.md`.

## Design rule

**Assembly goes in through XML import; UXP is read-mostly.** Premiere 27.0 beta has a staff-acknowledged
crash regression under rapid UXP timeline mutations. The only mutations in this panel are
bin creation and marker batches, each inside a single `executeTransaction`. Keep it that way.

## Layout

```
plugin/            the UXP plugin (manifest.json, index.html, index.js, icons/)
client/ppro_bridge.py   CLI + Python API (send / call / verify_xml)
scripts/check_api_names.py   every API name in index.js must exist in Adobe's typings
tests/             protocol tests against a fake panel (no Premiere needed)
vendor/package     @adobe/premierepro typings (26.5.0-beta.73) — the source of truth for names
```

## Install (one-time, needs you at the Mac)

1. Open **Adobe UXP Developer Tools** (already installed in `/Applications/Adobe UXP Developer Tools/`).
   First run asks to *Enable Developer Mode* — that needs an admin password.
2. Launch **Premiere Pro (Beta)** and open any project.
3. In UDT: **Add Plugin** → pick `plugin/manifest.json` → **Load**. Tick *Watch* if you want live reload.
4. In Premiere the panel appears under **Window ▸ UXP Plugins ▸ Agent Bridge**. Dock it anywhere;
   it must stay open (it is the receiver). Status turns green: `LIVE · <project> · <sequence>`.

UDT loads are per-session. For a permanent install, package it in UDT (**Package**) to a `.ccx`
and double-click it, or install with Adobe's UPIA agent:
`"/Library/Application Support/Adobe/Adobe Desktop Common/RemoteComponents/UPI/UnifiedPluginInstallerAgent/UnifiedPluginInstallerAgent.app/Contents/MacOS/UnifiedPluginInstallerAgent" --install ppro-agent-bridge.ccx`

## Use (from any terminal)

```
ppro-bridge status                     # heartbeat: is the panel alive, which project/sequence
ppro-bridge project                    # sequences in the open project
ppro-bridge import CUT.xml --bin SEQUENCES
ppro-bridge verify-xml CUT.xml         # xml vs live sequence: frames, order, offline, missing files → PASS/FAIL
ppro-bridge sweep-offline              # every clip in the project with its media path + offline flag
ppro-bridge dump-sequence --name "BAY TREE ROUGH CUT V4 · 2026-08-07" --out dump.json
ppro-bridge read-selection             # what the editor has selected right now
ppro-bridge set-active "NAME" --open
ppro-bridge save
ppro-bridge eval 'const p = await ppro.Project.getActiveProject(); return p.name;'
```

`~/bin/ppro-bridge` is a symlink to `client/ppro_bridge.py`. Add `--json` for raw output.
Exit codes: 0 ok · 1 command error · 2 panel unreachable · 3 verify FAILED.

`verify-xml` is strict on video: clip count per track, every start/end/in/out frame (xml in/out converted
from the clip's own timebase), extra populated tracks in Premiere, offline media, files missing on disk,
Premiere unable to report a clip's media state, and relinks to a different file (`--allow-relink` downgrades
that to a warning). Audio is reported as warnings only — xmeml splits stereo across two tracks and Premiere
merges them. Two sequences with the same name → use `--guid` (printed by `import` and `project`).
A Premiere result of `ok:false` (import/save/transaction) is an error, exit 1 — scripts can chain safely.

## Protocol

```
client  →  ~/.ppro-bridge/cmd-<id>.json        {"id","op","args"}      (written .tmp then renamed)
panel   →  ~/.ppro-bridge/cmd-<id>.running     (claimed; a crash never re-runs a command)
panel   →  ~/.ppro-bridge/result-<id>.json     {"id","op","ok","result"|"error","ms","ts"}
panel   →  ~/.ppro-bridge/heartbeat.json       every 2 s
```

Ops: `ping` `project` `bins` `sweepOffline` `dumpSequence{name|guid,videoOnly}` `readSelection{name}`
`importFiles{paths,bin,suppressUI,asNumberedStills}` `setActiveSequence{name,open}` `save`
`addMarkers{name,markers[]}` `eval{code,args}`.

Frames are derived from Premiere ticks: `frames = ticks / sequence.getTimebase()` (254016000000 ticks/s).

## Checks

```
node --check plugin/index.js
python3 scripts/check_api_names.py          # CLEAN = every name exists in Adobe's d.ts
python3 -m unittest discover -s tests -v    # 11 protocol tests against the fake panel
```

## Measured on Premiere Pro (Beta) 27.0.0 — 2026-08-29, first live day

- **`Project.importFiles([...xml])` does NOT import FCP7 XML** — it runs the media importer, which
  fails with "File Import Failure". `Project.open(xml)` also refuses ("Failed to convert project file").
- **OTIO does import through `importFiles`**, but only in a minimal hand-built shape:
  `Timeline → Stack → Track → Clip(ExternalReference)` with **plain absolute paths** in `target_url`.
  The otio-fcp-adapter's output (`file:///` URLs, metadata blobs) is rejected as "unsupported
  compression type". `scripts/xmeml_to_otio.py` builds the accepted shape from an xmeml; Premiere names
  the sequence after the top **stack**, so the stack is named too. Overlapping clipitems on one xmeml
  track are spilled onto extra tracks (A2 → A2/A3). Per-clip audio levels do not survive OTIO.
- **Anything under `~/Documents` fails in this beta via the API** — imports say "unsupported
  compression type", `changeMediaFilePath` returns false, background auto-save fails — while the
  byte-identical file anywhere else (`~/Movies`, `/private/tmp`, 220-char paths) imports online.
  TCC shows Documents *granted*; mechanism unknown; rule stands after six paired tests.
  **Keep the Premiere project, delivery media and OTIO/XML outside `~/Documents`.**
- A "File Import Failure" dialog blocks the panel until dismissed; commands now race a 90 s deadline
  (`COMMAND_TIMEOUT_MS`). `importFiles` must not be passed `undefined` in the optional slots
  ("Illegal Parameter type").
- `Project.getSequence(guid)` returned a non-Sequence (Promise?) despite the typings — look sequences up
  via `getSequences()` instead.
- `VideoTrack.getTrackItems` is typed synchronous; the panel `await`s it either way.
- `ppro.Application.version` is undefined at runtime; ping omits it.

### Working recipe (proven end-to-end, 91/91 clips online, verify-xml PASS)

```
# media + spine live outside ~/Documents, e.g. ~/Movies/<FILM>-EDIT/{MEDIA-vN,AUDIO}
.venv/bin/python scripts/xmeml_to_otio.py ~/Movies/<FILM>-EDIT/CUT.xml      # xml paths already rewritten
ppro-bridge eval 'await ppro.Project.createProject("/Users/…/Movies/<FILM>-EDIT/<FILM>-EDIT.prproj")'
ppro-bridge import ~/Movies/<FILM>-EDIT/CUT.otio                            # prints the new sequence guid
ppro-bridge verify-xml ~/Movies/<FILM>-EDIT/CUT.xml --guid <guid>           # PASS / FAIL, exit 3 on FAIL
ppro-bridge eval '…setMute(true) on alternate tracks…'                      # xmeml "enabled=FALSE" does not carry
```
