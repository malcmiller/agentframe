"""Serve the plain A2A peer (``a2a_peer.py``) as its own process.

Usage: ``python peer_process.py PORT``. It has no AgentFrame code; see
``agent_processes.peer_process``.
"""

import sys

import uvicorn

from a2a_peer import PeerExecutor, a2a_peer

if __name__ == "__main__":
    port = int(sys.argv[1])
    app = a2a_peer(PeerExecutor())(f"http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
