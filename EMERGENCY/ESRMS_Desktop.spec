# PyInstaller spec for the one-file ESRMS desktop build. Build with: packaging\build.ps1
# Bundles templates/static, the public .env.example defaults, and build/bundled.env: the project's .env
# (including SUPABASE_SERVICE_ROLE_KEY, at the owner's request) minus SECRET_KEY.
# WARNING: anyone with the EXE can extract it. Rotate the key in Supabase if the EXE leaks.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

os.makedirs("build", exist_ok=True)
with open(".env", encoding="utf-8") as src, open("build/bundled.env", "w", encoding="utf-8") as dst:
    dst.writelines(line for line in src if not line.strip().startswith("SECRET_KEY"))

datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
    (".env.example", "."),
    ("build/bundled.env", "."),
]
binaries = []
hiddenimports = collect_submodules("app") + ["config"]
for pkg in ("webview", "folium", "branca", "fpdf", "supabase", "supabase_auth", "supabase_functions",
            "postgrest", "storage3", "realtime", "pywebpush", "py_vapid", "http_ece", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "matplotlib", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ESRMS_Desktop",
    icon="packaging/esrms.ico",
    console=False,  # --windowed
    upx=False,
    runtime_tmpdir=None,
)
