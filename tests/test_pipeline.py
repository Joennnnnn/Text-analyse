"""端到端测试：本地假模型 + 外部交接 + 分阶段换模型。不需要真实 API key。

运行：.venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf

from chipdoc.cli import main
from chipdoc.extract import extract_rules
from chipdoc.index import build_index
from chipdoc.reduce import merge_facts
from chipdoc.render import diff_snapshots
from chipdoc.verify import Fact, check

ROOT = Path(__file__).resolve().parents[1]
TI_PDF = ROOT / "tms320f28054f.pdf"


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        system = body["messages"][0]["content"]
        if "抽取器" in system:
            payload = {"facts": [
                {"field": "max_freq", "value": "60 MHz", "unit": "MHz", "page": 1,
                 "quote": "SYSCLK up to 60 MHz"},
                {"field": "core", "value": "C28x", "unit": "", "page": 1,
                 "quote": "32-bit C28x core"},
                {"field": "can", "value": "3", "unit": "", "page": 2,
                 "quote": "three CAN modules"},          # 编造，应被丢弃
            ]}
        else:
            payload = {"bullets": ["SYSCLK up to 60 MHz [p.1]", "不存在的内容 [p.9]"]}
        data = json.dumps({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 100), "ACME X100 datasheet", fontsize=14)
    p1.insert_text((72, 130), "SYSCLK up to 60 MHz on this device.", fontsize=11)
    p1.insert_text((72, 150), "It embeds a 32-bit C28x core.", fontsize=11)
    p2 = doc.new_page()
    p2.insert_text((72, 100), "Memory", fontsize=14)
    p2.insert_text((72, 130), "On-chip flash is 128 KB.", fontsize=11)
    doc.save(str(path))
    doc.close()


class TestVerify(unittest.TestCase):
    def test_verbatim_hit(self):
        f = Fact("max_freq", "60 MHz", 1, "SYSCLK up to 60 MHz")
        self.assertEqual(check(f, "SYSCLK up to 60 MHz on this device.", 3).status, "verified")

    def test_wrong_page_rejected(self):
        f = Fact("flash", "128 KB", 2, "flash is 128 KB")
        self.assertEqual(check(f, "nothing relevant here", 3).status, "rejected")

    def test_value_only_downgrades(self):
        f = Fact("core", "C28x", 1, "完全对不上的引文")
        self.assertEqual(check(f, "It embeds a 32-bit C28x core.", 3).status, "page_only")

    def test_page_bounds(self):
        self.assertFalse(check(Fact("core", "C28x", 9, "x"), "C28x", 3).ok)


class TestMerge(unittest.TestCase):
    def test_prefers_verified(self):
        facts = [Fact("core", "C28x", 2, "x", confidence="page_only"),
                 Fact("core", "C28x", 1, "y", confidence="verified")]
        self.assertEqual(merge_facts(facts, "datasheet")["core"]["page"], 1)


class TestCatchAll(unittest.TestCase):
    def test_other_collects_multiple(self):
        facts = [Fact("other", "温度传感器: 有", 3, "x", confidence="verified"),
                 Fact("other", "双采样保持: 有", 5, "y", confidence="verified")]
        merged = merge_facts(facts, "datasheet")
        self.assertEqual(merged["other"]["value"], "2 条")
        self.assertEqual(len(merged["other"]["alternates"]), 2)


class TestDiff(unittest.TestCase):
    def test_changed_value(self):
        old = {"docs": {"a": {"title": "A", "fields": {"max_freq": {"value": "60 MHz", "page": 1}}}}}
        new = {"docs": {"a": {"title": "A", "fields": {"max_freq": {"value": "100 MHz", "page": 2}}}}}
        d = diff_snapshots(old, new)
        self.assertEqual(d["changed"][0]["new"], "100 MHz")

    def test_equivalent_not_flagged(self):
        old = {"docs": {"a": {"title": "A", "fields": {"flash": {"value": "64K", "page": 5}}}}}
        new = {"docs": {"a": {"title": "A", "fields": {"flash": {"value": "64 KB", "page": 5}}}}}
        self.assertEqual(diff_snapshots(old, new)["changed"], [])


class TestIndexAndRules(unittest.TestCase):
    def test_chunks_cover_every_page(self):
        idx = build_index(TI_PDF)
        covered = {p for c in idx.chunks for p in c.pages}
        self.assertEqual(covered, set(range(1, idx.page_count + 1)))

    def test_rules_frequency(self):
        facts, _ = extract_rules(build_index(TI_PDF))
        by = {f.field: f for f in facts}
        self.assertIn("max_freq", by)
        self.assertIn("60", by["max_freq"].value)

    def test_no_llm_noise(self):
        facts, _ = extract_rules(build_index(TI_PDF))
        self.assertFalse(any(f.value.lower() == "arm" for f in facts))


class TestPipeline(unittest.TestCase):
    """analyze → work/ 产物 → out/ 矩阵与 diff；再跑一次应复用。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.pdf = cls.tmp / "acme_x100.pdf"
        make_pdf(cls.pdf)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        os.environ["DEEPSEEK_API_KEY"] = "test-key"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def _run(self, *extra, config=None):
        work, out = self.tmp / "work", self.tmp / "out"
        argv = ["analyze", str(self.pdf), "--work", str(work), "--out", str(out),
                "--model", "mock", "--base-url", f"http://127.0.0.1:{self.port}/v1"]
        if config:
            argv += ["--config", str(config)]
        return main(argv + list(extra)), work, out

    def test_flow(self):
        code, work, out = self._run()
        self.assertEqual(code, 0)
        profile = json.loads((work / "acme_x100" / "profile.json").read_text())
        self.assertEqual(profile["fields"]["max_freq"]["value"], "60 MHz")
        self.assertEqual(profile["fields"]["max_freq"]["confidence"], "verified")
        self.assertNotIn("can", profile["fields"])       # 编造页码被丢弃
        bullets = [b for s in profile["sections"] for b in s["bullets"]]
        self.assertTrue(any("[p.1]" in b for b in bullets))
        self.assertFalse(any("[p.9]" in b for b in bullets))
        self.assertTrue((out / "matrix.md").exists())
        self.assertTrue((out / "diff.md").exists())

    def test_incremental_reuse(self):
        self._run()
        work = self.tmp / "work"
        before = (work / "acme_x100" / "facts" / "verified.json").read_text()
        self._run()
        self.assertEqual(before, (work / "acme_x100" / "facts" / "verified.json").read_text())

    def test_command_provider_for_summary(self):
        script = self.tmp / "cmd_model.py"
        script.write_text('print(\'{"bullets": ["SYSCLK up to 60 MHz CMD [p.1]"]}\')', encoding="utf-8")
        cfg = self.tmp / "cfg.json"
        cfg.write_text(json.dumps({
            "default": {"provider": "openai", "model": "mock",
                        "base_url": f"http://127.0.0.1:{self.port}/v1",
                        "api_key_env": "DEEPSEEK_API_KEY"},
            "stages": {"summarize": {"provider": "command",
                                     "command": [sys.executable, str(script)]}},
        }), encoding="utf-8")
        code, work, _ = self._run("--refresh", config=cfg)
        self.assertEqual(code, 0)
        profile = json.loads((work / "acme_x100" / "profile.json").read_text())
        bullets = [b for s in profile["sections"] for b in s["bullets"]]
        self.assertTrue(any("CMD" in b for b in bullets), bullets)


class TestExternalHandoff(unittest.TestCase):
    """完全不用 HTTP：index → extract(external) → ingest → verify → summarize(external) → ingest → merge → render。"""

    def test_handoff(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            pdf = tmp / "acme_x100.pdf"
            make_pdf(pdf)
            work, out = tmp / "work", tmp / "out"
            self.assertEqual(main(["index", str(pdf), "--work", str(work)]), 0)
            wd = work / "acme_x100"

            self.assertEqual(main(["extract", str(wd), "--provider", "external"]), 0)
            cid = sorted((wd / "prompt" / "extract").glob("*.json"))[0].stem
            payload = {"facts": [
                {"field": "max_freq", "value": "60 MHz", "page": 1, "quote": "SYSCLK up to 60 MHz"},
                {"field": "core", "value": "C28x", "page": 1, "quote": "32-bit C28x core"},
                {"field": "flash", "value": "999 KB", "page": 2, "quote": "flash is 999 KB"},
            ]}
            (wd / "response" / "extract").mkdir(parents=True, exist_ok=True)
            (wd / "response" / "extract" / f"{cid}.json").write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(main(["ingest", str(wd)]), 0)
            self.assertEqual(main(["verify", str(wd)]), 0)

            self.assertEqual(main(["summarize", str(wd), "--provider", "external"]), 0)
            sid = sorted((wd / "prompt" / "summary").glob("*.json"))[0].stem
            (wd / "response" / "summary").mkdir(parents=True, exist_ok=True)
            (wd / "response" / "summary" / f"{sid}.json").write_text(
                json.dumps({"bullets": ["SYSCLK up to 60 MHz [p.1]"]}), encoding="utf-8")
            self.assertEqual(main(["ingest", str(wd)]), 0)

            self.assertEqual(main(["merge", str(wd)]), 0)
            profile = json.loads((wd / "profile.json").read_text())
            self.assertNotIn("flash", profile["fields"])     # 编造的 999 KB 被丢弃
            self.assertEqual(profile["fields"]["max_freq"]["value"], "60 MHz")

            self.assertEqual(main(["render", "--work", str(work), "--out", str(out)]), 0)
            self.assertTrue((out / "matrix.md").exists())
            self.assertTrue((out / "diff.md").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPrepareFinish(unittest.TestCase):
    """本地准备（不调模型）→ agent 答题 → 本地收尾（不调模型）。"""

    def test_local_prepare_then_agent_then_finish(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            pdf = tmp / "acme_x100.pdf"
            make_pdf(pdf)
            work, out = tmp / "work", tmp / "out"
            self.assertEqual(main(["prepare", str(pdf), "--work", str(work), "--out", str(out)]), 0)

            wd = work / "acme_x100"
            self.assertTrue((wd / "TASKS.md").exists())
            cid = sorted((wd / "prompt" / "extract").glob("*.json"))[0].stem

            ans = tmp / "ans.json"
            ans.write_text(json.dumps({"facts": [
                {"field": "max_freq", "value": "60 MHz", "page": 1, "quote": "SYSCLK up to 60 MHz"},
                {"field": "flash", "value": "1 MB", "page": 2, "quote": "flash is 1 MB"},
            ]}), encoding="utf-8")
            self.assertEqual(main(["submit", str(wd), cid, "--file", str(ans)]), 0)
            self.assertEqual(main(["finish", str(wd), "--out", str(out)]), 0)

            profile = json.loads((wd / "profile.json").read_text())
            self.assertEqual(profile["fields"]["max_freq"]["value"], "60 MHz")
            self.assertNotIn("flash", profile["fields"])      # 编造的被丢弃
            summary = (out / "docs" / "acme_x100" / "summary.md").read_text(encoding="utf-8")
            self.assertIn("[p.1]", summary)
            self.assertTrue((out / "matrix.md").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGenericDocument(unittest.TestCase):
    """任意技术文档（这里用一页应用笔记）走 generic 字段集 + agent 循环所需命令。"""

    def test_generic_and_agent_commands(self):
        import contextlib
        import io
        tmp = Path(tempfile.mkdtemp())
        try:
            pdf = tmp / "an1234.pdf"
            doc = pymupdf.open()
            page = doc.new_page()
            page.insert_text((72, 90), "AN-1234 Application Note", fontsize=14)
            page.insert_text((72, 120), "SPI setup and hold times for the ACME sensor.", fontsize=11)
            doc.save(str(pdf))
            doc.close()

            work = tmp / "work"
            self.assertEqual(main(["index", str(pdf), "--work", str(work), "--kind", "generic"]), 0)

            from chipdoc.stages import Workdoc
            wd = Workdoc(work / "an1234")
            self.assertIn("key_params", [f.id for f in wd.fields])

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main(["pending", str(wd.dir)])
            self.assertIn("c000", buf.getvalue())

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main(["show", str(wd.dir), "c000"])
            text = buf.getvalue()
            self.assertIn("字段清单", text)
            self.assertIn("key_params", text)
            self.assertIn("<<<p.1>>>", text)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main(["status", str(wd.dir)])
            self.assertIn("0/1", buf.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
