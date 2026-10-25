import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.claude_code_client import (WEB_SEARCH_TOOL, ClaudeCodeClient, ClaudeError, StreamParser,  # noqa: E402
                                    classify_error, extract_sources)

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_claude.py")


class ClaudeClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "calls.jsonl")
        # Envoltorio ejecutable para que el cliente lo trate como el binario `claude`.
        self.exe = os.path.join(self.tmp.name, "claude")
        with open(self.exe, "w") as fh:
            fh.write(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        os.chmod(self.exe, os.stat(self.exe).st_mode | stat.S_IEXEC)
        os.environ["FAKE_CLAUDE_LOG"] = self.log
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        os.environ["CLAUDECODE"] = "1"  # simula que Vimo se lanzó desde una sesión de Claude Code

    def tearDown(self):
        for k in ("FAKE_CLAUDE_LOG", "FAKE_CLAUDE_MODE", "CLAUDECODE", "FAKE_CLAUDE_LOGGED_IN"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def client(self, **cfg):
        base = {"executable": self.exe, "timeout_seconds": 20, "enable_web_search": True, "extra_args": []}
        base.update(cfg)
        c = ClaudeCodeClient(base, "Eres Vimo.", os.path.join(self.tmp.name, "ws"), self.tmp.name)
        c.write_mcp_config()
        self.addCleanup(c.shutdown)
        return c

    def calls(self):
        with open(self.log) as fh:
            return [json.loads(line) for line in fh]

    def test_check_reports_version_and_auth(self):
        info = self.client().check()
        self.assertEqual(info["version"], "9.9.9 (Claude Code)")
        self.assertTrue(info["logged_in"])

    def test_check_not_logged_in(self):
        os.environ["FAKE_CLAUDE_LOGGED_IN"] = "0"
        with self.assertRaises(ClaudeError) as ctx:
            self.client().check()
        self.assertEqual(ctx.exception.kind, "auth")

    def test_turns_reuse_session_and_safe_arguments(self):
        c = self.client()
        r1 = c.submit("-hola; rm -rf /").result(timeout=20)
        r2 = c.submit("segundo").result(timeout=20)
        self.assertEqual(r1.text, "eco: -hola; rm -rf /")
        self.assertEqual(r2.text, "eco: segundo")
        first, second = self.calls()
        # El texto del usuario va por stdin, nunca en argv.
        self.assertNotIn("-hola; rm -rf /", " ".join(first["argv"]))
        self.assertIn("-hola; rm -rf /", first["stdin"])
        self.assertIn("[Fecha y hora local:", first["stdin"])
        # Primer turno crea la sesión; el segundo la reanuda con el mismo id.
        sid = first["argv"][first["argv"].index("--session-id") + 1]
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], sid)
        # Restricciones de herramientas y permisos.
        argv = first["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], WEB_SEARCH_TOOL)
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertFalse(first["env_claudecode"], "no debe heredar CLAUDECODE")
        self.assertEqual(c.session_turns, 2)

    def test_search_sources_are_extracted(self):
        os.environ["FAKE_CLAUDE_MODE"] = "search"
        progress = []
        r = self.client().submit("noticias", on_progress=lambda k, d: progress.append((k, d))).result(timeout=20)
        self.assertEqual(r.searches, ["noticias"])
        self.assertEqual(r.sources[0]["url"], "https://ejemplo.com/a")
        self.assertEqual(progress[0][0], "search")

    def test_auth_and_rate_limit_errors(self):
        for mode, kind in (("auth_error", "auth"), ("rate_limit", "rate_limit")):
            os.environ["FAKE_CLAUDE_MODE"] = mode
            with self.assertRaises(ClaudeError) as ctx:
                self.client().submit("x").result(timeout=20)
            self.assertEqual(ctx.exception.kind, kind)

    def test_timeout_kills_process(self):
        os.environ["FAKE_CLAUDE_MODE"] = "slow"
        c = self.client(timeout_seconds=1.5)
        t0 = time.monotonic()
        with self.assertRaises(ClaudeError) as ctx:
            c.submit("x").result(timeout=20)
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertLess(time.monotonic() - t0, 8)

    def test_cancel(self):
        os.environ["FAKE_CLAUDE_MODE"] = "slow"
        c = self.client()
        fut = c.submit("x")
        threading.Timer(1.0, c.cancel).start()
        with self.assertRaises(ClaudeError) as ctx:
            fut.result(timeout=20)
        self.assertEqual(ctx.exception.kind, "cancelled")

    def test_cancel_does_not_leak_into_next_request(self):
        os.environ["FAKE_CLAUDE_MODE"] = "slow"
        c = self.client()
        c.cancel()                       # sin nada en curso: no debe afectar a la siguiente
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        self.assertEqual(c.submit("hola").result(timeout=20).text, "eco: hola")

    def test_lost_session_starts_new_one(self):
        os.environ["FAKE_CLAUDE_MODE"] = "bad_resume"
        c = self.client()
        c.session_started, c.session_turns = True, 3
        old = c.session_id
        r = c.submit("hola").result(timeout=20)
        self.assertEqual(r.text, "eco: hola")
        self.assertNotEqual(c.session_id, old)

    def test_queue_limit(self):
        os.environ["FAKE_CLAUDE_MODE"] = "slow"
        c = self.client()
        c.submit("1")
        c.submit("2")
        with self.assertRaises(ClaudeError) as ctx:
            c.submit("3")
        self.assertEqual(ctx.exception.kind, "busy")
        c.cancel()

    def test_web_search_can_be_disabled(self):
        c = self.client(enable_web_search=False)
        argv = c.build_command(resume=False)
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--allowedTools", argv)
        self.assertIn("--strict-mcp-config", argv)


class ParsingTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_error("Please run /login"), "auth")
        self.assertEqual(classify_error("x", 429), "rate_limit")
        self.assertEqual(classify_error("getaddrinfo ENOTFOUND api.anthropic.com"), "network")
        self.assertEqual(classify_error("algo raro"), "unknown")

    def test_extract_sources_variants(self):
        payload = {"results": [{"title": "A", "url": "https://a.com"}, {"title": "mala", "url": "javascript:x"}]}
        self.assertEqual(len(extract_sources(json.dumps(payload))), 1)
        self.assertEqual(len(extract_sources([{"type": "text", "text": json.dumps(payload)}])), 1)
        self.assertEqual(extract_sources("no json"), [])

    def test_parser_ignores_garbage(self):
        p = StreamParser()
        p.feed("no es json")
        p.feed("")
        p.feed("[1, 2]")                                  # JSON que no es un objeto
        p.feed(json.dumps({"type": "assistant", "message": {"content": ["texto", None]}}))
        p.feed(json.dumps({"type": "user", "message": None}))
        p.feed(json.dumps({"type": "result", "subtype": "success", "result": "ok"}))
        self.assertEqual(p.result["result"], "ok")


if __name__ == "__main__":
    unittest.main()
