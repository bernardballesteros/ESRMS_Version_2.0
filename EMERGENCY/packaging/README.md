# ESRMS desktop build

Build: `powershell -ExecutionPolicy Bypass -File packaging\build.ps1` → `dist\ESRMS_Desktop.exe` (single file, ~44 MB).

- Entry point: `desktop.py` (Flask on 127.0.0.1:5000, falling back to a free port; pywebview window).
- Spec: `ESRMS_Desktop.spec`. Icon: `packaging/esrms.ico`, generated from `app/static/img/emblem.svg` by `make_icon.py`.
- Bundled config: only `.env.example` (public Supabase URL + anon key). The private `.env` is never bundled.
- Per-user config: `%LOCALAPPDATA%\ESRMS\.env` (or a `.env` next to the EXE). Put `SUPABASE_SERVICE_ROLE_KEY`,
  `ADMIN_EMAILS`, Resend/VAPID keys there on machines that need admin features or notifications.
- Data: `%LOCALAPPDATA%\ESRMS\` holds `logs\esrms.log`, `secret_key` (session signing key, generated on first run)
  and `webview\` (WebView2 profile: cookies/sign-ins). Uploads go to Supabase Storage, never to local disk.
- Requires Microsoft Edge WebView2 Runtime (preinstalled on Windows 10/11).
- Supabase Auth → URL Configuration must allow `http://127.0.0.1:5000/**` for email links and Google sign-in.
