# ESRMS — Emergency SOS Response Management System

## Overview

ESRMS is a web-based command platform that connects citizens, emergency response organizations,
dispatchers and hospitals in one system. Citizens report emergencies (with GPS location and
photos) through the companion Citizen App; ESRMS receives each report in real time, helps
dispatchers route it to the nearest suitable response organization, tracks the incident until it
is resolved, and hands patients off to receiving hospitals. It also gives administrators
analytics, high-risk area detection and a public announcement channel.

The system runs as a Flask web application backed by Supabase (PostgreSQL, authentication,
storage and realtime), and is also packaged as a one-file Windows desktop application
(`ESRMS_Desktop.exe`).

## Users and Roles

| Role | What they do |
|------|--------------|
| **Citizen** | Reports emergencies, receives announcements and sees public safety zones (via the Citizen App). |
| **Organization / Dispatcher** | Staff of EMS, fire, police, barangay response centers or other agencies. Monitor the live incident queue, update incident status, manage hotlines and responders. |
| **Hospital** | Receives incoming patient reports from dispatchers and manages them through arrival and handoff in the Hospital Portal. |
| **Administrator** | Manages user accounts, approves MFA resets, publishes announcements and risk areas, and views system-wide analytics. |

## Core Modules

### 1. Dispatch Center
- Live incident queue and interactive map of reported emergencies.
- Incident lifecycle: **New → Acknowledged → Dispatched → Units En Route → On Scene → Resolved** (or Cancelled).
- Organization-based dispatch: suggests the nearest registered base (EMS base, fire station, police
  station, barangay response center, hospital) by distance from the incident location.
- Dispatcher duty status (On Duty / On Break / Off Duty) — only on-duty dispatchers receive new incidents.
- Incident history, archiving and restore; responder hotline management.

### 2. Hospital Signal & Hospital Portal
- Dispatchers send a patient report to a hospital directly from an incident.
- Hospitals track it: **Sent → Acknowledged → Accepted (preparing) → Arrived → Handoff → Completed**,
  or mark it *Unable* with a required reason.
- Trauma bay tracking, hospital registration and a signal archive.

### 3. Analytics & Reporting
- KPIs per reporting period: total incidents, resolved, resolution rate, pending, in progress, cancelled.
- Charts by status and category, filterable incident records.
- Export reports to **CSV** and **PDF**, clearly marked when truncated.

### 4. High-Risk Area Detection
- Groups incidents into 1 km grid cells to show where emergencies concentrate.
- Areas are named after the nearest organization base.
- Administrators can publish a privacy-safe version to the Citizen App's **Safety Radar**:
  only a risk level and message, drawn at the grid-cell center, with small areas hidden so
  individual households cannot be identified.

### 5. Announcements
- Create, edit, pin, archive and delete announcements with categories (Emergency, Important,
  Advisory, General, Maintenance) and priorities (Critical → Low).
- Reusable templates; real-time delivery to the Citizen Announcement Viewer.

### 6. Notifications
- Database triggers record every real event (new incident, status change, published
  announcement, security change).
- A background worker delivers them by **email** (Resend) and **web push** (VAPID), with retries
  and idempotency so no duplicates are sent. Users choose which categories they receive.

### 7. Accounts & Security
- Sign-in with email/password or Google; registration for citizens, organizations, hospitals and admins.
- Multi-factor authentication (TOTP) with single-use recovery codes and admin-assisted reset.
- Session management (revoke one or all sessions), security activity log and alerts.
- Profile, agency settings (logo, personnel), display and notification preferences.
- Admin user management: suspend, reinstate, promote, demote, reset password, delete.

## Technology Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3, Flask |
| Database / Auth / Storage / Realtime | Supabase (PostgreSQL) |
| Frontend | Jinja2 templates, Tailwind CSS, JavaScript, service worker |
| Maps | Folium / Leaflet |
| Notifications | Resend (email), pywebpush (web push) |
| Reports | fpdf2 (PDF), CSV |
| Desktop | pywebview + PyInstaller (Windows) |

## Architecture Summary

```
Citizen App ──► Supabase (incidents, photos) ◄──► ESRMS Flask server ◄──► Browser / Desktop window
                     │                                 │
                     └── triggers → notification_events └── worker → Email (Resend) / Web Push
```

Business rules (dispatch, hospital, analytics, risk, announcements, accounts) live in pure Python
modules under `app/` that are independent of Flask and Supabase, so they are covered by unit tests
in `tests/`. Database schema changes are versioned in `supabase/migrations/`.

## Purpose

ESRMS shortens the time between a citizen's call for help and the arrival of the right responders,
gives every party a shared, real-time picture of each emergency, and turns historical incident data
into insight that helps authorities plan where resources are needed most.
