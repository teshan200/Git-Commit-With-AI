from flask import Flask, request, jsonify, Response
import subprocess
import sys
import json
from pathlib import Path
import os
import time
from functools import wraps

app = Flask(__name__)
ROOT = Path(__file__).parent
PY = sys.executable

# Simple in-memory cache: {key: (expiry_ts, value)}
_CACHE: dict[str, tuple[float, str]] = {}
CACHE_TTL = int(os.getenv("GITCOPILOT_CACHE_TTL", "60"))


def cache_get(key: str):
    entry = _CACHE.get(key)
    if not entry:
        return None
    expiry, value = entry
    if time.time() > expiry:
        _CACHE.pop(key, None)
        return None
    return value


def cache_set(key: str, value: str, ttl: int = CACHE_TTL):
    _CACHE[key] = (time.time() + ttl, value)


def require_token(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = os.getenv("DASHBOARD_TOKEN")
        if not token:
            return func(*args, **kwargs)
        # prefer header, then query param
        provided = request.headers.get("X-API-KEY") or request.args.get("token")
        if not provided or provided != token:
            return jsonify({"error": "Unauthorized"}), 401
        return func(*args, **kwargs)

    return wrapper


def run_cli(args, cwd=ROOT):
    cmd = [PY, str(ROOT / "git_agent.py")] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    return proc.returncode, stdout, stderr


@app.route("/")
def index():
    return (
        "<h3>Git Commit Copilot Dashboard</h3>"
        "<ul>"
        "<li><a href='/summary'>Repository Summary (JSON)</a></li>"
        "<li><a href='/changelog'>Changelog (preview)</a></li>"
        "</ul>"
    )


@app.route("/summary")
@require_token
def summary():
    repo = request.args.get("repo")
    cache_key = f"summary:{repo or 'default'}"
    cached = cache_get(cache_key)
    if cached:
        try:
            return jsonify(json.loads(cached))
        except Exception:
            return Response(cached, mimetype="application/json")

    args = ["summarize", "--format", "json"]
    if repo:
        args = ["--repo", repo] + args

    code, out, err = run_cli(args)
    if code != 0:
        return jsonify({"error": err or out}), 500

    try:
        payload = json.loads(out)
    except Exception:
        return jsonify({"error": "Failed to parse CLI output", "raw": out}), 500

    body = payload.get("summary") if isinstance(payload, dict) and "summary" in payload else payload
    cache_set(cache_key, json.dumps(body))
    return jsonify(body)


@app.route("/changelog")
@require_token
def changelog():
    repo = request.args.get("repo")
    cache_key = f"changelog:{repo or 'default'}"
    cached = cache_get(cache_key)
    if cached:
        return Response(cached, mimetype="text/markdown")

    args = ["changelog", "--dry-run"]
    if repo:
        args = ["--repo", repo] + args

    code, out, err = run_cli(args)
    if code != 0:
        return jsonify({"error": err or out}), 500

    try:
        payload = json.loads(out)
    except Exception:
        cache_set(cache_key, out)
        return Response(out, mimetype="text/plain")

    if payload.get("status") == "success" and payload.get("dry_run"):
        md = payload.get("changelog", "")
        cache_set(cache_key, md)
        return Response(md, mimetype="text/markdown")

    return jsonify(payload)


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
