"""A stand-in for the UXP panel: speaks the same file protocol so the client can be tested without Premiere.

Run in a thread by the tests; answers a fixed set of ops from FIXTURE.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

TICKS = 254016000000
FPS = 24
TPF = TICKS // FPS


def tt(frames: int) -> dict:
    return {"seconds": frames / FPS, "ticks": str(frames * TPF)}


def clip(track, name, start, end, in_, out, path, offline=False, media_error=None):
    return {
        "kind": "video", "track": track, "name": name,
        "start": tt(start), "end": tt(end), "in": tt(in_), "out": tt(out), "duration": tt(end - start),
        "startFrame": start, "endFrame": end, "inFrame": in_, "outFrame": out,
        "disabled": False, "speed": 100, "selected": False,
        "projectItem": name, "mediaPath": path, "offline": offline, "isSequence": False, "mediaError": media_error,
    }


def make_fixture(media_dir: Path, offline_second: bool = False, unknown_second: bool = False, extra_track: bool = False, relink_second: str | None = None) -> dict:
    a = str(media_dir / "S01.mp4")
    b = relink_second or str(media_dir / "S02.mp4")
    video = [{"index": 0, "name": "V1", "id": 1, "muted": False, "items": [
        clip(0, "S01 · Dawn", 0, 72, 12, 84, a),
        clip(0, "S02 · Pot", 72, 120, 12, 60, b, offline=("unknown" if unknown_second else offline_second), media_error=("isOffline: boom" if unknown_second else None)),
    ]}]
    if extra_track:
        video.append({"index": 1, "name": "V2", "id": 2, "muted": False, "items": [clip(1, "STRAY", 0, 24, 0, 24, a)]})
    return {
        "sequence": {"name": "TEST CUT", "guid": "abc"},
        "timing": {"timebase": str(TPF), "ticksPerFrame": TPF, "fps": FPS, "endSeconds": 5.0, "endFrames": 120},
        "video": video,
        "audio": [],
        "markers": [{"name": "M1", "comments": "", "type": "Comment", "colorIndex": 0, "start": tt(0), "duration": tt(0), "startFrame": 0, "guid": "m1"}],
        "markersError": None,
    }


class FakePanel(threading.Thread):
    def __init__(self, bridge_dir: Path, fixture: dict, delay: float = 0.0):
        super().__init__(daemon=True)
        self.dir = bridge_dir
        self.fixture = fixture
        self.delay = delay
        self.stop = threading.Event()
        self.seen: list[dict] = []
        self.paused = False

    def heartbeat(self):
        p = self.dir / "heartbeat.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "pluginVersion": "fake", "paused": self.paused, "processed": len(self.seen), "project": "FAKE.prproj", "activeSequence": "TEST CUT"}))
        os.replace(tmp, p)

    def run(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat()
        last_hb = time.time()
        while not self.stop.is_set():
            if time.time() - last_hb > 0.5:
                self.heartbeat(); last_hb = time.time()
            for cmd in sorted(self.dir.glob("cmd-*.json")):
                cid = cmd.name[len("cmd-"):-len(".json")]
                running = self.dir / f"cmd-{cid}.running"
                os.replace(cmd, running)
                body = json.loads(running.read_text())
                self.seen.append(body)
                time.sleep(self.delay)
                op, args = body["op"], body.get("args", {})
                try:
                    if op == "ping":
                        res = {"pluginVersion": "fake", "appVersion": "27.0.0", "project": {"name": "FAKE.prproj"}}
                    elif op == "dumpSequence":
                        if args.get("guid") and args["guid"] != "abc":
                            raise ValueError(f'no sequence with guid {args["guid"]}')
                        if args.get("name") not in (None, "TEST CUT"):
                            raise ValueError(f'no sequence named "{args.get("name")}"')
                        res = self.fixture
                    elif op == "importFiles":
                        if any(p.endswith("fail.xml") for p in args["paths"]):
                            res = {"ok": False, "imported": args["paths"], "targetBin": args.get("bin"), "newSequences": [], "expectedSequence": True}
                        else:
                            res = {"ok": True, "imported": args["paths"], "targetBin": args.get("bin"), "newSequences": [{"name": "TEST CUT", "guid": "abc"}], "expectedSequence": True}
                    elif op == "sweepOffline":
                        items = [i for t in self.fixture["video"] for i in t["items"]]
                        off = [i for i in items if i["offline"]]
                        res = {"project": "FAKE.prproj", "totalItems": len(items), "clips": len(items), "offline": len(off), "offlineItems": off, "items": items}
                    else:
                        raise ValueError(f"unknown op {op}")
                    env = {"id": cid, "op": op, "ok": True, "result": res, "ms": 1, "ts": "now"}
                except Exception as e:  # noqa: BLE001
                    env = {"id": cid, "op": op, "ok": False, "error": str(e), "ms": 1, "ts": "now"}
                out = self.dir / f"result-{cid}.json"
                tmp = self.dir / f"result-{cid}.json.tmp"
                tmp.write_text(json.dumps(env))
                os.replace(tmp, out)
                running.unlink()
            time.sleep(0.05)
