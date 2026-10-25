#!/usr/bin/env python3
"""Sustituto de `claude` para las pruebas: imita la salida stream-json de `claude -p`.

Modo según la variable FAKE_CLAUDE_MODE: ok | search | auth_error | rate_limit | slow | bad_resume.
Guarda los argumentos y el stdin recibidos en FAKE_CLAUDE_LOG (JSON) para poder comprobarlos.
"""
import json
import os
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": os.environ.get("FAKE_CLAUDE_LOGGED_IN", "1") == "1", "authMethod": "fake"}))
    sys.exit(0)

prompt = sys.stdin.read()
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
        fh.write(json.dumps({"argv": args, "stdin": prompt, "env_claudecode": "CLAUDECODE" in os.environ}) + "\n")

sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]


def emit(obj):
    print(json.dumps(obj), flush=True)


emit({"type": "system", "subtype": "init", "session_id": sid, "tools": ["mcp__vimo__web_search"]})
if mode == "slow":
    time.sleep(60)
if mode == "bad_resume" and "--resume" in args:
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "result": "No conversation found with session ID: " + sid, "session_id": sid})
    sys.exit(1)
if mode == "auth_error":
    emit({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
          "result": "Invalid API key · Please run /login", "session_id": sid})
    sys.exit(1)
if mode == "rate_limit":
    emit({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
          "result": "Claude usage limit reached", "session_id": sid})
    sys.exit(1)
if mode == "search":
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__vimo__web_search", "input": {"query": "noticias"}}]}})
    payload = {"query": "noticias", "results": [{"title": "Noticia", "url": "https://ejemplo.com/a", "snippet": "x"}]}
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"result": json.dumps(payload)})}]}})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Según ejemplo.com…"}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": f"eco: {prompt.splitlines()[-1]}",
      "session_id": sid, "duration_ms": 12, "num_turns": 1, "total_cost_usd": 0.0})
