"""Client ↔ panel protocol tests, run against the fake panel (no Premiere needed).

python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_panel import FakePanel, make_fixture  # noqa: E402

XMEML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
<sequence id="seq-test"><name>TEST CUT</name><duration>{dur}</duration><rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
<media><video><track><enabled>TRUE</enabled>
<clipitem id="c1"><name>S01 · Dawn</name><start>0</start><end>72</end><in>12</in><out>84</out><file id="f1"><name>S01.mp4</name><pathurl>file://localhost{a}</pathurl><media><video/></media></file></clipitem>
<clipitem id="c2"><name>S02 · Pot</name><start>72</start><end>{end2}</end><in>12</in><out>{out2}</out><file id="f2"><name>S02.mp4</name><pathurl>file://localhost{b}</pathurl><media><video/></media></file></clipitem>
</track></video></media></sequence></xmeml>
"""


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "bridge"
        self.media = Path(self.tmp.name) / "media"
        self.media.mkdir()
        (self.media / "S01.mp4").write_bytes(b"x")
        (self.media / "S02.mp4").write_bytes(b"x")
        os.environ["PPRO_BRIDGE_DIR"] = str(self.dir)
        import ppro_bridge
        importlib.reload(ppro_bridge)
        self.pb = ppro_bridge
        self.panel = None

    def start_panel(self, **kw):
        fixture_kw = {k: v for k, v in kw.items() if k in ("offline_second", "unknown_second", "extra_track", "relink_second")}
        self.panel = FakePanel(self.dir, make_fixture(self.media, **fixture_kw), delay=kw.get("delay", 0.0))
        self.panel.start()
        for _ in range(50):
            if (self.dir / "heartbeat.json").exists():
                break
            time.sleep(0.02)

    def tearDown(self):
        if self.panel:
            self.panel.stop.set(); self.panel.join(timeout=2)
        self.tmp.cleanup()

    def write_xml(self, end2=120, out2=60):
        p = Path(self.tmp.name) / "cut.xml"
        p.write_text(XMEML.format(dur=end2, a=self.media / "S01.mp4", b=self.media / "S02.mp4", end2=end2, out2=out2))
        return p

    # ---- transport
    def test_unreachable_without_heartbeat(self):
        with self.assertRaises(self.pb.PanelUnreachable):
            self.pb.send("ping", {}, timeout=1)
        self.assertEqual(list(self.dir.glob("cmd-*")), [], "no command file should be left behind")

    def test_ping_roundtrip_and_cleanup(self):
        self.start_panel()
        r = self.pb.call("ping")
        self.assertEqual(r["appVersion"], "27.0.0")
        time.sleep(0.1)
        leftovers = [p.name for p in self.dir.iterdir() if p.name != "heartbeat.json"]
        self.assertEqual(leftovers, [], "cmd/result files must be consumed")

    def test_error_envelope_becomes_exception(self):
        self.start_panel()
        with self.assertRaises(self.pb.BridgeError) as cm:
            self.pb.call("dumpSequence", {"name": "NOPE"})
        self.assertIn("no sequence named", str(cm.exception))

    def test_timeout_withdraws_unclaimed_command(self):
        self.start_panel()
        self.panel.stop.set(); self.panel.join()
        # heartbeat still fresh, but nobody is polling
        with self.assertRaises(self.pb.PanelUnreachable):
            self.pb.send("ping", {}, timeout=0.6)
        self.assertEqual(list(self.dir.glob("cmd-*")), [])

    def test_commands_are_processed_in_order(self):
        self.start_panel()
        for i in range(3):
            self.pb.call("ping")
        self.assertEqual([c["op"] for c in self.panel.seen], ["ping"] * 3)

    # ---- verify-xml
    def test_verify_xml_pass(self):
        self.start_panel()
        ok, notes = self.pb.verify_xml(self.write_xml(), None, 5)
        self.assertTrue(ok, "\n".join(notes))
        self.assertTrue(any("V1: 2 clips, 0 frame mismatches" in n for n in notes))

    def test_verify_xml_detects_frame_mismatch(self):
        self.start_panel()
        ok, notes = self.pb.verify_xml(self.write_xml(end2=121, out2=61), None, 5)
        self.assertFalse(ok)
        self.assertTrue(any("FAIL duration" in n for n in notes))
        self.assertTrue(any("frame mismatches" in n and "1 frame mismatches" in n for n in notes))

    def test_verify_xml_detects_offline_media(self):
        self.start_panel(offline_second=True)
        ok, notes = self.pb.verify_xml(self.write_xml(), None, 5)
        self.assertFalse(ok)
        self.assertTrue(any("OFFLINE" in n for n in notes))

    def test_verify_xml_detects_missing_file_on_disk(self):
        self.start_panel()
        (self.media / "S02.mp4").unlink()
        ok, notes = self.pb.verify_xml(self.write_xml(), None, 5)
        self.assertFalse(ok)
        self.assertTrue(any("does not exist" in n for n in notes))

    def test_verify_xml_fails_on_unknown_media_state(self):
        self.start_panel(unknown_second=True)
        ok, notes = self.pb.verify_xml(self.write_xml(), None, 5)
        self.assertFalse(ok)
        self.assertTrue(any("could not report media state" in n for n in notes))

    def test_verify_xml_fails_on_extra_live_track_with_clips(self):
        self.start_panel(extra_track=True)
        ok, notes = self.pb.verify_xml(self.write_xml(), None, 5)
        self.assertFalse(ok)
        self.assertTrue(any("V2: not in the xml but Premiere has 1 clips" in n for n in notes))

    def test_verify_xml_relink_fails_unless_allowed(self):
        other = self.media / "S02-other.mp4"; other.write_bytes(b"y")
        self.start_panel(relink_second=str(other))
        ok, notes = self.pb.verify_xml(self.write_xml(), None, 5)
        self.assertFalse(ok)
        self.assertTrue(any("media path differs" in n and n.startswith("FAIL") for n in notes))
        ok2, notes2 = self.pb.verify_xml(self.write_xml(), None, 5, allow_relink=True)
        self.assertTrue(ok2, "\n".join(notes2))

    def test_verify_xml_by_guid(self):
        self.start_panel()
        ok, _ = self.pb.verify_xml(self.write_xml(), None, 5, seq_guid="abc")
        self.assertTrue(ok)
        with self.assertRaises(self.pb.BridgeError):
            self.pb.verify_xml(self.write_xml(), None, 5, seq_guid="nope")

    def test_verify_xml_converts_clip_timebase(self):
        # a 48fps source: xml in/out 24/168 in clip frames == 12/84 sequence frames
        self.start_panel()
        p = Path(self.tmp.name) / "cut48.xml"
        xml = XMEML.format(dur=120, a=self.media / "S01.mp4", b=self.media / "S02.mp4", end2=120, out2=60)
        xml = xml.replace("<in>12</in><out>84</out>", "<in>24</in><out>168</out><rate><timebase>48</timebase></rate>", 1)
        p.write_text(xml)
        ok, notes = self.pb.verify_xml(p, None, 5)
        self.assertTrue(ok, "\n".join(notes))

    def test_verify_xml_tolerates_nested_sequence_clipitem(self):
        self.start_panel()
        p = Path(self.tmp.name) / "nested.xml"
        xml = XMEML.format(dur=120, a=self.media / "S01.mp4", b=self.media / "S02.mp4", end2=120, out2=60)
        xml = xml.replace('<file id="f2"><name>S02.mp4</name><pathurl>file://localhost' + str(self.media / "S02.mp4") + '</pathurl><media><video/></media></file>', '<sequence id="inner"><name>INNER</name></sequence>')
        p.write_text(xml)
        ok, notes = self.pb.verify_xml(p, None, 5)  # must not raise; live item is a plain clip → warn, not crash
        self.assertTrue(any("nests a sequence" in n for n in notes))

    def test_result_ok_false_is_an_error(self):
        self.start_panel()
        bad = Path(self.tmp.name) / "fail.xml"; bad.write_text("<xmeml/>")
        with self.assertRaises(self.pb.BridgeError) as cm:
            self.pb.call("importFiles", {"paths": [str(bad)]})
        self.assertIn("ok=false", str(cm.exception))
        self.assertEqual(self.pb.main(["import", str(bad)]), 1)

    def test_stale_results_are_swept(self):
        self.start_panel()
        stale = self.dir / "result-000-0000.json"; stale.write_text("{}")
        os.utime(stale, (time.time() - 5000, time.time() - 5000))
        self.pb.call("ping")
        self.assertFalse(stale.exists())

    # ---- CLI
    def test_cli_status_and_sweep(self):
        self.start_panel(offline_second=True)
        self.assertEqual(self.pb.main(["status"]), 0)
        self.assertEqual(self.pb.main(["sweep-offline"]), 0)
        self.assertEqual(self.pb.main(["verify-xml", str(self.write_xml())]), 3)

    def test_cli_import_refuses_missing_file(self):
        self.start_panel()
        self.assertEqual(self.pb.main(["import", str(self.media / "nope.xml")]), 1)
        self.assertEqual(self.panel.seen, [], "nothing should reach Premiere")


if __name__ == "__main__":
    unittest.main()
