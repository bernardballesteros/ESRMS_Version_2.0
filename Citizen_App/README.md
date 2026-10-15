# ESRMS Citizen Emergency App

Android app (Python + [Flet](https://flet.dev)) that lets citizens send SOS reports to the ESRMS
911 dispatchers and read official announcements. It uses the **same Supabase project** as the
ESRMS Dispatcher Dashboard (`..\EMERGENCY`, Flask).

```
Citizen App ──insert (RLS)──► emergency_incidents ──► Dispatcher Dashboard (map + incident list)
Dispatcher Dashboard ──► announcements ──citizen_announcements()──► Citizen App (ANNOUNCEMENT tab)
                          announcement_feed (Realtime counter) ──────► Citizen App live refresh
```

## Why Flet

The dispatcher dashboard is a Flask **web** app; its HTML UI can't become an APK. The citizen
app is a separate native-style Python UI. Flet was chosen because `flet build apk` runs
natively on Windows (no WSL; Kivy/Buildozer would need Linux) and has maintained GPS
(`flet-geolocator`) and permission (`flet-permission-handler`) extensions.

## Security model

* The app holds only the **anon (public) key** (`src/config.json`). `esrms/config.py` refuses to
  start if a service-role key is put there. The service-role key stays in the Flask server.
* Citizens sign in with Supabase Auth (same accounts as the web portal's citizen registration).
* `emergency_incidents` RLS (from `003_dispatch.sql`): a citizen may insert a `new` report as
  themselves and read only their own reports. No update/delete. Dispatcher columns
  (acknowledged/resolved/archived/updated_by) must be empty.
* `004_citizen_app.sql` adds a guard trigger: priority and verification are cleared on citizen
  inserts, max 3 reports / 10 min per account, same type twice within 60 s is rejected.
* Announcements stay closed to the public key. The app reads them through
  `citizen_announcements()`, a `security definer` function that applies the same window and
  audience rules as Flask's `/api/announcements/public` (all-citizen + own-barangay only).
* Retry-safe submission: each report carries a `client_request_id` (unique). If the network
  drops after sending, a retry returns the stored report instead of creating a second one.
  Success is shown only after the database returns the stored row.

## No signal: what the app does and does not do

**GPS is not a communication channel.** GNSS satellites only let the phone work out where it is.
The app has no satellite uplink and never claims one. With no cell or Wi-Fi network, nothing leaves
the phone.

What is implemented:

* **Fresh position, no internet needed.** Opening the SOS screen and every Retry GPS press call
  `CitizenApp.locate()`, which makes new high-accuracy requests through `flet-geolocator` (the
  phone's GNSS receiver on Android). "Get current position" may answer with a stored position, so
  `esrms/gps.py` checks the fix time of every answer and keeps asking until one is fresh or
  `gps_timeout_seconds` (default 30, `config.json`) runs out, with a live "Acquiring GPS location...
  12 s of 30 s". Without internet there is no assistance data, so a cold first fix can take minutes
  under open sky; while the SOS screen is open the app starts a new run every 30 s.
* **Current vs old.** A fix is "current" only if its own timestamp is under 2 minutes old
  (`report.FRESH_SECONDS`). Otherwise the screen says "Unable to obtain a fresh GPS fix" and shows any
  old stored position separately with its age. That old position is attached to a report only if the
  citizen presses "Use old position", and the dispatcher then sees it marked LAST KNOWN with its time.
  A fix that was fresh earlier in the session stays attached and is labelled with its age.
* **Interruptions.** Permission denied/blocked and Location turned off each get their own message
  and a settings shortcut. When the app returns to the foreground it re-checks permissions and
  starts a new request; a request that never answers is abandoned at the timeout.
* **Desktop testing.** `flet run` on a PC uses Windows' Wi-Fi/IP position, not GPS. The Windows
  geolocator plugin also reports fix times in seconds where milliseconds are expected (every
  position looks like January 1970); `gps.fix_time` rescales that on Windows only.
  `scripts\gps_check.py` prints what the real location API of the machine returns.
* **Never lost.** If sending fails (no reply, expired session, server error) the report goes to the
  outbox (`esrms/outbox.py`, stored in SharedPreferences, read back after every write). It survives
  closing the app and restarting the phone. If the phone cannot store it, the app says "NOT SENT AND
  NOT SAVED".
* **Saved is not received.** A saved report shows "NOT SENT - saved on this phone only" in amber/red
  on the SOS screen, the dashboard banner and My reports. The green "Report received by ESRMS"
  screen with an `INC-` number is only ever built from the row the database returned.
* **Automatic sync.** While the app is open it retries every 30 s and as soon as the OS reports a
  connection, restoring the session first if the app was started offline. The same
  `client_request_id` is reused, so a report can't be stored twice. No schema change: the real
  written time and position time are added to `description`, because `created_at` is the arrival time.
* **Offline start.** The signed-in citizen's id, name and phone (no tokens) are kept on the phone, so
  an SOS can be written when the app is opened with no signal.
* **Other channels.** "Send by SMS" opens the phone's own messaging app with the coordinates filled
  in (SMS needs a weak cell signal, not data). The app cannot know whether an SMS was delivered and
  does not mark anything as sent. Set `sos_sms_number` in `config.json` only to a number an ESRMS
  operator really reads; left empty, the citizen chooses the recipient.

Limits to know about:

* Sync runs **only while the app is open**. Background delivery needs an Android foreground service
  or WorkManager, which Flet does not provide without native code.
* A report by a signed-out or different account is never sent; sign-out is blocked while an SOS is unsent.
* Offline GPS and the connectivity trigger are exercised with fakes in `tests/` and
  `scripts/ui_smoke.py`. They still need a field test on real phones (airplane mode, outdoors).

### Satellite SOS / messaging (checked October 2026, not integrated)

| Option | Works in the Philippines? | Hardware / plan | Can this app use it? |
| --- | --- | --- | --- |
| Apple Emergency SOS via satellite | **No.** Philippines is not on [Apple's list](https://support.apple.com/en-ph/101573) | iPhone 14+, free for 2 years after activation | No public API; iOS build of this app doesn't exist |
| Google Pixel Satellite SOS (Skylo + Garmin Response) | **No.** Not on [Google's list](https://support.google.com/pixelphone/answer/15254448) (US, Canada, Europe, Australia) | Pixel 9 and later except 9a | No; built into Google Messages / emergency dialer |
| Android `SatelliteManager` | n/a | Android 15+/16 | Sending is a system/carrier API; a normal app can at most observe state |
| Globe + Starlink Direct to Cell | **Reported launched in 2026, not verified by us.** Press and a third-party summary of Globe's page describe SMS plus data for selected apps, Android LTE phones first, promos around PHP 99 / 30 days and PHP 299 / 90 days. Globe's own pages could not be opened to confirm | Globe SIM with the promo activated *before* leaving coverage, clear sky | No integration needed: the phone just regains a network. SMS should work; whether this app's HTTPS traffic is allowed over the satellite link is **unknown** and must be tested with Globe |
| Garmin inReach (Iridium) | Iridium coverage is global; local rescue coordination via Garmin Response is not verified for the Philippines | Device plus subscription (US list prices about USD 8-30 / month plus activation) | Separate device. Garmin's IPC API is for professional/enterprise accounts and would feed ESRMS server-side, not this app |
| 406 MHz PLB (Cospas-Sarsat) | Global system; Philippine beacon registration and response not verified here | One-off beacon purchase, no subscription | No; standalone beacon |

Before building any satellite feature: confirm with Globe (service area, app allowlist, cost) and
test on a real SIM. Until then the app tells the citizen plainly that the SOS has not reached ESRMS.

## Account pages (profile button → My profile / Settings / Security & MFA)

Everything is done as the signed-in citizen with the public key; Supabase decides whose account it is
from the session token, so no page can read or change another citizen.

* **My Profile / Edit Profile** (`ui/account.py`, rules in `esrms/account.py`): name, phone and
  address are saved to the account's Supabase Auth metadata (what the SOS form reads) and name/phone
  also to the citizen's own `public.users` row (what the dashboard reads; own-row RLS). An email
  change goes through Supabase Auth, which emails a confirmation link; the address changes only after
  it is opened.
* **Profile picture**: stored as `esrms-media/avatars/<user id>.<ext>`, the same object the dashboard
  uses (PNG/JPEG/WebP, 2 MB). The app needs storage policies for this: run
  `supabase/007_citizen_avatars.sql` once in the Supabase SQL Editor. Until then the app says pictures
  are not enabled and saves nothing.
* **Change password**: the current password is checked by Supabase Auth, then the new one is set
  through Supabase Auth. Google-only accounts get a link to Google's security page instead of a form.
* **MFA**: Supabase Auth TOTP. Enabling starts a real enrolment (setup key, authenticator link, QR)
  and counts only after Supabase verifies a 6-digit code. Once enabled, sign-in on this app stops at
  a code screen until Supabase verifies a code for that session. Disabling needs a current code.
  Status always comes from the factor list Supabase returns.
* **MFA recovery**: the app has no recovery codes and cannot remove a factor without a code. An
  administrator removes the factor (dashboard, or Supabase → Authentication → Users).
* The MFA step is enforced by the app at sign-in. To have the database refuse reports from sessions
  that skipped it, add an `aal2` check to the RLS policies; that is not done here because it would
  also block an SOS from a citizen who cannot reach their authenticator.

### Safety Radar (RADAR tab)

A public-awareness view of the dashboard's High-Risk Area Detection. The app does no risk analysis.

```
emergency_incidents → ESRMS Analytics (EMERGENCY/app/risk.py, Flask, service role)
   → EMERGENCY/app/public_risk.py removes everything private
   → public_risk_areas + public_risk_feed (migration 012, read-only for signed-in citizens)
   → Citizen App: esrms/radar.py measures distances on the phone, ui/radar_view.py draws them
```

* **Setup:** run `EMERGENCY/supabase/migrations/012_public_risk_areas.sql`, then publish once with
  `cd ..\EMERGENCY; .venv\Scripts\python scripts\publish_risk_areas.py` (`--dry-run` shows what would be
  published). After that the dashboard republishes hourly while a dispatcher has Analytics open.
  `RISK_PUBLIC_ENABLED=0` on the dashboard stops publishing.
* **What is published:** per zone, an id, a level (low / moderate / high), the centre of its 1 km grid
  cell, a radius, a fixed sentence and a date. Never incident points, counts, types, codes, people,
  responders or organization status. Areas with fewer than 3 located incidents are not published, and
  nothing is published while Analytics reports "insufficient historical data" (the app then shows
  "Not enough data", never "low risk").
* **Location:** the app downloads the whole public list and compares it with the phone's position
  locally; the position is not sent for this and is not shown. Without location the citizen gets the
  general map; permission is requested only when they press ENABLE LOCATION.
* **Refresh:** when the tab opens and every 5 minutes while it is open (one tiny feed-version check,
  full download only when the version changed). The last copy is kept on the phone for offline use.
* **Animation:** the cyan sweep is decorative and always on; it reads and changes no data.

### Google sign-in

"Continue with Google" on the sign-in screen uses Supabase Auth's Google provider with PKCE
(`esrms/oauth.py`). No Google secret is in the app; it stays in the Supabase dashboard.

* **Phone (APK):** the system browser opens Google's account chooser; Supabase then redirects to
  `ph.esrms.citizen://auth/callback`, which Android hands back to the app (deep link declared in
  `pyproject.toml` → `[tool.flet.android.deep_linking]`). The app exchanges the code for a session,
  then opens the dashboard (or the MFA code screen). The one-time PKCE verifier is kept on the phone
  for up to 15 minutes so it still works if Android closed the app meanwhile, and is deleted after use.
* **PC (`flet run`):** a one-shot server on `http://localhost:53682/callback` catches the redirect.
* **Supabase → Authentication → URL Configuration → Redirect URLs** must contain both
  `ph.esrms.citizen://auth/callback` and `http://localhost:53682/callback`. If the phone URL is
  missing, Supabase sends the browser to the Site URL instead and the app never gets the result.
* A first Google sign-in creates the Supabase user (the `on_auth_user_created` trigger adds its
  `public.users` row once). The app then marks the account's metadata `role: citizen`, like the
  registration form does, unless a role is already set. Signing in with Google using the email of an
  existing account uses that account; Supabase links the identity, so no second user is created.

### Account Settings (language, theme, account information)

`ui/settings.py`, opened from the profile menu's **Settings**. The old permissions / barangay /
sign-out dialog is still there, under "Permissions & sign out" on the same page.

* **Language** (`esrms/i18n.py`): screens call `app.tr("key")`. English and Filipino are selectable;
  Filipino covers the dashboard, bottom navigation, profile menu and Account Settings, and the rest
  stays in English until those strings are moved into the table and translated. A language is only
  selectable when it has every key (`tests/test_i18n.py`). Text sent to dispatchers is never translated.
* **Theme** (`ui/theme.py`): dark (default) and light palettes; `t.apply()` swaps the colour names
  every screen reads. `SMOKE_THEME=light` makes `scripts\ui_smoke.py` render every screen in light.
* Both are saved in SharedPreferences (`esrms.language`, `esrms.theme`) and restored at start.
* **Account type** is the role on the account's own `public.users` row (citizens cannot change it),
  **Member since** is the account's Supabase Auth `created_at`, **App version** is
  `esrms.__version__`, which a test keeps equal to `[project].version` in `pyproject.toml`
  (change both for a release). The last values seen are kept on the phone for offline use.

## 1. Database setup (once)

1. Supabase Dashboard → project `speawbhwvwekhdltcauz` → **SQL Editor**.
2. Paste and run `EMERGENCY/supabase/migrations/004_citizen_app.sql` (additive, safe to re-run;
   it ends with `notify pgrst, 'reload schema'` so the API sees the new column and function at once).
   It must run after `schema.sql`, `002_announcements.sql` and `003_dispatch.sql` from the same folder.
3. Check from this folder (read-only):
   ```powershell
   .venv\Scripts\python scripts\check_database.py
   ```
4. Optional: Authentication → Sign In / Providers → enable **Anonymous sign-ins** if you want the
   "Report without an account" button to work. It is currently **disabled**, and the app says so.

## 2. Run on the PC (development)

```powershell
cd C:\Users\berna\IdeaProjects\Citizen_App
py -3.13 -m venv .venv                     # already created
.venv\Scripts\python -m pip install -r requirements.txt
copy src\config.example.json src\config.json   # already created with the anon key
.venv\Scripts\flet run src\main.py
```

## 3. Tests

```powershell
.venv\Scripts\python -m pytest              # 72 unit tests: report rules, fresh-fix rules, offline outbox, account/MFA, language/theme, backend (mocked), announcements
.venv\Scripts\python scripts\account_smoke.py  # My Profile, Security & MFA and Account Settings against a fake Supabase
.venv\Scripts\python scripts\gps_check.py   # real location API of this machine: open SOS, press Retry GPS twice
.venv\Scripts\python scripts\ui_smoke.py    # renders every screen; sends an SOS through a fake offline server
# After the migration, with a TEST citizen account (creates one [TEST] incident):
$env:ESRMS_TEST_EMAIL="..."; $env:ESRMS_TEST_PASSWORD="..."
.venv\Scripts\python scripts\e2e_live.py
```

Dispatcher side: open the dashboard (`cd ..\EMERGENCY; .venv\Scripts\python main.py`), sign in as
an organization/dispatcher. The `[TEST]` incident appears in the incident list and on the map at
the submitted coordinates. Publish an announcement → it appears in the app's ANNOUNCEMENT tab
within a second (Realtime) or 30 s (polling fallback). Drafts and future scheduled posts stay hidden.
Clean up test incidents: `cd ..\EMERGENCY; .venv\Scripts\python scripts\simulate_incident.py --cleanup`.

Never send test reports to a real emergency service; all test rows are marked `[TEST]`.

## 4. Build and install the APK (Windows)

1. Windows Settings → System → For developers → turn on **Developer Mode** (Flutter needs symlinks),
   and install Git for Windows (`winget install --id Git.Git -e`); Flutter will not run without it.
2. Build (first run downloads Flutter, a JDK and the Android SDK, several GB, ~40 min; later builds
   take a few minutes):
   ```powershell
   cd C:\Users\berna\IdeaProjects\Citizen_App
   .venv\Scripts\flet build apk --yes --splash-color "#05090F" --splash-dark-color "#05090F"
   ```
   Output: `build\apk\esrms-citizen.apk` (~135 MB, all CPU types; add `--split-per-abi` for smaller
   per-CPU files). The launcher icon is `src\assets\icon.png` (`scripts\make_icon.py` redraws it).
3. Install: copy the APK to the phone and open it (allow "install unknown apps"), or with USB
   debugging on: `adb install -r build\apk\esrms-citizen.apk` (adb is in the Android SDK that
   `flet build` installed, under `%USERPROFILE%\Android\sdk\platform-tools`).
4. On the phone: first launch → permission explanations → sign in → hold SOS 3 s.

The APK is debug-signed by default. For distribution, create a keystore and pass
`--android-signing-key-store` / `--android-signing-key-alias` to `flet build apk`.

## Permissions

| Permission | When | If denied |
| --- | --- | --- |
| Location (foreground, fine+coarse) | Explained on first launch, requested on "Allow" | Manual landmark/coordinates entry; report marked manual / unverified; never a fake fix |
| Background location | **Not requested** – GPS is read only while the app is open | – |
| Notifications (Android 13+) | Explained on first launch | App fully usable; Settings shows how to enable |

Choices are remembered (`SharedPreferences`); onboarding shows once. Settings (profile button)
shows both statuses, can re-ask, and opens the phone's app settings.

**Push notifications are not implemented**: ESRMS has no push service (FCM) configured. New
announcements show as in-app alerts while the app is open (Realtime). Adding FCM needs a Firebase
project and a server-side sender in the Flask app.

## Layout

```
src/main.py                    entry point (flet run / flet build)
src/config.json                Supabase URL + anon key (git-ignored)
src/esrms/catalog.py           emergency categories + incident types (edit here)
src/esrms/report.py            builds/validates the emergency_incidents row
src/esrms/gps.py               fresh-fix acquisition: rejects stored positions, timeout, reasons
src/esrms/outbox.py            SOS reports saved on the phone until the database confirms them
src/esrms/backend.py           Supabase Auth + REST client (httpx)
src/esrms/realtime.py          Realtime listener for announcement_feed
src/esrms/announcements.py     announcement display rules
src/esrms/ui/                  screens: onboarding, sign-in, COMMAND, SOS flow, ANNOUNCEMENT, settings
../EMERGENCY/supabase/migrations/004_citizen_app.sql   database changes this app needs
scripts/                       check_database, ui_smoke, e2e_live
tests/                         pytest unit tests
```
