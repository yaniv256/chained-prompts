#!/bin/bash
# Chain Prompt Watcher
# Runs in yaniv's session, watches for prompt file, injects into tmux
#
# Usage: ./chain-watcher.sh [target]
# Default target: 0:zara

PROMPT_FILE="/tmp/chain-prompt-queue.txt"
FIND_SESSION="/home/yaniv/agent-flow/mcp-servers/periodic-prompts/find-session.sh"

echo "Chain watcher started. Watching $PROMPT_FILE (dynamic target via find-session.sh)"

while true; do
    if [ -f "$PROMPT_FILE" ]; then
        # Resolve target dynamically each time
        TARGET=$(bash "$FIND_SESSION")
        echo "$(date): Resolved target: $TARGET"

        # Load and inject
        tmux load-buffer "$PROMPT_FILE"
        tmux paste-buffer -t "$TARGET"
        sleep 0.3
        tmux send-keys -t "$TARGET" Enter

        # Clean up
        rm "$PROMPT_FILE"
        echo "$(date): Injected prompt to $TARGET"
    fi
    sleep 0.5
done
