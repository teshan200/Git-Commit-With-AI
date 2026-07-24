"""Interactive Git Commit Copilot built with Google ADK.

This script exposes a small agent that inspects staged git changes, drafts a
Conventional Commit message, and only commits after explicit user confirmation.
"""

from __future__ import annotations

import argparse
import json
import asyncio
import os
import subprocess
import sys
import tempfile
import platform
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools import ToolContext

try:
    from google.adk.runners import CLIRunner
except ImportError:  # pragma: no cover - compatibility fallback for older SDKs.
    CLIRunner = InMemoryRunner

from google.genai import types
import datetime
import shutil


ENV_FILE = Path(__file__).with_name(".env")
load_dotenv(ENV_FILE if ENV_FILE.exists() else None)

if not os.getenv("GOOGLE_API_KEY"):
    fallback_api_key = os.getenv("GEMINI_API_KEY")
    if fallback_api_key:
        os.environ["GOOGLE_API_KEY"] = fallback_api_key

if not os.getenv("GOOGLE_API_KEY"):
    raise RuntimeError(
        "Missing Google API key. Add GOOGLE_API_KEY to .env (or set GEMINI_API_KEY)."
    )

APP_NAME = "git_commit_copilot_app"
USER_ID = "git_commit_copilot_user"
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
MAX_DIFF_CHARS = 10_000
SESSION_REPO_KEY = "repo_path"


def _resolve_repo_root(repo_path: str | None = None) -> Path:
    """Resolve the repository root to operate on.

    Preference order:
    1. `GIT_REPO_PATH` environment variable, if provided.
    2. The current working directory, if it is already a git repository.
    3. The nearest parent directory containing a `.git` folder.

    Returns:
        The resolved repository root path.

    Raises:
        FileNotFoundError: If no git repository can be found.
    """

    repo_override = repo_path or os.getenv("GIT_REPO_PATH")
    if repo_override:
        repo_root = Path(repo_override).expanduser().resolve()
        if (repo_root / ".git").exists():
            return repo_root

    current_directory = Path.cwd().resolve()
    if (current_directory / ".git").exists():
        return current_directory

    for candidate in current_directory.parents:
        if (candidate / ".git").exists():
            return candidate

    raise FileNotFoundError("No git repository found. Set GIT_REPO_PATH or run the agent inside a repo.")


def set_repo_path(repo_path: str, tool_context: ToolContext) -> dict[str, Any]:
    """Store the target repository path for the current chat session.

    Args:
        repo_path: Absolute or relative path to the git repository to use.
        tool_context: ADK tool context used to persist session state.

    Returns:
        A status dictionary confirming the active repository or describing the error.
    """

    candidate = Path(repo_path).expanduser().resolve()
    if not (candidate / ".git").exists():
        return {
            "status": "error",
            "error_message": f"{candidate} is not a git repository. Provide the path to a folder that contains a .git directory.",
        }

    tool_context.state[SESSION_REPO_KEY] = str(candidate)
    return {
        "status": "success",
        "message": "Repository path saved for this session.",
        "repo_path": str(candidate),
    }


def _get_active_repo_path(tool_context: ToolContext | None = None) -> str | None:
    """Return the session repo path when one has been set."""

    if tool_context is None:
        return None

    repo_path = tool_context.state.get(SESSION_REPO_KEY)
    return str(repo_path) if repo_path else None


def _run_git_command(command: list[str], repo_path: str | None = None) -> str:
    """Run a git command and return decoded output.

    Args:
        command: Command arguments to pass to git.

    Returns:
        The trimmed command output.

    Raises:
        FileNotFoundError: If git is not installed or not available on PATH.
        subprocess.CalledProcessError: If git exits with a non-zero status.
    """

    output = subprocess.check_output(
        command,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_resolve_repo_root(repo_path),
    )
    return output.strip()


def get_staged_diff(tool_context: ToolContext) -> dict[str, Any]:
    """Return the staged git diff for the current repository.

    The diff is limited to 10,000 characters to reduce token overflow risk.

    Returns:
        A status dictionary containing the staged diff or an actionable warning.
    """

    try:
        diff_text = _run_git_command(["git", "diff", "--staged"], _get_active_repo_path(tool_context))
    except FileNotFoundError:
        return {
            "status": "error",
            "error_message": "git was not found on PATH. Install Git or open the project in a Git-enabled environment.",
        }
    except subprocess.CalledProcessError as exc:
        return {
            "status": "error",
            "error_message": "Failed to read the staged diff.",
            "details": exc.output.strip() if isinstance(exc.output, str) else str(exc),
        }

    if not diff_text:
        return {
            "status": "warning",
            "message": "No staged changes were found. Run `git add` before asking for a commit message.",
            "diff": "",
            "truncated": False,
        }

    truncated = len(diff_text) > MAX_DIFF_CHARS
    if truncated:
        diff_text = diff_text[:MAX_DIFF_CHARS]

    return {
        "status": "success",
        "message": "Staged diff retrieved successfully.",
        "diff": diff_text,
        "truncated": truncated,
        "character_count": len(diff_text),
    }


def get_git_context(tool_context: ToolContext) -> dict[str, Any]:
    """Return basic repository context for commit drafting.

    This includes the current branch and the latest commit summary.

    Returns:
        A status dictionary with branch and commit information.
    """

    try:
        repo_path = _get_active_repo_path(tool_context)
        branch_name = _run_git_command(["git", "branch", "--show-current"], repo_path)
        last_commit_summary = _run_git_command(["git", "log", "-1", "--oneline"], repo_path)
        remote_url = _run_git_command(["git", "remote", "get-url", "origin"], repo_path)
    except FileNotFoundError:
        return {
            "status": "error",
            "error_message": "git was not found on PATH. Install Git or open the project in a Git-enabled environment.",
        }
    except subprocess.CalledProcessError as exc:
        remote_url = ""
        if exc.returncode != 0:
            try:
                repo_path = _get_active_repo_path(tool_context)
                branch_name = _run_git_command(["git", "branch", "--show-current"], repo_path)
                last_commit_summary = _run_git_command(["git", "log", "-1", "--oneline"], repo_path)
            except Exception:
                pass
        return {
            "status": "error",
            "error_message": "Failed to collect git context.",
            "details": exc.output.strip() if isinstance(exc.output, str) else str(exc),
        }

    return {
        "status": "success",
        "branch": branch_name or "(detached HEAD)",
        "last_commit_summary": last_commit_summary or "No commits found.",
        "remote_url": remote_url or "",
    }


def execute_git_push(tool_context: ToolContext) -> dict[str, Any]:
    """Push the current branch to the origin remote.

    Returns:
        A status dictionary with the git output or a clear error message.
    """

    repo_path = _get_active_repo_path(tool_context)

    try:
        branch_name = _run_git_command(["git", "branch", "--show-current"], repo_path)
        if not branch_name:
            return {
                "status": "error",
                "error_message": "Cannot push from a detached HEAD. Switch to a branch first.",
            }

        remote_url = _run_git_command(["git", "remote", "get-url", "origin"], repo_path)
        completed_process = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=_resolve_repo_root(repo_path),
        )
        output = (completed_process.stdout or completed_process.stderr or "").strip()
        return {
            "status": "success",
            "message": "Branch pushed to origin successfully.",
            "branch": branch_name,
            "remote_url": remote_url,
            "git_output": output,
        }
    except FileNotFoundError:
        return {
            "status": "error",
            "error_message": "git was not found on PATH. Install Git or open the project in a Git-enabled environment.",
        }
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or exc.stderr or "").strip()
        if "No configured push destination" in output or "fatal: The current branch" in output:
            return {
                "status": "error",
                "error_message": "No push destination is configured for origin. Add a GitHub remote first.",
                "git_output": output,
            }
        return {
            "status": "error",
            "error_message": "git push failed.",
            "git_output": output,
            "returncode": exc.returncode,
        }


def execute_git_commit(commit_message: str, tool_context: ToolContext) -> dict[str, Any]:
    """Create a git commit with the provided commit message.

    Args:
        commit_message: The commit message to use for `git commit -m`.

    Returns:
        A status dictionary containing the git output and any failure details.
    """

    try:
        completed_process = subprocess.run(
            ["git", "commit", "-m", commit_message],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=_resolve_repo_root(_get_active_repo_path(tool_context)),
        )
        output = (completed_process.stdout or completed_process.stderr or "").strip()
        return {
            "status": "success",
            "message": "Commit created successfully.",
            "git_output": output,
        }
    except FileNotFoundError:
        return {
            "status": "error",
            "error_message": "git was not found on PATH. Install Git or open the project in a Git-enabled environment.",
        }
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or exc.stderr or "").strip()
        return {
            "status": "error",
            "message": "git commit failed.",
            "git_output": output,
            "returncode": exc.returncode,
        }


def create_branch(branch_name: str, tool_context: ToolContext) -> dict[str, Any]:
    """Create and switch to a new branch with the given name.

    Returns a status dict with the branch name or an error message.
    """

    try:
        repo_path = _get_active_repo_path(tool_context)
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            check=True,
            capture_output=True,
            text=True,
            cwd=_resolve_repo_root(repo_path),
        )
        return {"status": "success", "branch": branch_name}
    except FileNotFoundError:
        return {"status": "error", "error_message": "git was not found on PATH."}
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or exc.stderr or "").strip()
        return {"status": "error", "error_message": "Failed to create branch.", "git_output": output}


def create_github_pr(title: str, body: str, base: str | None, tool_context: ToolContext) -> dict[str, Any]:
    """Create a GitHub PR using the `gh` CLI if available.

    Falls back with an informative error if `gh` isn't installed.
    """

    if shutil.which("gh") is None:
        return {
            "status": "error",
            "error_message": "GitHub CLI `gh` not found on PATH. Install `gh` or create the PR manually.",
        }

    try:
        repo_path = _get_active_repo_path(tool_context)
        # If base is not provided, let gh prompt or default to repository default branch.
        cmd = ["gh", "pr", "create", "--title", title, "--body", body]
        if base:
            cmd += ["--base", base]

        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            cwd=_resolve_repo_root(repo_path),
        )
        output = (completed.stdout or completed.stderr or "").strip()
        return {"status": "success", "message": "PR created.", "gh_output": output}
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or exc.stderr or "").strip()
        return {"status": "error", "error_message": "Failed to create PR.", "gh_output": output}


def auto_branch_commit_and_pr(commit_message: str, pr_title: str | None, pr_body: str | None, base: str | None, tool_context: ToolContext) -> dict[str, Any]:
    """Orchestrate branch creation, commit, push, and PR creation.

    This expects the changes to already be staged. It will:
    1. Create a timestamped branch named `improvement/YYYYMMDD-HHMMSS`.
    2. Commit using `commit_message`.
    3. Push the branch and create a PR with `pr_title`/`pr_body`.
    """

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    branch_name = f"improvement/{timestamp}"

    # 1) create branch
    branch_res = create_branch(branch_name, tool_context)
    if branch_res.get("status") != "success":
        return {"status": "error", "step": "create_branch", "result": branch_res}

    # 2) commit
    commit_res = execute_git_commit(commit_message, tool_context)
    if commit_res.get("status") != "success":
        return {"status": "error", "step": "commit", "result": commit_res}

    # 3) push
    push_res = execute_git_push(tool_context)
    if push_res.get("status") != "success":
        return {"status": "error", "step": "push", "result": push_res}

    # 4) create PR
    title = pr_title or (commit_message.splitlines()[0] if commit_message else branch_name)
    body = pr_body or commit_message
    pr_res = create_github_pr(title, body, base, tool_context)
    if pr_res.get("status") != "success":
        return {"status": "error", "step": "create_pr", "result": pr_res}

    return {
        "status": "success",
        "branch": branch_name,
        "commit": commit_res,
        "push": push_res,
        "pr": pr_res,
    }


def generate_commit_message(tool_context: ToolContext) -> dict[str, Any]:
    """Generate a Conventional Commit message from the staged diff using a heuristic.

    Falls back to a simple heuristic when no model call is available.
    """

    diff_res = get_staged_diff(tool_context)
    if diff_res.get("status") != "success":
        return {"status": "error", "error_message": "No staged diff available.", "details": diff_res}

    diff = diff_res.get("diff", "")
    if not diff:
        return {"status": "warning", "message": "No staged changes to summarize.", "commit_message": ""}

    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))

    lower = diff.lower()
    if any(k in lower for k in ("fix", "bug", "error", "panic", "typo")):
        prefix = "fix:"
    elif any(k in lower for k in ("add ", "new file", "create ", "implement ", "feature")) or added > removed:
        prefix = "feat:"
    elif any(k in lower for k in ("doc", "readme", "comment", "documentation")):
        prefix = "docs:"
    elif any(k in lower for k in ("refactor", "cleanup", "restructure")):
        prefix = "refactor:"
    elif any(k in lower for k in ("format", "style", "whitespace")):
        prefix = "style:"
    else:
        prefix = "chore:"

    # attempt to extract first changed filename
    filenames = []
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                # format: diff --git a/path b/path
                a = parts[2]
                if a.startswith("a/"):
                    filenames.append(a[2:])

    subject = f"{prefix} update {filenames[0] if filenames else ''}".strip()
    if len(subject) > 50:
        subject = subject[:47].rstrip() + "..."

    body_lines = []
    body_lines.append(f"Changes: +{added} / -{removed} (staged)")
    if filenames:
        body_lines.append("Files changed:")
        for f in filenames[:5]:
            body_lines.append(f"- {f}")

    body = "\n".join(body_lines)
    commit_message = subject + ("\n\n" + body if body else "")

    return {"status": "success", "commit_message": commit_message, "generated_by": "heuristic"}


def generate_commit_message_model(tool_context: ToolContext) -> dict[str, Any]:
    """Attempt to generate a commit message using the GenAI model.

    If the model client isn't available or a call fails, returns an error dict
    so callers can fall back to the heuristic generator.
    """

    diff_res = get_staged_diff(tool_context)
    if diff_res.get("status") != "success":
        return {"status": "error", "error_message": "No staged diff available.", "details": diff_res}

    diff = diff_res.get("diff", "")
    if not diff:
        return {"status": "warning", "message": "No staged changes to summarize.", "commit_message": ""}

    prompt = (
        "You are an assistant that writes Conventional Commit messages. "
        "Given the following git diff, produce a single concise Conventional Commit message (subject under 50 chars), "
        "and, if helpful, a short body. Output only the commit message.\n\n"
        f"DIFF:\n{diff}"
    )

    try:
        # Try a few possible client patterns for different installed genai versions.
        genai = __import__("google.genai")
    except Exception:
        return {"status": "error", "error_message": "google.genai not installed"}

    try:
        # preferred pattern: genai.text.generate
        if hasattr(genai, "text") and hasattr(genai.text, "generate"):
            resp = genai.text.generate(model=DEFAULT_MODEL, input=prompt)
            text = getattr(resp, "text", None) or getattr(resp, "output", None)
        # alternative: genai.generate_text
        elif hasattr(genai, "generate_text"):
            resp = genai.generate_text(model=DEFAULT_MODEL, prompt=prompt)
            text = getattr(resp, "output", None) or getattr(resp, "text", None)
        else:
            return {"status": "error", "error_message": "Unsupported google.genai API surface"}

        # Extract string from response flexibly
        if isinstance(text, str):
            commit_message = text.strip()
        else:
            # try common nested structures
            commit_message = ""
            try:
                if isinstance(text, list) and text:
                    # look for .content or .text
                    first = text[0]
                    commit_message = getattr(first, "text", "") or getattr(first, "content", "") or str(first)
                else:
                    commit_message = str(text)
            except Exception:
                commit_message = str(text)

        commit_message = commit_message.strip()
        # safeguard: ensure it's not empty
        if not commit_message:
            return {"status": "error", "error_message": "Model returned empty message"}

        return {"status": "success", "commit_message": commit_message, "generated_by": "model"}
    except Exception as exc:
        return {"status": "error", "error_message": "Model call failed", "details": str(exc)}


root_agent = Agent(
    name="git_commit_copilot",
    model=DEFAULT_MODEL,
    instruction=(
        "You are Git Commit Copilot, an intelligent assistant for drafting and"
        " executing git commits.\n\n"
        "Operating rules:\n"
        "1. Always call get_staged_diff first whenever the user asks for a"
        " commit message or asks you to review staged changes.\n"
        "2. Use get_git_context when branch or recent history would improve the"
        " commit decision.\n"
        "3. Analyze the staged diff and draft exactly one Conventional Commit"
        " message. Use prefixes such as feat:, fix:, refactor:, style:, docs:,"
        " or chore:.\n"
        "4. Keep the subject line under 50 characters. If the change is"
        " complex, add a brief bulleted body describing what changed and why.\n"
        "5. Present the proposed commit message cleanly inside a Markdown code"
        " block.\n"
        "6. Before committing, ask the user for explicit confirmation. Never"
        " call execute_git_commit unless the user clearly confirms with a"
        " response such as yes or commit it. If the user asks to push to"
        " GitHub, automatically push after the confirmed commit. After a"
        " successful commit, automatically call execute_git_push so the commit"
        " is pushed to the origin remote on the current branch.\n"
        "7. If the user communicates in Sinhala, reply in Sinhala while keeping"
        " the Conventional Commit message itself in standard commit format.\n"
        "8. If there are no staged changes, tell the user to run git add first."
        "9. If no repository has been set for the session, ask the user to paste"
        " the repo path and call set_repo_path before using any git tools."
    ),
    tools=[set_repo_path, get_staged_diff, get_git_context, generate_commit_message, execute_git_commit, execute_git_push, create_branch, create_github_pr, auto_branch_commit_and_pr],
)


def _extract_text(event: Any) -> str:
    """Extract plain text from a runner event."""

    if not getattr(event, "content", None) or not getattr(event.content, "parts", None):
        return ""

    texts = [part.text for part in event.content.parts if getattr(part, "text", None)]
    return "".join(texts).strip()


async def _create_runner() -> Any:
    """Create the most capable ADK runner available in the installed SDK."""

    try:
        return CLIRunner(agent=root_agent, app_name=APP_NAME)
    except TypeError:
        return CLIRunner(root_agent)


async def _chat_loop(runner: Any) -> None:
    """Run a simple interactive terminal loop against the agent."""

    session = await runner.session_service.create_session(
        user_id=USER_ID,
        app_name=APP_NAME,
    )

    print("Git Commit Copilot ready. Type /new for a fresh session or /exit to quit.")
    print(f"Active session: {session.id}")

    while True:
        try:
            user_input = await asyncio.to_thread(input, "You: ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        message = user_input.strip()
        if not message:
            continue

        lowered = message.lower()
        if lowered in {"/exit", "exit", "quit"}:
            break
        if lowered == "/new":
            session = await runner.session_service.create_session(
                user_id=USER_ID,
                app_name=APP_NAME,
            )
            print(f"New session: {session.id}")
            continue

        content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session.id,
            new_message=content,
        ):
            if tool_calls := event.get_function_calls():
                for function_call in tool_calls:
                    print(f"[tool call] {function_call.name}({function_call.args})")

            if tool_responses := event.get_function_responses():
                for function_response in tool_responses:
                    print(f"[tool result] {function_response.name}: {function_response.response}")

            text = _extract_text(event)
            if text and getattr(event, "author", None) != "user":
                print(text)


async def main() -> None:
    """Run the interactive Git Commit Copilot."""

    runner = await _create_runner()
    await _chat_loop(runner)


def _print_result(result: dict[str, Any]) -> None:
    try:
        print(json.dumps(result, indent=2))
    except Exception:
        print(result)


def run_cli(argv: list[str] | None = None) -> int:
    """Run a small CLI wrapper to call the agent helper functions.

    Usage examples:
      git_agent.py auto-pr --message "Fix bug" --title "Fix: bug"
      git_agent.py commit --message "chore: update"
      git_agent.py branch --name feature/xyz
      git_agent.py pr --title "My PR" --body "Details"
    """

    parser = argparse.ArgumentParser(prog="git_commit_copilot")
    parser.add_argument("--repo", help="Path to the git repository", default=None)
    subparsers = parser.add_subparsers(dest="cmd")

    p_auto = subparsers.add_parser("auto-pr", help="Create branch, commit, push, and open PR")
    p_auto.add_argument("--message", required=False, help="Commit message (staged changes expected). If omitted, a message will be generated.")
    p_auto.add_argument("--title", required=False, help="PR title")
    p_auto.add_argument("--body", required=False, help="PR body")
    p_auto.add_argument("--base", required=False, help="PR base branch (defaults to repo default)")

    p_commit = subparsers.add_parser("commit", help="Create a git commit with provided message")
    p_commit.add_argument("--message", required=True)

    p_branch = subparsers.add_parser("branch", help="Create and switch to a branch")
    p_branch.add_argument("--name", required=True)

    p_pr = subparsers.add_parser("pr", help="Create a GitHub PR using gh CLI")
    p_pr.add_argument("--title", required=True)
    p_pr.add_argument("--body", required=False)
    p_pr.add_argument("--base", required=False)

    p_install = subparsers.add_parser("install-hook", help="Install commit-msg hook to enforce Conventional Commits")

    args = parser.parse_args(argv)

    if args.repo:
        os.environ["GIT_REPO_PATH"] = args.repo

    if args.cmd == "auto-pr":
        message = args.message
        if not message:
            # Try model-backed generation, fall back to heuristic
            try:
                model_res = generate_commit_message_model(None)
            except Exception:
                model_res = {"status": "error", "error_message": "model generation failed"}

            if model_res.get("status") == "success":
                message = model_res.get("commit_message")
            else:
                heuristic_res = generate_commit_message(None)
                message = heuristic_res.get("commit_message")

        # Allow interactive editing of the generated message when running in a TTY
        if sys.stdin.isatty():
            print("\nGenerated commit message:\n")
            print(message)
            try:
                edit_choice = input("\nEdit commit message before applying? (y/N): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                edit_choice = "n"

            if edit_choice == "y":
                message = edit_with_editor(message)
                print("\nFinal commit message:\n")
                print(message)

            # Validate and optionally auto-fix or re-edit
            attempts = 0
            while attempts < 3:
                val = validate_conventional_commit(message)
                if val.get("valid"):
                    break
                print("\nConventional Commit validation issues:")
                for err in val.get("errors", []):
                    print(f"- {err}")

                try:
                    fix_choice = input("Auto-apply suggested fix and continue? (Y/n): ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    fix_choice = "y"

                if fix_choice in ("", "y", "yes"):
                    message = val.get("suggested_message") or message
                    print("\nApplied suggested commit message:\n")
                    print(message)
                    break
                else:
                    try:
                        reedit = input("Open editor to modify message? (Y/n): ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        reedit = "n"

                    if reedit in ("", "y", "yes"):
                        message = edit_with_editor(message)
                        print("\nEdited commit message:\n")
                        print(message)
                        attempts += 1
                        continue
                    else:
                        # user declined fixes; proceed with current message
                        break

        res = auto_branch_commit_and_pr(message, args.title, args.body, args.base, None)
        _print_result(res)
        return 0 if res.get("status") == "success" else 2

    if args.cmd == "commit":
        res = execute_git_commit(args.message, None)
        _print_result(res)
        return 0 if res.get("status") == "success" else 2

    if args.cmd == "branch":
        res = create_branch(args.name, None)
        _print_result(res)
        return 0 if res.get("status") == "success" else 2

    if args.cmd == "pr":
        res = create_github_pr(args.title, args.body or "", args.base, None)
        _print_result(res)
        return 0 if res.get("status") == "success" else 2

    if args.cmd == "install-hook":
        res = install_commit_msg_hook(None)
        _print_result(res)
        return 0 if res.get("status") == "success" else 2

    parser.print_help()
    return 1


def edit_with_editor(initial_text: str) -> str:
    """Open the user's editor to edit `initial_text` and return the result."""

    editor = os.getenv("VISUAL") or os.getenv("EDITOR")
    if not editor:
        if platform.system() == "Windows":
            editor = "notepad"
        else:
            editor = "vi"

    with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".md") as tf:
        tf.write(initial_text)
        tf.flush()
        path = tf.name

    try:
        subprocess.run([editor, path])
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def validate_conventional_commit(message: str) -> dict[str, Any]:
    """Validate and suggest fixes for a Conventional Commit message.

    Checks:
      - Subject prefix (feat|fix|chore|docs|style|refactor|perf|test)
      - Subject line length <= 50
      - Blank line between subject and body when body exists

    Returns a dict with `valid` boolean, `errors` list, and `suggested_message`.
    """

    if message is None:
        return {"valid": False, "errors": ["Empty message"], "suggested_message": ""}

    lines = message.strip().splitlines()
    subject = lines[0] if lines else ""
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

    errors: list[str] = []
    suggested = subject

    # check prefix
    prefixes = ("feat:", "fix:", "chore:", "docs:", "style:", "refactor:", "perf:", "test:")
    lower_sub = subject.lower()
    if not any(lower_sub.startswith(p) for p in prefixes):
        # attempt to infer prefix
        if any(k in lower_sub for k in ("fix", "bug", "error", "typo")):
            pref = "fix:"
        elif any(k in lower_sub for k in ("doc", "readme", "comment", "documentation")):
            pref = "docs:"
        elif any(k in lower_sub for k in ("refactor", "cleanup", "restructure")):
            pref = "refactor:"
        elif any(k in lower_sub for k in ("add ", "new ", "implement", "feature")):
            pref = "feat:"
        else:
            pref = "chore:"

        suggested = f"{pref} {subject}".strip()
        errors.append("Missing conventional commit prefix; suggested prefix added.")

    # subject length
    if len(subject) > 50:
        errors.append(f"Subject longer than 50 chars ({len(subject)})")
        # truncate suggested subject to 50 chars
        sug_subject = (suggested[:47].rstrip() + "...") if len(suggested) > 50 else suggested
        suggested = sug_subject

    # body separation
    if body and (len(lines) > 1 and lines[1].strip() != ""):
        errors.append("Missing blank line between subject and body")
        suggested = suggested + "\n\n" + body

    valid = len(errors) == 0
    suggested_message = suggested
    if body and "\n\n" not in suggested_message:
        suggested_message = suggested_message + ("\n\n" + body if body else "")

    return {"valid": valid, "errors": errors, "suggested_message": suggested_message}


def install_commit_msg_hook(tool_context: ToolContext) -> dict[str, Any]:
    """Install a Python-based commit-msg hook into the repo's .git/hooks.

    The hook calls `validate_conventional_commit` from this module and
    aborts the commit if validation fails.
    """

    try:
        repo_root = _resolve_repo_root(_get_active_repo_path(tool_context))
    except Exception as exc:
        return {"status": "error", "error_message": "Cannot find git repository", "details": str(exc)}

    hook_dir = repo_root / ".git" / "hooks"
    if not hook_dir.exists():
        return {"status": "error", "error_message": ".git/hooks directory not found"}

    hook_path = hook_dir / "commit-msg"
    hook_code = """#!/usr/bin/env python3
import sys
from pathlib import Path
from git_agent import validate_conventional_commit

def main():
    if len(sys.argv) < 2:
        print('No commit message file provided', file=sys.stderr)
        sys.exit(1)
    msgfile = Path(sys.argv[1])
    msg = msgfile.read_text(encoding='utf-8')
    res = validate_conventional_commit(msg)
    if not res.get('valid'):
        print('Conventional Commit validation failed:', file=sys.stderr)
        for e in res.get('errors', []):
            print('- ' + e, file=sys.stderr)
        print('\nSuggested commit message:\n', file=sys.stderr)
        print(res.get('suggested_message', ''), file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

if __name__ == '__main__':
    main()
"""

    try:
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write(hook_code)
        # Make executable where supported
        try:
            import os
            os.chmod(hook_path, 0o755)
        except Exception:
            pass

        return {"status": "success", "message": f"Installed commit-msg hook at {hook_path}"}
    except Exception as exc:
        return {"status": "error", "error_message": "Failed to write hook", "details": str(exc)}


if __name__ == "__main__":
    # If any CLI args are provided, run the CLI mode; otherwise start interactive chat.
    if len(sys.argv) > 1:
        raise SystemExit(run_cli())
    asyncio.run(main())