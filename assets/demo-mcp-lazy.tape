# Kodiqa — Lazy MCP tools demo  →  assets/demo-mcp-lazy.gif
# Install vhs: https://github.com/charmbracelet/vhs
# Run: vhs assets/demo-mcp-lazy.tape
#
# Records a real, key-free session: connect an MCP server and toggle lazy mode.
# Only slash commands are typed (no chat), so it makes no API calls.
# (Lines starting with # are tape comments — they are NOT typed into the terminal.)

Output assets/demo-mcp-lazy.gif

Set FontSize 16
Set Width 1400
Set Height 760
Set Theme "Dracula"
Set TypingSpeed 45ms
Set Padding 20

# Launch (skip the startup update check; model is incidental — no chat happens)
Type "kodiqa --no-update --model claude"
Enter
Sleep 7s

# Connect a real MCP server — the filesystem server exposes 14 tools, no API key
Type "/mcp add fs npx -y @modelcontextprotocol/server-filesystem /tmp"
Enter
Sleep 9s

# Lazy mode is ON by default: 3 meta-tools per turn, 14 tools discovered on demand
Type "/mcp list"
Enter
Sleep 6s

# Toggle OFF to see the contrast — every tool schema injected on every turn
Type "/mcp lazy off"
Enter
Sleep 3s
Type "/mcp list"
Enter
Sleep 6s

# Back to the token-efficient default
Type "/mcp lazy on"
Enter
Sleep 4s

Type "/quit"
Enter
Sleep 3s
