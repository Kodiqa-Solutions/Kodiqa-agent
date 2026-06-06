# Kodiqa — Editor/IDE bridge demo  →  assets/demo-bridge.gif
# Run: vhs assets/demo-bridge.tape
#
# Key-free: starts the bridge and hits /health + /diagnostics (no model call).
# Uses a fixed demo token via KODIQA_BRIDGE_TOKEN so the curl is reproducible.

Output assets/demo-bridge.gif

Set FontSize 16
Set Width 1400
Set Height 760
Set Theme "Dracula"
Set TypingSpeed 45ms
Set Padding 20

# Start Kodiqa as a local HTTP server your editor can call
Type "export KODIQA_BRIDGE_TOKEN=demo-secret"
Enter
Type "kodiqa --serve --port 8765 &"
Enter
Sleep 8s

# An editor extension just makes HTTP calls — e.g. health check:
Type "curl -s localhost:8765/health | python3 -m json.tool"
Enter
Sleep 4s

# Diagnostics for a file (LSP-backed; empty here since no LSP is running)
Type 'curl -s "localhost:8765/diagnostics?file=app.py" -H "Authorization: Bearer demo-secret" | python3 -m json.tool'
Enter
Sleep 4s

# POST /ask sends a prompt (+ selected code as context) and returns the answer
Type 'echo ''POST /ask  {"prompt":"explain this","context":"<selected code>"}  ->  {"response": "..."}'''
Enter
Sleep 4s

Type "kill %1"
Enter
Sleep 2s
