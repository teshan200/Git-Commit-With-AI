"""Interactive Git Commit Copilot built with Google ADK.

This script exposes a small agent that inspects staged git changes, drafts a
Conventional Commit message, and only commits after explicit user confirmation.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
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
    tools=[set_repo_path, get_staged_diff, get_git_context, execute_git_commit, execute_git_push],
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


if __name__ == "__main__":
    asyncio.run(main())