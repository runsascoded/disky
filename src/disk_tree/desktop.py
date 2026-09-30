"""Native macOS window around the disk-tree Flask server (pywebview).

Runs the existing Flask app (`disk_tree.server.app`) on a loopback port in a
daemon thread, then opens a WKWebView window pointed at it. Bundled as
`disk-tree.app` (see `packaging/macos/`), this gives scans a stable TCC
identity — prompts and Full Disk Access show "disk-tree", not "python3.13" —
and one FDA grant covers the app's child `gfind` reads. See
`specs/macos-app.md`.

Run directly for development: `disk-tree-app` (needs the `app` extra:
`uv sync --extra app`). The window and the CLI/server share the same config
and DB, so a scan started here shows up everywhere.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time


def _debug_enabled() -> bool:
    """Whether to open the WKWebView with the Web Inspector enabled.

    With it on, right-click → *Inspect Element* opens Safari's Web Inspector
    (console, network, sources) against the embedded UI — the only way to read
    a JS stack out of the native window, since WebKit's errors (e.g. "The
    string did not match the expected pattern") differ from Chrome's and don't
    reproduce under the dev-server CIC. Default: on from source (dev), off in
    the frozen bundle unless `DISK_TREE_APP_DEBUG` is set truthy.
    """
    env = os.environ.get('DISK_TREE_APP_DEBUG')
    if env is not None:
        return env.strip().lower() not in ('', '0', 'false', 'no')
    return not getattr(sys, 'frozen', False)


def _free_loopback_port() -> int:
    """An ephemeral port the OS just handed us, bound to loopback only."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _wait_until_up(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"Flask server did not come up on 127.0.0.1:{port} within {timeout}s")


def main() -> None:
    import webview

    from disk_tree.server import STATIC_DIR, app

    if not STATIC_DIR:
        # The window would render the API's SPA-fallback 404s; better to say so.
        raise SystemExit(
            "No bundled UI found. Build it first: `cd ui && pnpm build` "
            "(or install a wheel built with the UI included)."
        )

    port = _free_loopback_port()

    def serve() -> None:
        # A real WSGI server, not Flask's dev server: the latter prints its
        # banner but never binds under PyInstaller. waitress is pure-Python,
        # threaded, and freezes cleanly. Threads cover concurrent data requests
        # plus the long-lived progress SSE stream.
        from waitress import serve as _serve
        _serve(app, host='127.0.0.1', port=port, threads=8)

    threading.Thread(target=serve, daemon=True, name='disk-tree-flask').start()
    _wait_until_up(port)

    # Headless self-check (CI / frozen-bundle verification): confirm the
    # embedded server is up and serving the API, then exit without a window.
    if os.environ.get('DISK_TREE_APP_SMOKE'):
        import json
        import urllib.request
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/scans', timeout=10) as r:
            n = len(json.load(r))
        print(f'SMOKE OK: server up on {port}, /api/scans returned {n} scans')
        return

    webview.create_window(
        'disk-tree',
        f'http://127.0.0.1:{port}/',
        width=1200,
        height=820,
        min_size=(720, 480),
    )
    # `debug=True` enables the WKWebView Web Inspector (right-click → Inspect).
    webview.start(debug=_debug_enabled())


if __name__ == '__main__':
    main()
