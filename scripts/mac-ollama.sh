#!/usr/bin/env bash
# THE lab's inference host (ADR 0060, profile ollama-mac). Every FlowAI local agent runs here: the
# lab has no GPU, and the in-lab Ollama sat at 91% of a 6 GiB cap on a 7.8 GiB VM until llama-server
# crashed mid-verify. There is no in-lab fallback by design, so if this is not running, every
# `-local` twin fails and verify S4c.5/S4d.5f/5h-5k go red.
#
# Runs Homebrew's ollama as a per-user LaunchAgent bound to all interfaces so the lab can reach it
# through oob-gw (http://ollama.lab.internal:11434), and pulls the pinned model. Idempotent; no sudo.
#
# Two things this script cannot do for you, both of which silently break the whole local fleet:
#   1. macOS firewall. It allows applications by resolved path, and the running binary here is the
#      Homebrew one, NOT /Applications/Ollama.app. Allow it once (measured 2026-09-11: the lab could
#      ping the Mac but TCP 11434 was dropped until this was run), and again after any brew upgrade,
#      which changes the Cellar path:
#        sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add $(command -v ollama)
#        sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp $(command -v ollama)
#   2. The DHCP reservation. topology/ipam.yaml home_lan.inference_host records this Mac's address,
#      which is inside the router's DHCP pool; if the lease moves, ollama.lab.internal points at
#      nothing until unbound is re-rendered.
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
    <!-- a fleet-wide tool result must fit the context: twelve parsed devices need 16384 (ADR 0046).
         This used to be set on the lab's Ollama container; it moved here with the inference. -->
    <key>OLLAMA_CONTEXT_LENGTH</key><string>16384</string>
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
# the lab reaches this by name; a local-only listener is the failure the firewall note above describes
if command -v ipconfig >/dev/null; then
  ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
  [ -n "$ip" ] && { curl -s -m 3 "http://${ip}:11434/api/tags" >/dev/null \
    && echo "reachable on ${ip}:11434" \
    || echo "WARNING: ${ip}:11434 does not answer from this Mac - the lab will not reach it either (firewall?)"; }
fi
ollama list | grep -q "^${MODEL} " && echo "model ${MODEL} present" || { echo "pulling ${MODEL} (large download)"; ollama pull "$MODEL"; }
ollama list | grep "^${MODEL} "
