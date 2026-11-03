"""Multiplexing TCP proxy for fgelev.

Keeps a single fgelev process alive so its terrain-tile cache persists
across multiple client connections and export runs. If fgelev exits
unexpectedly it is restarted automatically.

fgelev's protocol is designed for concurrency: every request carries a
caller-supplied id and every response echoes that id, so multiple clients
can have queries in flight simultaneously without serialisation.

    client → proxy:  "<id> <lon> <lat>\\n"
    proxy  → fgelev: "<id> <lon> <lat>\\n"   (all clients merged)
    fgelev → proxy:  "<id>:<elevation>\\n"
    proxy  → client: "<id>:<elevation>\\n"   (routed by id)

One thread reads fgelev's stdout and dispatches responses; one thread per
client reads its queries and writes them to fgelev's stdin. A shared lock
guards stdin writes so lines are not interleaved.

Usage:
    python3 proxy.py [--host 0.0.0.0] [--port 9000]

Environment:
    FGELEV_PATH   Path to the fgelev binary (default: /usr/local/bin/fgelev)
    FG_ROOT       Passed to fgelev via environment
    FG_SCENERY    Passed to fgelev via environment
"""

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("fgelev-proxy")

FGELEV_PATH = os.environ.get("FGELEV_PATH", "/usr/local/bin/fgelev")
FG_ROOT = os.environ.get("FG_ROOT", "")
FG_SCENERY = os.environ.get("FG_SCENERY", "")

# Seconds to wait before restarting fgelev after an unexpected exit.
RESTART_DELAY = 2


class FgelevProxy:
    """Owns a single fgelev process and multiplexes client connections.

    Clients register an event keyed by their request id. The dispatcher
    thread reads fgelev's stdout and wakes the matching client thread.
    If fgelev exits unexpectedly, pending queries are failed and fgelev
    is restarted automatically.
    """

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()   # guards _proc replacement
        self._stdin_lock = threading.Lock()  # guards writes to fgelev stdin
        self._pending: dict[str, threading.Event] = {}
        self._pending_lock = threading.Lock()
        self._responses: dict[str, str] = {}
        self._stopping = False

    # ------------------------------------------------------------------
    # fgelev lifecycle
    # ------------------------------------------------------------------

    def _start_fgelev(self) -> subprocess.Popen:
        """Launch a fresh fgelev process and return it."""
        env = os.environ.copy()
        if FG_ROOT:
            env["FG_ROOT"] = FG_ROOT
        if FG_SCENERY:
            env["FG_SCENERY"] = FG_SCENERY

        proc = subprocess.Popen(
            [FGELEV_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # suppress OSG plugin messages
            text=True,
            bufsize=1,
            env=env,
        )
        logger.info("fgelev started (pid %d)", proc.pid)
        return proc

    def _dispatcher_loop(self):
        """Read fgelev stdout and route responses to waiting client threads.

        Runs forever, restarting fgelev if it exits unexpectedly.
        """
        while not self._stopping:
            with self._proc_lock:
                proc = self._proc

            # Drain stdout until EOF (fgelev exit)
            for line in proc.stdout:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                ob_id, _ = line.split(":", 1)
                ob_id = ob_id.strip()

                with self._pending_lock:
                    event = self._pending.pop(ob_id, None)
                    if event is not None:
                        self._responses[ob_id] = line

                if event is not None:
                    event.set()
                else:
                    logger.warning("Response for unknown id %r — ignoring.", ob_id)

            if self._stopping:
                break

            rc = proc.wait()
            logger.warning("fgelev exited (rc=%d) — failing pending queries.", rc)

            # Unblock all waiting client threads so they return None
            with self._pending_lock:
                for ob_id, event in self._pending.items():
                    logger.error("Failing pending query id=%s (fgelev restarting)", ob_id)
                    event.set()
                self._pending.clear()

            # Restart fgelev after a short delay
            logger.info("Restarting fgelev in %ds...", RESTART_DELAY)
            time.sleep(RESTART_DELAY)
            new_proc = self._start_fgelev()
            with self._proc_lock:
                self._proc = new_proc

    def start(self):
        """Start fgelev and the dispatcher thread."""
        self._proc = self._start_fgelev()
        t = threading.Thread(
            target=self._dispatcher_loop,
            name="fgelev-dispatcher",
            daemon=True,
        )
        t.start()

    def stop(self):
        """Shut down fgelev and stop the dispatcher."""
        self._stopping = True
        with self._proc_lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.stdin.close()
            proc.wait(timeout=5)

    # ------------------------------------------------------------------
    # Query interface (called from client threads)
    # ------------------------------------------------------------------

    def query(self, ob_id: str, lon: str, lat: str) -> str | None:
        """Send one query to fgelev and return the raw response line.

        Blocks until fgelev responds. Returns None if fgelev has exited
        and the query could not be completed (client should retry or skip).
        """
        event = threading.Event()
        with self._pending_lock:
            self._pending[ob_id] = event

        with self._proc_lock:
            proc = self._proc

        if proc is None or proc.poll() is not None:
            # fgelev not running right now — dispatcher will restart it,
            # but this query arrived in the gap. Fail it immediately.
            with self._pending_lock:
                self._pending.pop(ob_id, None)
            logger.error("fgelev not running — dropping query id=%s", ob_id)
            return None

        try:
            with self._stdin_lock:
                proc.stdin.write(f"{ob_id} {lon} {lat}\n")
                proc.stdin.flush()
        except OSError as exc:
            logger.error("fgelev stdin write failed: %s", exc)
            with self._pending_lock:
                self._pending.pop(ob_id, None)
            return None

        # Block until the dispatcher wakes us (response received or fgelev died)
        event.wait()

        return self._responses.pop(ob_id, None)

    # ------------------------------------------------------------------
    # Client handling
    # ------------------------------------------------------------------

    def handle_client(self, conn: socket.socket, addr):
        logger.info("Client connected: %s", addr)
        try:
            client_in = conn.makefile("r")
            client_out = conn.makefile("w")

            for line in client_in:
                line = line.strip()
                if not line:
                    continue

                parts = line.split()
                if len(parts) != 3:
                    logger.warning("Malformed query from %s: %r", addr, line)
                    continue

                ob_id, lon, lat = parts
                logger.debug("REQ  %s → id=%s lon=%s lat=%s", addr, ob_id, lon, lat)
                response = self.query(ob_id, lon, lat)
                if response is None:
                    logger.error("No response for id=%s — dropping.", ob_id)
                    continue

                logger.debug("RESP %s ← %s", addr, response)
                client_out.write(response + "\n")
                client_out.flush()

        except OSError as exc:
            logger.debug("Client %s connection error: %s", addr, exc)
        finally:
            conn.close()
            logger.info("Client disconnected: %s", addr)

    # ------------------------------------------------------------------
    # Server loop
    # ------------------------------------------------------------------

    def serve(self, host: str, port: int):
        self.start()

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(10)
        logger.info("Listening on %s:%d", host, port)

        try:
            while True:
                conn, addr = server.accept()
                t = threading.Thread(
                    target=self.handle_client,
                    args=(conn, addr),
                    daemon=True,
                )
                t.start()
        except KeyboardInterrupt:
            logger.info("Shutting down.")
        finally:
            server.close()
            self.stop()


def main():
    parser = argparse.ArgumentParser(description="Multiplexing TCP proxy for fgelev")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    proxy = FgelevProxy()
    proxy.serve(args.host, args.port)


if __name__ == "__main__":
    main()

