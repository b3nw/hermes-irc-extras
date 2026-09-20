"""Test configurations and path setup for hermes-irc-extras."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add the parent directory of this test folder (the repo root) to python's PATH
# so that the test runner can import `hermes_irc_extras` cleanly.
REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

# Let's also check if we can locate the main hermes-agent repository on this host
# and add it to our python path, so that we can run tests importing its modules!
# Override with HERMES_AGENT_REPO; the defaults below just keep this host working.
_DEFAULT_HERMES_AGENT_REPOS = (
    "/opt/data/workspace/developer/projects/hermes/hermes-agent/repo",
    "/workspace/projects/3p/hermes/hermes-agent",
    "/opt/data/workspace/developer/projects/hermes/hermes-agent/"
    "worktrees/feat-hermes-irc-extras",
)
_CANDIDATES = (os.environ.get("HERMES_AGENT_REPO"), *_DEFAULT_HERMES_AGENT_REPOS)
for _candidate in _CANDIDATES:
    if not _candidate:
        continue
    _path = Path(_candidate).expanduser().resolve()
    if _path.exists():
        sys.path.insert(1, str(_path))
        break
