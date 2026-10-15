-- ESRMS account settings: My Profile, Agency Settings, Security & MFA, Notifications.
-- Run in Supabase Dashboard -> SQL Editor after 004_citizen_app.sql.
-- Additive and safe to re-run: no table is dropped and no existing row is changed.
--
-- Access model (same as 003_dispatch.sql): the Flask server reads and writes these with the
-- service-role key after checking the signed-in user, always filtered to that user's id.
-- The own-row policies below additionally let the Citizen Emergency App read and save its
-- own notification preferences with the public key.

-- ---------------------------------------------------------------- 1. profile + agency columns

-- Personal contact number and photo (My Profile).
alter table public.users add column if not exists phone      text check (phone is null or char_length(phone) <= 32);
alter table public.users add column if not exists avatar_url text check (avatar_url is null or char_length(avatar_url) <= 500);

-- Agency Settings. An agency is the organization account that registered it, so its settings
-- stay on that account's row next to agency_name / ori_code / psap_phone.
alter table public.users add column if not exists agency_address     text check (agency_address is null or char_length(agency_address) <= 300);
alter table public.users add column if not exists agency_email       text check (agency_email is null or char_length(agency_email) <= 254);
alter table public.users add column if not exists agency_description text check (agency_description is null or char_length(agency_description) <= 500);
alter table public.users add column if not exists agency_logo_url    text check (agency_logo_url is null or char_length(agency_logo_url) <= 500);

-- ---------------------------------------------------------------- 2. notification preferences

create table if not exists public.notification_preferences (
    user_id               uuid primary key references auth.users (id) on delete cascade,
    emergency_incidents   boolean not null default true,   -- critical / high priority incidents
    new_reports           boolean not null default true,   -- new reports from the Citizen App
    dispatch_updates      boolean not null default true,   -- dispatched / en route / on scene
    status_updates        boolean not null default true,   -- acknowledged / resolved / cancelled
    system_announcements  boolean not null default true,
    security_alerts       boolean not null default true,
    browser_enabled       boolean not null default false,  -- desktop notifications while the console is open
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now()
);

drop trigger if exists notification_preferences_set_updated_at on public.notification_preferences;
create trigger notification_preferences_set_updated_at
    before update on public.notification_preferences
    for each row execute function public.set_updated_at();

alter table public.notification_preferences enable row level security;

drop policy if exists "Users can view own notification preferences" on public.notification_preferences;
create policy "Users can view own notification preferences"
    on public.notification_preferences for select to authenticated
    using (auth.uid() = user_id);

drop policy if exists "Users can create own notification preferences" on public.notification_preferences;
create policy "Users can create own notification preferences"
    on public.notification_preferences for insert to authenticated
    with check (auth.uid() = user_id);

drop policy if exists "Users can update own notification preferences" on public.notification_preferences;
create policy "Users can update own notification preferences"
    on public.notification_preferences for update to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ---------------------------------------------------------------- 3. security activity

-- Password changes, MFA changes and remote sign-outs. Shown on the Security page and used
-- for the "Security alerts" notification. Written only by the server.
create table if not exists public.account_security_events (
    id          bigint generated always as identity primary key,
    user_id     uuid not null references auth.users (id) on delete cascade,
    event       text not null check (event in ('password_changed', 'mfa_enabled', 'mfa_disabled',
                                               'sessions_revoked', 'email_change_requested')),
    user_agent  text check (user_agent is null or char_length(user_agent) <= 300),
    created_at  timestamptz not null default now()
);
create index if not exists account_security_events_user_idx
    on public.account_security_events (user_id, id desc);

alter table public.account_security_events enable row level security;

drop policy if exists "Users can view own security events" on public.account_security_events;
create policy "Users can view own security events"
    on public.account_security_events for select to authenticated
    using (auth.uid() = user_id);
-- No insert/update/delete policies: only the server records security events.

-- ---------------------------------------------------------------- 4. profile photos + agency logos

-- Public bucket: files are shown in <img> tags. Uploads go through the Flask server
-- (service role) after it validates type and size, so no storage policies are needed.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('esrms-media', 'esrms-media', true, 2097152, array['image/png', 'image/jpeg', 'image/webp'])
on conflict (id) do nothing;

-- ---------------------------------------------------------------- 5. refresh the API schema cache

notify pgrst, 'reload schema';
