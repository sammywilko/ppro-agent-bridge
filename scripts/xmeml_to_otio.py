#!/usr/bin/env python3
"""Build an OpenTimelineIO file from an FCP7 xmeml sequence, in the minimal shape Premiere's OTIO importer accepts.

Why not the otio-fcp-adapter: its output (file:// URLs, adapter metadata blobs, disabled tracks) is rejected by
Premiere 27.0 beta's importer with "The file has an unsupported compression type", while a hand-built
Timeline → Stack → Track → Clip(ExternalReference plain path) imports cleanly (measured 2026-08-29).

Why OTIO at all: UXP's Project.importFiles() runs the media importer, which rejects FCP7 XML, and
Project.open() refuses it ("Failed to convert project file"). OTIO rides the media importer and is accepted.

What survives: names, cut list, per-clip source in/out, fps, multiple tracks, gaps.
What does NOT: per-clip audio levels (the xmeml `audiolevels` filter), effects. Track "enabled=FALSE"
is emitted as OTIO Track.enabled=False only with --keep-disabled (default: all enabled; mute V2/A2 via the
bridge afterwards). Markers are emitted with --markers (sequence markers on the top stack).

Usage:  xmeml_to_otio.py CUT.xml [CUT.otio] [--video-only] [--markers] [--keep-disabled] [--no-audio]
Run with the repo venv: .venv/bin/python scripts/xmeml_to_otio.py CUT.xml
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import xml.dom.minidom as md
from pathlib import Path

import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange


def _child(e, tag):
    for c in e.childNodes:
        if c.nodeName == tag:
            return c
    return None


def _txt(e, tag, default=None):
    c = _child(e, tag)
    return c.firstChild.data.strip() if c is not None and c.firstChild else default


def parse(xml_path: Path) -> dict:
    doc = md.parse(str(xml_path))
    seq = doc.getElementsByTagName("sequence")[0]
    files = {}
    for f in doc.getElementsByTagName("file"):
        pu = _child(f, "pathurl")
        if pu is None:
            continue
        path = urllib.parse.unquote(re.sub(r"^file://(localhost)?", "", pu.firstChild.data))
        dur = _txt(f, "duration")
        rate = _child(f, "rate")
        files[f.getAttribute("id")] = {"path": path, "duration": int(dur) if dur else None,
                                       "timebase": int(_txt(rate, "timebase")) if rate is not None and _txt(rate, "timebase") else None}
    tb = int(_txt(_child(seq, "rate"), "timebase"))
    out = {"name": _txt(seq, "name"), "timebase": tb, "duration": int(_txt(seq, "duration")), "video": [], "audio": [], "markers": []}
    for m in [c for c in seq.childNodes if c.nodeName == "marker"]:
        out["markers"].append({"name": _txt(m, "name", ""), "comment": _txt(m, "comment", ""), "in": int(_txt(m, "in", "0")),
                               "out": int(_txt(m, "out", "-1"))})
    media = _child(seq, "media")
    for kind in ("video", "audio"):
        grp = _child(media, kind)
        if grp is None:
            continue
        for t in [c for c in grp.childNodes if c.nodeName == "track"]:
            items = []
            for c in [c for c in t.childNodes if c.nodeName == "clipitem"]:
                fe = _child(c, "file")
                if fe is None:
                    continue  # nested sequence — not supported here
                fi = files.get(fe.getAttribute("id"))
                if not fi:
                    continue
                rate = _child(c, "rate")
                ctb = int(_txt(rate, "timebase")) if rate is not None and _txt(rate, "timebase") else (fi["timebase"] or tb)
                items.append({"name": _txt(c, "name"), "start": int(_txt(c, "start")), "end": int(_txt(c, "end")),
                              "in": int(_txt(c, "in")), "out": int(_txt(c, "out")), "clipTimebase": ctb, "file": fi})
            items.sort(key=lambda i: i["start"])
            out[kind].append({"enabled": (_txt(t, "enabled", "TRUE").upper() == "TRUE"), "items": items})
    return out


def build(plan: dict, video_only=False, markers=False, keep_disabled=False, no_audio=False) -> otio.schema.Timeline:
    tb = plan["timebase"]
    tl = otio.schema.Timeline(name=plan["name"])
    tl.tracks.name = plan["name"]  # Premiere names the imported sequence after the top stack
    kinds = [("video", otio.schema.TrackKind.Video, "V")]
    if not video_only and not no_audio:
        kinds.append(("audio", otio.schema.TrackKind.Audio, "A"))
    for key, kind, prefix in kinds:
        n = 0
        for t in plan[key]:
            # An xmeml track may stack clipitems at the same TC (Bay Tree's A2 holds two alternate mixes at
            # frame 0). OTIO tracks are sequential, so overlapping items spill onto extra tracks.
            lanes: list[list[dict]] = []
            for it in t["items"]:
                for lane in lanes:
                    if lane[-1]["end"] <= it["start"]:
                        lane.append(it)
                        break
                else:
                    lanes.append([it])
            for lane in (lanes or [[]]):
                n += 1
                track = otio.schema.Track(name=f"{prefix}{n}", kind=kind)
                if keep_disabled and not t["enabled"]:
                    track.enabled = False
                cursor = 0
                for it in lane:
                    if it["start"] > cursor:
                        track.append(otio.schema.Gap(source_range=TimeRange(RationalTime(0, tb), RationalTime(it["start"] - cursor, tb))))
                    ctb = it["clipTimebase"]
                    avail = None
                    if it["file"]["duration"]:
                        avail = TimeRange(RationalTime(0, ctb), RationalTime(it["file"]["duration"], ctb))
                    ref = otio.schema.ExternalReference(target_url=it["file"]["path"], available_range=avail)
                    clip = otio.schema.Clip(name=it["name"], media_reference=ref,
                                            source_range=TimeRange(RationalTime(it["in"], ctb), RationalTime(it["out"] - it["in"], ctb)))
                    track.append(clip)
                    cursor = it["end"]
                tl.tracks.append(track)
    if markers:
        for m in plan["markers"]:
            dur = max(0, (m["out"] - m["in"])) if m["out"] >= 0 else 0
            tl.tracks.markers.append(otio.schema.Marker(name=m["name"] or m["comment"][:40],
                                                        marked_range=TimeRange(RationalTime(m["in"], tb), RationalTime(dur, tb)),
                                                        color=otio.schema.MarkerColor.GREEN))
    return tl


def main(argv) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        return 2
    xml_path = Path(args[0]).resolve()
    out_path = Path(args[1]).resolve() if len(args) > 1 else xml_path.with_suffix(".otio")
    plan = parse(xml_path)
    tl = build(plan, video_only="--video-only" in flags, markers="--markers" in flags,
               keep_disabled="--keep-disabled" in flags, no_audio="--no-audio" in flags)
    otio.adapters.write_to_file(tl, str(out_path))
    print(tl.name)
    for t in tl.tracks:
        clips = sum(1 for c in t if isinstance(c, otio.schema.Clip))
        print(f"  {t.name:3} {t.kind:5} clips={clips:3} enabled={t.enabled} frames={t.duration().value:g}")
    print(f"markers={len(tl.tracks.markers)} → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
