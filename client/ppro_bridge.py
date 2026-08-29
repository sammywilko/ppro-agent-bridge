#!/usr/bin/env python3
"""ppro_bridge — drive Premiere Pro from a terminal via the Agent Bridge UXP panel.

The panel polls  ~/.ppro-bridge/cmd-<id>.json  and answers  result-<id>.json.
This client writes the command atomically, waits for the answer, and prints it.

  ppro_bridge.py status                       # is the panel alive? (heartbeat only, no command)
  ppro_bridge.py ping
  ppro_bridge.py project
  ppro_bridge.py bins
  ppro_bridge.py sweep-offline
  ppro_bridge.py dump-sequence [--name N | --guid G] [--out FILE] [--video-only]
  ppro_bridge.py read-selection [--name N | --guid G]
  ppro_bridge.py import PATH [PATH...] [--bin NAME] [--show-ui]      # prints the guid of any new sequence
  ppro_bridge.py set-active (--name N | --guid G) [--open]
  ppro_bridge.py save
  ppro_bridge.py add-markers (--name N | --guid G) --markers-json FILE
  ppro_bridge.py eval 'JS' | eval --file script.js
  ppro_bridge.py verify-xml SEQUENCE.xml [--name N | --guid G] [--allow-relink]
                                              # compare an FCP7 xmeml cut against the live sequence

Every command accepts --json (raw result) and --timeout SECONDS. Set PPRO_BRIDGE_DIR to override the dir.
Exit codes: 0 ok · 1 command error · 2 panel not reachable · 3 verify FAILED.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import xml.dom.minidom as md
from pathlib import Path

BRIDGE_DIR = Path(os.environ.get("PPRO_BRIDGE_DIR", Path.home() / ".ppro-bridge"))
HEARTBEAT_STALE_S = 8.0
STALE_FILE_S = 600.0  # orphaned result-/tmp/withdrawn files older than this are swept


class BridgeError(Exception):
    exit_code = 1


class PanelUnreachable(BridgeError):
    exit_code = 2


# ---------------------------------------------------------------- transport
def heartbeat() -> dict | None:
    p = BRIDGE_DIR / "heartbeat.json"
    if not p.exists():
        return None
    try:
        hb = json.loads(p.read_text())
    except Exception:
        return None
    hb["age_s"] = round(time.time() - p.stat().st_mtime, 1)
    hb["alive"] = hb["age_s"] < HEARTBEAT_STALE_S and not hb.get("paused")
    return hb


def sweep_stale() -> int:
    """Remove orphaned protocol files (results nobody collected, tmp/withdrawn leftovers). Returns count."""
    n = 0
    now = time.time()
    for p in BRIDGE_DIR.glob("*"):
        if p.name == "heartbeat.json" or not p.is_file():
            continue
        if not (p.name.startswith("result-") or p.suffix in (".tmp", ".withdrawn")):
            continue
        try:
            if now - p.stat().st_mtime > STALE_FILE_S:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def send(op: str, args: dict | None = None, timeout: float = 60.0) -> dict:
    """Write a command, wait for its result. Returns the full result envelope."""
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    sweep_stale()
    hb = heartbeat()
    if hb is None or not hb["alive"]:
        why = "no heartbeat" if hb is None else ("panel PAUSED" if hb.get("paused") else f"heartbeat {hb['age_s']}s old")
        raise PanelUnreachable(f"Agent Bridge panel not reachable in {BRIDGE_DIR} ({why}). Is Premiere open with the panel loaded?")
    cid = f"{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
    cmd = BRIDGE_DIR / f"cmd-{cid}.json"
    tmp = BRIDGE_DIR / f"cmd-{cid}.json.tmp"
    result = BRIDGE_DIR / f"result-{cid}.json"
    tmp.write_text(json.dumps({"id": cid, "op": op, "args": args or {}, "sentAt": time.time()}))
    os.replace(tmp, cmd)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if result.exists():
            env = None
            for _ in range(20):  # the panel renames a .tmp into place, so a partial read is unlikely, but be safe
                try:
                    env = json.loads(result.read_text())
                    break
                except (json.JSONDecodeError, OSError):
                    time.sleep(0.05)
            if env is None:
                raise BridgeError(f"unreadable result file {result}")
            try:
                result.unlink()
            except OSError:
                pass
            return env
        time.sleep(0.15)
    # Timed out. Withdraw atomically: if OUR rename wins, the panel never claimed it and it never ran.
    withdrawn = BRIDGE_DIR / f"cmd-{cid}.withdrawn"
    try:
        os.rename(cmd, withdrawn)
        try:
            withdrawn.unlink()
        except OSError:
            pass
        raise PanelUnreachable(f"panel never picked up {op} within {timeout}s (command withdrawn — it did NOT run)")
    except FileNotFoundError:
        pass
    raise BridgeError(f"{op} was CLAIMED by the panel but returned no result within {timeout}s — it may still be running in Premiere; do not blindly retry (result will land as {result.name})")


def call(op: str, args: dict | None = None, timeout: float = 60.0):
    env = send(op, args, timeout)
    if not env.get("ok"):
        raise BridgeError(f"{op} failed in Premiere: {env.get('error')}")
    r = env["result"]
    if isinstance(r, dict) and r.get("ok") is False:
        raise BridgeError(f"{op}: Premiere returned ok=false ({json.dumps({k: v for k, v in r.items() if k != 'ok'})[:300]})")
    return r


# ---------------------------------------------------------------- xmeml parsing
def _child(e, tag):
    for c in e.childNodes:
        if c.nodeName == tag:
            return c
    return None


def _txt(e, tag, default=None):
    c = _child(e, tag)
    return c.firstChild.data.strip() if c is not None and c.firstChild else default


def parse_xmeml(xml_path: Path) -> dict:
    doc = md.parse(str(xml_path))
    seq = doc.getElementsByTagName("sequence")[0]
    files = {}
    for f in doc.getElementsByTagName("file"):
        pu = _child(f, "pathurl")
        if pu is not None:
            files[f.getAttribute("id")] = urllib.parse.unquote(re.sub(r"^file://(localhost)?", "", pu.firstChild.data))
    media = _child(seq, "media")
    seq_tb = int(_txt(_child(seq, "rate"), "timebase"))
    out = {"name": _txt(seq, "name"), "timebase": seq_tb, "duration": int(_txt(seq, "duration")), "video": [], "audio": []}
    for kind in ("video", "audio"):
        grp = _child(media, kind)
        if grp is None:
            continue
        for ti, t in enumerate([c for c in grp.childNodes if c.nodeName == "track"]):
            items = []
            for c in [c for c in t.childNodes if c.nodeName == "clipitem"]:
                fe = _child(c, "file")
                nested = fe is None  # a nested-sequence clipitem carries <sequence>, not <file>
                path = files.get(fe.getAttribute("id")) if fe is not None else None
                rate = _child(c, "rate")
                clip_tb = int(_txt(rate, "timebase")) if rate is not None and _txt(rate, "timebase") else seq_tb
                cin, cout = int(_txt(c, "in")), int(_txt(c, "out"))
                # xmeml in/out are in the CLIP's timebase; express them in sequence frames for comparison
                if clip_tb != seq_tb:
                    cin, cout = round(cin * seq_tb / clip_tb), round(cout * seq_tb / clip_tb)
                items.append({
                    "name": _txt(c, "name"), "start": int(_txt(c, "start")), "end": int(_txt(c, "end")),
                    "in": cin, "out": cout, "clipTimebase": clip_tb, "path": path, "nested": nested,
                    "enabled": (_txt(c, "enabled", "TRUE").upper() == "TRUE"),
                })
            items.sort(key=lambda i: i["start"])
            out[kind].append({"index": ti, "enabled": (_txt(t, "enabled", "TRUE").upper() == "TRUE"), "items": items})
    return out


# ---------------------------------------------------------------- verification
def _frames(x):
    return x if isinstance(x, int) else None


def _compare_track(tag: str, plan_items: list, live_items: list, allow_relink: bool, strict_frames: bool) -> tuple[int, list[str]]:
    """Returns (fail_count, notes). strict_frames=False downgrades frame mismatches to warnings (audio)."""
    notes: list[str] = []
    fails = 0
    live_items = sorted(live_items, key=lambda i: (_frames(i.get("startFrame")) is None, _frames(i.get("startFrame")) or 0))
    if len(plan_items) != len(live_items):
        fails += 1
        notes.append(f"FAIL {tag}: {len(plan_items)} clips in xml, {len(live_items)} in Premiere")
    mism = offline = missing = unknown = relinked = 0
    for pi, li in zip(plan_items, live_items):
        label = f"{tag} {str(pi['name'])[:40]!r}"
        lf = (_frames(li.get("startFrame")), _frames(li.get("endFrame")), _frames(li.get("inFrame")), _frames(li.get("outFrame")))
        if None in lf:
            unknown += 1
            notes.append(f"FAIL {label}: Premiere returned no frame numbers ({li.get('start')}…) — timebase unreadable")
            continue
        if (pi["start"], pi["end"], pi["in"], pi["out"]) != lf:
            mism += 1
            if mism <= 5:
                notes.append(f"{'FAIL' if strict_frames else 'warn'} {label}: xml start/end/in/out {pi['start']}/{pi['end']}/{pi['in']}/{pi['out']} vs live {lf[0]}/{lf[1]}/{lf[2]}/{lf[3]}")
        if pi.get("enabled") is False and not li.get("disabled"):
            notes.append(f"warn {label}: xml marks the clip disabled but Premiere has it enabled")
        if pi.get("nested"):
            if not li.get("isSequence"):
                notes.append(f"warn {label}: xml nests a sequence here; Premiere item is not a sequence")
            continue
        if li.get("offline") is True:
            offline += 1
            notes.append(f"FAIL {label}: media OFFLINE in Premiere ({li.get('mediaPath')})")
        elif li.get("offline") == "unknown" or li.get("mediaError") or li.get("mediaPath") in (None, ""):
            unknown += 1
            notes.append(f"FAIL {label}: Premiere could not report media state ({li.get('mediaError') or 'no media path'})")
            continue
        lp = li.get("mediaPath")
        if not Path(lp).exists():
            missing += 1
            notes.append(f"FAIL {label}: Premiere points at a file that does not exist: {lp}")
        elif pi["path"] and Path(pi["path"]).resolve() != Path(lp).resolve():
            relinked += 1
            notes.append(f"{'warn' if allow_relink else 'FAIL'} {label}: media path differs — xml {pi['path']} vs live {lp}")
    if strict_frames:
        fails += mism
    fails += offline + missing + unknown + (0 if allow_relink else relinked)
    verdict = "ok  " if not fails and not (len(plan_items) != len(live_items)) else "FAIL"
    notes.append(f"{verdict} {tag}: {len(live_items)} clips, {mism} frame mismatches, {offline} offline, {missing} missing, {unknown} unknown, {relinked} relinked")
    return fails, notes


def verify_xml(xml_path: Path, seq_name: str | None, timeout: float, seq_guid: str | None = None, allow_relink: bool = False) -> tuple[bool, list[str]]:
    """Compare the xmeml against the live Premiere sequence. Video is strict; audio is reported as warnings
    (xmeml splits stereo across two tracks, Premiere merges them, so a frame-exact audio compare is not meaningful)."""
    plan = parse_xmeml(xml_path)
    sel = {"guid": seq_guid} if seq_guid else {"name": seq_name or plan["name"]}
    live = call("dumpSequence", sel, timeout)
    notes: list[str] = []
    fails = 0

    fps = live["timing"]["fps"]
    if fps is None or round(fps, 3) != plan["timebase"]:
        fails += 1
        notes.append(f"FAIL fps: xml timebase {plan['timebase']} vs live {fps}")
    if live["timing"]["endFrames"] != plan["duration"]:
        fails += 1
        notes.append(f"FAIL duration: xml {plan['duration']} frames vs live {live['timing']['endFrames']}")
    else:
        notes.append(f"ok   duration {plan['duration']} frames @ {plan['timebase']}fps")
    if live.get("markersError"):
        notes.append(f"warn markers unreadable: {live['markersError']}")

    for kind, strict in (("video", True), ("audio", False)):
        prefix = "V" if kind == "video" else "A"
        live_tracks = {t["index"]: t for t in live.get(kind, [])}
        plan_tracks = {t["index"]: t for t in plan.get(kind, [])}
        for idx in sorted(set(live_tracks) | set(plan_tracks)):
            tag = f"{prefix}{idx + 1}"
            pt, lt = plan_tracks.get(idx), live_tracks.get(idx)
            if pt is None:
                if lt and lt["items"]:
                    if strict:
                        fails += 1
                    notes.append(f"{'FAIL' if strict else 'warn'} {tag}: not in the xml but Premiere has {len(lt['items'])} clips on it")
                continue
            if lt is None:
                if pt["items"]:
                    if strict:
                        fails += 1
                    notes.append(f"{'FAIL' if strict else 'warn'} {tag}: track missing in Premiere ({len(pt['items'])} clips expected)")
                continue
            if pt["enabled"] is False and lt.get("muted") is False:
                notes.append(f"warn {tag}: xml disables this track; Premiere reports it unmuted (check its output toggle)")
            f, n = _compare_track(tag, pt["items"], lt["items"], allow_relink, strict)
            if strict:
                fails += f
            notes += n
    return fails == 0, notes


# ---------------------------------------------------------------- CLI
def _fr(x) -> str:
    return str(x) if isinstance(x, int) else "?"


def human(op: str, r) -> str:
    if op == "sweepOffline":
        lines = [f"{r['project']}: {r['clips']} clips, {r['offline']} OFFLINE, {r.get('unknown', 0)} unknown"]
        for it in r["offlineItems"]:
            lines.append(f"  OFFLINE  {it.get('path') or it.get('name')}  →  {it.get('mediaPath')}")
        for it in r.get("unknownItems", []):
            lines.append(f"  UNKNOWN  {it.get('path') or it.get('name')}  ({it.get('error')})")
        return "\n".join(lines)
    if op == "dumpSequence":
        t = r["timing"]
        lines = [f"{r['sequence']['name']}  [{r['sequence']['guid']}]  {_fr(t['endFrames'])} frames @ {round(t['fps'], 3) if t['fps'] else '?'}fps  markers={len(r['markers'])}"]
        if r.get("markersError"):
            lines.append(f"  (markers unreadable: {r['markersError']})")
        for tr in r["video"]:
            lines.append(f"  V{tr['index'] + 1} {tr['name'] or ''}: {len(tr['items'])} clips" + ("  (muted)" if tr.get("muted") else ""))
            for it in tr["items"][:80]:
                flag = " OFFLINE" if it.get("offline") is True else (" ?media" if it.get("offline") == "unknown" or it.get("mediaError") else "")
                dis = " disabled" if it.get("disabled") else ""
                lines.append(f"     {_fr(it['startFrame']):>6}-{_fr(it['endFrame']):<6} in {_fr(it['inFrame']):>5} out {_fr(it['outFrame']):<5} {str(it['name'])[:48]}{flag}{dis}")
        for tr in r["audio"]:
            lines.append(f"  A{tr['index'] + 1} {tr['name'] or ''}: {len(tr['items'])} clips" + ("  (muted)" if tr.get("muted") else ""))
        return "\n".join(lines)
    if op == "readSelection":
        lines = [f"{r['sequence']['name']}: {r['count']} selected"]
        for it in r["items"]:
            lines.append(f"  V{(it['track'] or 0) + 1} {_fr(it['startFrame'])}-{_fr(it['endFrame'])}  {it['name']}  ←  {it.get('mediaPath')}")
        return "\n".join(lines)
    if op == "project":
        lines = [f"{r['name']}  ({r['path']})", f"active: {r['activeSequence']['name'] if r['activeSequence'] else '—'}"]
        lines += [f"  · {s['name']}  [{s['guid']}]" for s in r["sequences"]]
        return "\n".join(lines)
    if op == "importFiles":
        lines = [f"imported {len(r['imported'])} file(s) into {r.get('targetBin') or 'root'}"]
        for s in r.get("newSequences", []):
            lines.append(f"  new sequence: {s['name']}  guid={s['guid']}")
        if r.get("expectedSequence") and not r.get("newSequences"):
            lines.append("  WARNING: an xml/otio was imported but no new sequence appeared — check Premiere's Events panel")
        return "\n".join(lines)
    return json.dumps(r, indent=2)


def _seq_args(ns) -> dict:
    return {k: v for k, v in {"name": getattr(ns, "name", None), "guid": getattr(ns, "guid", None)}.items() if v}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print the raw result JSON")
    ap.add_argument("--timeout", type=float, default=60.0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("ping")
    sub.add_parser("project")
    sub.add_parser("bins")
    sub.add_parser("sweep-offline")

    def seq_opts(p):
        p.add_argument("--name"); p.add_argument("--guid")
    d = sub.add_parser("dump-sequence"); seq_opts(d); d.add_argument("--out"); d.add_argument("--video-only", action="store_true")
    s = sub.add_parser("read-selection"); seq_opts(s)
    i = sub.add_parser("import"); i.add_argument("paths", nargs="+"); i.add_argument("--bin"); i.add_argument("--show-ui", action="store_true")
    a = sub.add_parser("set-active"); seq_opts(a); a.add_argument("--open", action="store_true")
    sub.add_parser("save")
    m = sub.add_parser("add-markers"); seq_opts(m); m.add_argument("--markers-json", required=True)
    e = sub.add_parser("eval"); e.add_argument("code", nargs="?"); e.add_argument("--file"); e.add_argument("--args-json")
    v = sub.add_parser("verify-xml"); v.add_argument("xml"); seq_opts(v); v.add_argument("--allow-relink", action="store_true")
    ns = ap.parse_args(argv)

    try:
        if ns.cmd == "status":
            hb = heartbeat()
            if hb is None:
                print(f"no heartbeat in {BRIDGE_DIR} — panel not running"); return 2
            print(json.dumps(hb, indent=2) if ns.json else f"{'ALIVE' if hb['alive'] else 'STALE/PAUSED'} · {hb.get('project')} · {hb.get('activeSequence')} · {hb['age_s']}s ago · {hb.get('processed')} cmds")
            return 0 if hb["alive"] else 2
        if ns.cmd == "verify-xml":
            ok, notes = verify_xml(Path(ns.xml).resolve(), ns.name, ns.timeout, seq_guid=ns.guid, allow_relink=ns.allow_relink)
            print("\n".join(notes)); print("VERIFY:", "PASS" if ok else "FAIL")
            return 0 if ok else 3
        if ns.cmd in ("set-active", "add-markers") and not _seq_args(ns):
            raise BridgeError(f"{ns.cmd}: --name or --guid required")
        op, args = {
            "ping": ("ping", {}), "project": ("project", {}), "bins": ("bins", {}), "sweep-offline": ("sweepOffline", {}),
            "dump-sequence": ("dumpSequence", {**_seq_args(ns), "videoOnly": getattr(ns, "video_only", False)}),
            "read-selection": ("readSelection", _seq_args(ns)),
            "import": ("importFiles", {"paths": [str(Path(p).resolve()) for p in getattr(ns, "paths", [])], "bin": getattr(ns, "bin", None), "suppressUI": not getattr(ns, "show_ui", False)}),
            "set-active": ("setActiveSequence", {**_seq_args(ns), "open": getattr(ns, "open", False)}),
            "save": ("save", {}),
            "add-markers": ("addMarkers", {**_seq_args(ns), "markers": json.loads(Path(ns.markers_json).read_text()) if getattr(ns, "markers_json", None) else []}),
            "eval": ("eval", {"code": (Path(ns.file).read_text() if getattr(ns, "file", None) else getattr(ns, "code", None)), "args": json.loads(ns.args_json) if getattr(ns, "args_json", None) else {}}),
        }[ns.cmd]
        if ns.cmd == "import":
            for p in args["paths"]:
                if not Path(p).exists():
                    raise BridgeError(f"import: file not found {p}")
        if ns.cmd == "eval" and not args.get("code"):
            raise BridgeError("eval: pass code or --file")
        args = {k: v for k, v in args.items() if v is not None}
        r = call(op, args, ns.timeout)
        if ns.cmd == "dump-sequence" and ns.out:
            Path(ns.out).write_text(json.dumps(r, indent=1)); print(f"wrote {ns.out}")
        print(json.dumps(r, indent=2) if ns.json else human(op, r))
        return 0
    except BridgeError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return ex.exit_code


if __name__ == "__main__":
    sys.exit(main())
