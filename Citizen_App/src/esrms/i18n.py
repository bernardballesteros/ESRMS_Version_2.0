"""Interface text in the languages the app supports. No Flet code; tested in tests/test_i18n.py.

How to use: screens call app.tr("key") instead of writing the English text inline. To translate a
screen, move its strings into STRINGS["en"] and add the same keys to the other language.

A language is offered as selectable only when it has every key (tests enforce this). Text that is
stored or sent to dispatchers (emergency types, report descriptions) is never translated.
"""

LANGUAGES = {
    # code: (name shown in the list, selectable, note shown under it)
    "en": ("English", True, ""),
    "fil": ("Filipino", True, "Dashboard, menus and settings. SOS, radar, sign-in and account screens stay "
                              "in English until their translations are reviewed."),
    "ceb": ("Cebuano", False, "Not available yet: needs translation."),
}
DEFAULT = "en"

STRINGS = {
    "en": {
        # bottom navigation
        "nav.command": "COMMAND",
        "nav.announcement": "ANNOUNCEMENT",
        "nav.radar": "RADAR",
        # profile menu
        "menu.profile": "My profile",
        "menu.settings": "Settings",
        "menu.security": "Security & MFA",
        "menu.citizen": "ESRMS CITIZEN",
        "menu.barangay": "BARANGAY {name}",
        # dashboard
        "dash.title": "CITIZEN DASHBOARD",
        "dash.tagline": "SAFER COMMUNITY. STRONGER TOGETHER.",
        "dash.network_ready": "NETWORK READY",
        "dash.offline": "OFFLINE",
        "dash.live": "LIVE",
        "dash.sector": "SECTOR // {where}",
        "dash.location": "LOCATION",
        "dash.radar_active": "SAFETY RADAR ACTIVE",
        "dash.radar_offline": "OFFLINE - SOS IS SAVED ON PHONE, NOT SENT",
        "dash.services": "DISPATCH SERVICES",
        "dash.quick_act": "// QUICK-ACT",
        "dash.my_reports": "My reports & dispatcher status",
        "dash.link_live": "ESRMS LINK: REALTIME SYNCED",
        "dash.link_polling": "ESRMS LINK: POLLING",
        "dash.pending": "{n} SOS NOT SENT - saved on this phone only. Tap to retry or send by SMS.",
        "dash.fix_current": "CURRENT FIX, {age} AGO",
        "dash.fix_retry": "TAP TO RETRY",
        "dash.fix_acquiring": "ACQUIRING... {s} S",
        "dash.gps_acquiring": "ACQUIRING GPS LOCATION... {s} S",
        "dash.gps_denied": "GPS PERMISSION NOT GRANTED - MANUAL LOCATION",
        "dash.gps_blocked": "GPS BLOCKED IN SETTINGS - MANUAL LOCATION",
        "dash.gps_off": "DEVICE LOCATION IS OFF",
        "dash.gps_error": "NO FRESH GPS FIX - TAP TO RETRY",
        "dash.gps_standby": "GPS STANDBY - TAP TO LOCATE",
        # account settings
        "set.title": "Account Settings",
        "set.preferences": "App Preferences",
        "set.language": "Language",
        "set.theme": "Theme",
        "set.theme.dark": "Dark",
        "set.theme.light": "Light",
        "set.info": "Account Information",
        "set.account_type": "Account Type",
        "set.member_since": "Member Since",
        "set.version": "App Version",
        "set.unavailable": "Unavailable",
        "set.more": "More",
        "set.permissions": "Permissions & sign out",
        "set.permissions_sub": "Location, notifications, barangay",
        "set.offline": "No connection: showing the details saved on this phone.",
        "set.choose_language": "Choose language",
        "set.choose_theme": "Choose theme",
        "set.cancel": "Cancel",
        "set.apply": "Apply",
        "set.language_saved": "Language changed to {name}.",
        "set.theme_saved": "Theme changed to {name}.",
        "set.not_saved": "Applied, but the phone could not save it. It will reset when the app restarts.",
        "role.citizen": "Citizen",
        "role.responder": "Responder",
        "role.organization": "Organization",
        "role.admin": "Administrator",
        "role.guest": "Guest (quick access)",
    },
    "fil": {
        "nav.command": "COMMAND",
        "nav.announcement": "ANUNSYO",
        "nav.radar": "RADAR",
        "menu.profile": "Aking profile",
        "menu.settings": "Mga setting",
        "menu.security": "Seguridad at MFA",
        "menu.citizen": "MAMAMAYAN NG ESRMS",
        "menu.barangay": "BARANGAY {name}",
        "dash.title": "DASHBOARD NG MAMAMAYAN",
        "dash.tagline": "LIGTAS NA KOMUNIDAD. MAS MATATAG KUNG SAMA-SAMA.",
        "dash.network_ready": "HANDA ANG NETWORK",
        "dash.offline": "WALANG KONEKSYON",
        "dash.live": "LIVE",
        "dash.sector": "SEKTOR // {where}",
        "dash.location": "LOKASYON",
        "dash.radar_active": "AKTIBO ANG SAFETY RADAR",
        "dash.radar_offline": "OFFLINE - ANG SOS AY NAKA-SAVE SA TELEPONO, HINDI PA NAIPADALA",
        "dash.services": "MGA SERBISYO NG DISPATCH",
        "dash.quick_act": "// MABILIS NA AKSYON",
        "dash.my_reports": "Aking mga report at status ng dispatcher",
        "dash.link_live": "ESRMS LINK: REALTIME NA NAKA-SYNC",
        "dash.link_polling": "ESRMS LINK: PANA-PANAHONG TINITINGNAN",
        "dash.pending": "{n} SOS ANG HINDI PA NAIPADALA - naka-save lang sa teleponong ito. Pindutin para "
                        "subukan ulit o ipadala sa SMS.",
        "dash.fix_current": "KASALUKUYANG LOKASYON, {age} ANG NAKALIPAS",
        "dash.fix_retry": "PINDUTIN PARA SUBUKAN ULIT",
        "dash.fix_acquiring": "KINUKUHA... {s} S",
        "dash.gps_acquiring": "KINUKUHA ANG LOKASYON NG GPS... {s} S",
        "dash.gps_denied": "WALANG PAHINTULOT SA GPS - MANWAL NA LOKASYON",
        "dash.gps_blocked": "NAKA-BLOCK ANG GPS SA SETTINGS - MANWAL NA LOKASYON",
        "dash.gps_off": "NAKA-OFF ANG LOKASYON NG TELEPONO",
        "dash.gps_error": "WALANG BAGONG GPS FIX - PINDUTIN PARA SUBUKAN ULIT",
        "dash.gps_standby": "NAKAHANDA ANG GPS - PINDUTIN PARA HANAPIN",
        "set.title": "Mga Setting ng Account",
        "set.preferences": "Mga Kagustuhan sa App",
        "set.language": "Wika",
        "set.theme": "Tema",
        "set.theme.dark": "Madilim",
        "set.theme.light": "Maliwanag",
        "set.info": "Impormasyon ng Account",
        "set.account_type": "Uri ng Account",
        "set.member_since": "Miyembro Mula",
        "set.version": "Bersyon ng App",
        "set.unavailable": "Hindi makuha",
        "set.more": "Iba pa",
        "set.permissions": "Mga pahintulot at pag-sign out",
        "set.permissions_sub": "Lokasyon, notipikasyon, barangay",
        "set.offline": "Walang koneksyon: ipinapakita ang mga detalyeng naka-save sa teleponong ito.",
        "set.choose_language": "Pumili ng wika",
        "set.choose_theme": "Pumili ng tema",
        "set.cancel": "Kanselahin",
        "set.apply": "Ilapat",
        "set.language_saved": "Napalitan ang wika: {name}.",
        "set.theme_saved": "Napalitan ang tema: {name}.",
        "set.not_saved": "Nailapat, pero hindi ito na-save ng telepono. Babalik ito sa dati kapag binuksan ulit "
                         "ang app.",
        "role.citizen": "Mamamayan",
        "role.responder": "Responder",
        "role.organization": "Organisasyon",
        "role.admin": "Administrador",
        "role.guest": "Bisita (mabilis na access)",
    },
}


def selectable(code):
    return code in LANGUAGES and LANGUAGES[code][1] and code in STRINGS


def normalize(code):
    """A saved language code, or the default if it is unknown or no longer selectable."""
    return code if selectable(code) else DEFAULT


def tr(key, lang=DEFAULT, **values):
    """Text for `key` in `lang`; English if that language lacks it; the key itself if nobody has it."""
    text = STRINGS.get(lang, {}).get(key) or STRINGS[DEFAULT].get(key) or key
    return text.format(**values) if values else text
