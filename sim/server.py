"""Run an ASGI app in a daemon thread with uvicorn (for hermetic tests and `make sim`)."""

from __future__ import annotations

import socket
import threading
import time

import uvicorn


class ServerThread:
    def __init__(self, app, host: str = "127.0.0.1", port: int = 0):
        self.app, self.host, self.port = app, host, port
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def start(self) -> ServerThread:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(128)
        self.port = sock.getsockname()[1]
        cfg = uvicorn.Config(
            self.app, host=self.host, port=self.port, log_level="warning", timeout_graceful_shutdown=2
        )
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline or not self._thread.is_alive():
                raise RuntimeError("sim server failed to start")
            time.sleep(0.01)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(5)
        self._server = self._thread = None

    def __enter__(self) -> ServerThread:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
