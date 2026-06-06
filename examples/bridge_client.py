#!/usr/bin/env python3
"""Minimal reference client for the Kodiqa editor/IDE bridge.

Start the bridge first:   kodiqa --serve            (prints URL + token)
Then:                     python examples/bridge_client.py <url> <token>

This is the ~20 lines an editor extension needs: call /ask with the user's
question and the selected code as context, and show the response. /diagnostics
returns LSP diagnostics for a file (when an LSP is running).
"""

import sys
import requests


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    headers = {"Authorization": f"Bearer {token}"}

    print("health:", requests.get(f"{url}/health", timeout=10).json())

    # Ask about a selection (an editor would pass the selected code as `context`)
    r = requests.post(f"{url}/ask", headers=headers, timeout=120, json={
        "prompt": "What does this function do, and is there a bug?",
        "context": "def add(a, b):\n    return a - b",
    })
    print("\nanswer:\n", r.json().get("response", r.text))

    # Diagnostics for a file (requires an LSP started in Kodiqa via /lsp)
    d = requests.get(f"{url}/diagnostics", headers=headers, params={"file": "app.py"}, timeout=10)
    print("\ndiagnostics:", d.json())


if __name__ == "__main__":
    main()
