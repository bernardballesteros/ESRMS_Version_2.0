"""ESRMS desktop launcher: runs the Flask app on 127.0.0.1 and shows it in a pywebview window.

Used as the entry point of the one-file Windows build (ESRMS_Desktop.spec). It also runs from
source: `python desktop.py`.

Configuration is read, in order of priority, from:
  1. real environment variables
  2. %LOCALAPPDATA%\\ESRMS\\.env       (per-user config; put private keys such as SUPABASE_SERVICE_ROLE_KEY here)
  3. .env next to ESRMS_Desktop.exe
  4. the bundled bundled.env          (the build machine's .env minus SECRET_KEY, incl. the service role key)
  5. the bundled .env.example         (public defaults)
The service role key is embedded at the owner's request so installs work without setup.
"""
import ctypes
import logging
import logging.handlers
import os
import secrets
import socket
import sys
import threading
import time
import urllib.request

APP_NAME = "ESRMS"
PREFERRED_PORT = 5000  # Supabase auth redirect URLs are allow-listed for this port
FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
EXE_DIR = os.path.dirname(sys.executable) if FROZEN else BUNDLE_DIR
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
LOG_FILE = os.path.join(DATA_DIR, "logs", "esrms.log")

log = logging.getLogger("esrms.desktop")


def message_box(text, title=APP_NAME, error=True):
    flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000)  # MB_SETFOREGROUND
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    # --windowed builds have no console; anything that writes to stdout/stderr must not crash.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if not FROZEN:
        root.addHandler(logging.StreamHandler())


def single_instance():
    """Hold a named mutex for the life of the process; False if ESRMS is already running."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\ESRMS_Desktop_SingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def load_configuration():
    from dotenv import load_dotenv

    # load_dotenv never overrides a value that is already set, so the first file wins.
    for path in (os.path.join(DATA_DIR, ".env"), os.path.join(EXE_DIR, ".env")):
        if os.path.isfile(path):
            load_dotenv(path, override=False)
            log.info("Loaded configuration from %s", path)
    for name in ("bundled.env", ".env.example"):
        defaults = os.path.join(BUNDLE_DIR, name)
        if os.path.isfile(defaults):
            load_dotenv(defaults, override=False)

    # A stable per-installation session key, so sign-ins survive restarts. Never the example placeholder.
    if os.environ.get("SECRET_KEY", "") in ("", "change-this-to-a-long-random-string", "dev-change-me"):
        key_file = os.path.join(DATA_DIR, "secret_key")
        if not os.path.isfile(key_file):
            with open(key_file, "w") as f:
                f.write(secrets.token_hex(32))
        with open(key_file) as f:
            os.environ["SECRET_KEY"] = f.read().strip()

    if not os.environ.get("APP_BASE_URL"):
        os.environ.pop("APP_BASE_URL", None)
    log.info("Supabase URL configured: %s; service role key present: %s",
             bool(os.environ.get("SUPABASE_URL")), bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY")))


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def choose_port():
    if port_free(PREFERRED_PORT):
        return PREFERRED_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log.warning("Port %d is in use; using %d instead. Email/OAuth links that point back to this "
                "app only work if this port is allowed in Supabase's redirect URLs.", PREFERRED_PORT, port)
    return port


class ServerThread(threading.Thread):
    def __init__(self, app, port):
        super().__init__(name="esrms-flask", daemon=True)
        from werkzeug.serving import make_server

        self.server = make_server("127.0.0.1", port, app, threaded=True)

    def run(self):
        log.info("Flask server listening on http://127.0.0.1:%d", self.server.server_port)
        self.server.serve_forever()

    def stop(self):
        log.info("Stopping Flask server")
        self.server.shutdown()
        self.server.server_close()


def wait_until_ready(url, thread, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not thread.is_alive():
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    setup_logging()
    log.info("Starting ESRMS desktop (frozen=%s, cwd=%s)", FROZEN, os.getcwd())

    mutex = single_instance()
    if mutex is None:
        message_box("ESRMS is already running.", error=False)
        return 0

    try:
        load_configuration()
        # Imported after the configuration is loaded: config.Config reads the environment at import time.
        from app import create_app
        from app.delivery import start_worker

        app = create_app()
        port = choose_port()
        server = ServerThread(app, port)
        server.start()
        start_worker(app)
    except Exception as exc:
        log.exception("ESRMS failed to start")
        message_box(f"ESRMS could not start:\n\n{exc}\n\nDetails: {LOG_FILE}")
        return 1

    url = f"http://127.0.0.1:{port}/"
    if not wait_until_ready(url + "static/img/emblem.svg", server):
        log.error("Flask server did not become ready")
        message_box(f"The ESRMS server did not start.\n\nDetails: {LOG_FILE}")
        return 1

    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True  # analytics CSV/PDF exports
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.create_window("ESRMS — Emergency Response and Safety Management System", url,
                          width=1400, height=900, min_size=(1000, 650))
    try:
        # A persistent profile keeps sign-ins, cookies and settings between launches.
        webview.start(private_mode=False, storage_path=os.path.join(DATA_DIR, "webview"))
    except Exception as exc:
        log.exception("Desktop window failed")
        message_box("The ESRMS window could not open. Microsoft Edge WebView2 Runtime is required "
                    f"(https://go.microsoft.com/fwlink/p/?LinkId=2124703).\n\n{exc}\n\nDetails: {LOG_FILE}")
        return 1
    finally:
        server.stop()
    log.info("ESRMS closed")
    return 0


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    # The notification worker is a daemon thread; exit without waiting on its poll sleep.
    os._exit(code)
