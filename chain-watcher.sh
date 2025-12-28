#!/bin/bash
# Chain Prompt Watcher
# Runs in yaniv's session, watches for prompt file, injects into tmux
#
# Usage: ./chain-watcher.sh [target]
# Default target: 0:zara

PROMPT_FILE="/tmp/chain-prompt-queue.txt"
TARGET="${1:-0:zara}"

echo "Chain watcher started. Watching $PROMPT_FILE for $TARGET"

while true; do
    if [ -f "$PROMPT_FILE" ]; then
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
