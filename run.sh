#!/bin/zsh
# Entry point for launchd: load credentials, then run one sync pass.
source "$HOME/.env.private"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"
cd "$(dirname "$0")" && exec uv run --quiet python rm_zotero_sync.py "$@"
