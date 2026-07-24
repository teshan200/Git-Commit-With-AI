from flask import Flask, request, jsonify, Response
import subprocess
import sys
import json
from pathlib import Path

app = Flask(__name__)
ROOT = Path(__file__).parent
PY = sys.executable


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
def summary():
    repo = request.args.get("repo")
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

    # CLI prints summary directly when format=json; if present, return it
    if isinstance(payload, dict) and "summary" in payload:
        return jsonify(payload["summary"])
    return jsonify(payload)


@app.route("/changelog")
def changelog():
    repo = request.args.get("repo")
    args = ["changelog", "--dry-run"]
    if repo:
        args = ["--repo", repo] + args

    code, out, err = run_cli(args)
    if code != 0:
        return jsonify({"error": err or out}), 500

    try:
        payload = json.loads(out)
    except Exception:
        return Response(out, mimetype="text/plain")

    if payload.get("status") == "success" and payload.get("dry_run"):
        return Response(payload.get("changelog", ""), mimetype="text/markdown")

    return jsonify(payload)


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
