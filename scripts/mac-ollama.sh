#!/usr/bin/env bash
# Optional fast local models for FlowAI on the owner's Mac Mini (ADR 0037, profile ollama-mac).
# Runs Homebrew's ollama as a per-user LaunchAgent bound to all interfaces so the Itential VM can
# reach it through oob-gw's NAT (http://<mac LAN ip>:11434), and pulls the pinned model.
# Idempotent; no sudo. Not lab infrastructure: nothing in verify/ depends on it.
set -euo pipefail
MODEL=${1:-qwen3:30b-a3b}
PLIST="$HOME/Library/LaunchAgents/com.itential-enterprise-lab.ollama.plist"
OLLAMA=$(command -v ollama) || { echo "ollama not installed (brew install ollama)"; exit 1; }
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.itential-enterprise-lab.ollama</string>
  <key>ProgramArguments</key><array><string>${OLLAMA}</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>OLLAMA_HOST</key><string>0.0.0.0:11434</string>
    <key>OLLAMA_KEEP_ALIVE</key><string>30m</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${HOME}/Library/Logs/ollama.log</string>
  <key>StandardErrorPath</key><string>${HOME}/Library/Logs/ollama.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/com.itential-enterprise-lab.ollama" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
for _ in $(seq 1 20); do curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null || { echo "ollama did not start; see ~/Library/Logs/ollama.log"; exit 1; }
echo "ollama serving on 0.0.0.0:11434 ($(ollama --version 2>/dev/null | tail -1))"
ollama list | grep -q "^${MODEL} " && echo "model ${MODEL} present" || { echo "pulling ${MODEL} (large download)"; ollama pull "$MODEL"; }
ollama list | grep "^${MODEL} "
