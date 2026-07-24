import os
import importlib

from git_helpers import validate_conventional_commit, generate_commit_message_from_diff


def test_validate_missing_prefix():
    msg = "Update README with details"
    res = validate_conventional_commit(msg)
    assert not res["valid"]
    assert "Missing conventional commit prefix" in res["errors"][0]
    assert res["suggested_message"].startswith(("chore:", "feat:", "docs:"))


def test_validate_with_prefix_and_body():
    msg = "feat: add login\n\nThis adds a new login flow.\n"
    res = validate_conventional_commit(msg)
    assert res["valid"]


def test_generate_commit_message_from_diff():
    diff = """
diff --git a/README.md b/README.md
index 83db48f..bf269f5 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,6 @@
+Added new usage examples
+Fixed typo in intro
+Another line
"""
    res = generate_commit_message_from_diff(diff)
    assert res["status"] == "success"
    assert res["commit_message"]
