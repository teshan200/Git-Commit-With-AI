# Git Commit With AI

Git Commit With AI is an interactive AI agent built with Google ADK that reviews staged Git changes, generates Conventional Commit messages, asks for confirmation, creates the commit, and can push the commit to GitHub.

## Features

- Generates commit messages from staged diffs
- Follows Conventional Commits style
- Requires explicit confirmation before committing
- Supports per-session repository selection in ADK Web
- Pushes the current branch to `origin` after a confirmed commit
- Supports English and Sinhala conversation

## Tech Stack

- Python 3.10+
- `google-adk`
- `google-genai`
- `python-dotenv`
- Git

## Project Structure

```text
Buildathon/
|-- .env
|-- .gitignore
|-- git_agent.py
|-- git_commit_copilot/
|   `-- agent.py
`-- README.md
```

## Requirements

Make sure you have:

- Python 3.10 or newer
- Git installed and available on `PATH`
- A Google API key for Gemini
- An existing Git repository to test against

## Installation

Install the required packages:

```powershell
pip install google-adk google-genai python-dotenv
```

## Environment Setup

Create a `.env` file in the project root:

```env
GOOGLE_API_KEY=your_google_api_key_here
```

Optional model override:

```env
GEMINI_MODEL=gemini-3.1-flash-lite
```

## Running the Agent

### Option 1: ADK Web

From the project root, run:

```powershell
adk web .
```

If needed, you can point directly to the ADK agent folder:

```powershell
adk web git_commit_copilot
```

This starts the ADK Web UI. Select the `git_commit_copilot` agent in the browser.

### Option 2: Local CLI Loop

You can also run the script directly:

```powershell
python git_agent.py
```

## How Repository Selection Works

The agent can work with any repository.

In ADK Web, first tell the agent which repository to use:

```text
set repo path "C:\Users\MSI\Desktop\test repo"
```

After that, all Git operations in that session use that repository.

If you want to switch repositories, send a new repo path in the same chat session.

## How to Use the Agent

### 1. Stage your changes

In the target repository:

```powershell
git add .
```

Or stage a specific file:

```powershell
git add path\to\file.py
```

Check the staged files:

```powershell
git status
```

You should see files listed under `Changes to be committed`.

### 2. Ask for a commit message

In the ADK Web chat, after setting the repo path, ask:

```text
write a commit message for my staged changes
```

The agent will:

1. Read the staged diff
2. Inspect Git context
3. Draft a Conventional Commit message
4. Ask for confirmation before committing

### 3. Confirm the commit

Reply with something explicit, such as:

```text
yes, commit it
```

The agent will then:

1. Create the commit
2. Push the current branch to `origin`

## Push Requirements

Automatic push works only if the target repository already has:

- a valid `origin` remote
- GitHub authentication configured locally
- a branch checked out, not detached `HEAD`

To inspect remotes manually:

```powershell
git remote -v
```

If needed, add a GitHub remote:

```powershell
git remote add origin https://github.com/your-user/your-repo.git
```

## Testing Guide

### Quick test flow

1. Open or create a Git repository
2. Make a small file change
3. Stage the change with `git add .`
4. Start ADK Web with `adk web .`
5. In chat, set the repository path
6. Ask for a commit message
7. Confirm the commit
8. Verify the result with:

```powershell
git log -1 --oneline
git status
git remote -v
```

### Safe test suggestion

Use a temporary repository or a throwaway file so you can test commit and push behavior without affecting important work.

## Troubleshooting

### ADK Web says no agents found

Run ADK Web from the project root:

```powershell
adk web .
```

The ADK entrypoint is:

- `git_commit_copilot/agent.py`

### No staged changes found

Stage your files first:

```powershell
git add .
```

### Repo path is rejected

Make sure you pass the repository root folder, the one that contains `.git`.

### Push fails

Check:

- `origin` exists
- your GitHub credentials are configured
- you are on a branch

### API key error

Make sure `.env` contains:

```env
GOOGLE_API_KEY=your_google_api_key_here
```

## Notes

- The agent only commits after explicit confirmation.
- Repository choice is stored per chat session.
- The script uses the current session repo path, `GIT_REPO_PATH`, or the current working directory when resolving Git commands.

## Files

- `git_agent.py`: main agent logic, tools, and local CLI runner
- `git_commit_copilot/agent.py`: ADK-discoverable entrypoint for `adk web`
- `.env`: local API key configuration
- `.gitignore`: ignores secrets, cache files, and ADK local state
