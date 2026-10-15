-- ESRMS: email + push delivery, MFA recovery, session management, agency membership.
-- Run in Supabase Dashboard -> SQL Editor after 005_account_settings.sql.
-- Additive and safe to re-run: no table is dropped and no existing row is changed.
--
-- Access model: every table here has RLS enabled and NO policies, so the public key and
-- signed-in browsers cannot read or write them. Only the Flask server (service role) does,
-- after checking the signed-in user. Push endpoints, recovery-code hashes and delivery logs
-- therefore never reach a browser.

-- ---------------------------------------------------------------- 1. delivery preferences

alter table public.notification_preferences
    add column if not exists email_enabled boolean not null default false;

-- One row per browser/device the user enabled push on.
create table if not exists public.push_subscriptions (
    id               uuid primary key default gen_random_uuid(),
    user_id          uuid not null references auth.users (id) on delete cascade,
    endpoint         text not null unique check (char_length(endpoint) between 10 and 1000),
    p256dh           text not null check (char_length(p256dh) between 10 and 200),
    auth             text not null check (char_length(auth) between 10 and 100),
    user_agent       text check (user_agent is null or char_length(user_agent) <= 300),
    failure_count    integer not null default 0,
    last_success_at  timestamptz,
    created_at       timestamptz not null default now()
);
create index if not exists push_subscriptions_user_idx on public.push_subscriptions (user_id);
alter table public.push_subscriptions enable row level security;

-- ---------------------------------------------------------------- 2. notification outbox

-- Database triggers write one row per real ESRMS event. The Flask delivery worker claims
-- each row once, works out who should receive it and records one delivery per recipient
-- and channel. event_key is unique, so the same event can never be queued twice.
create table if not exists public.notification_events (
    id              bigint generated always as identity primary key,
    event_key       text not null unique,
    category        text not null check (category in ('emergency_incidents', 'new_reports', 'dispatch_updates',
                                                      'status_updates', 'system_announcements', 'security_alerts')),
    title           text not null check (char_length(title) <= 200),
    body            text not null default '' check (char_length(body) <= 500),
    code            text,                                   -- machine detail: status or security event name
    url             text,                                   -- path inside ESRMS
    subject_id      uuid,                                   -- incident or announcement id
    target_user_id  uuid references auth.users (id) on delete cascade,   -- only this user (security alerts)
    actor_id        uuid,                                   -- who caused it; not notified
    available_at    timestamptz not null default now(),     -- scheduled announcements wait until this time
    status          text not null default 'pending' check (status in ('pending', 'processing', 'done')),
    claimed_at      timestamptz,
    created_at      timestamptz not null default now()
);
create index if not exists notification_events_queue_idx
    on public.notification_events (available_at) where status <> 'done';
alter table public.notification_events enable row level security;

create table if not exists public.notification_deliveries (
    id               bigint generated always as identity primary key,
    event_id         bigint not null references public.notification_events (id) on delete cascade,
    user_id          uuid not null references auth.users (id) on delete cascade,
    channel          text not null check (channel in ('email', 'push')),
    target           text not null,                          -- email address or push_subscriptions.id
    status           text not null default 'pending' check (status in ('pending', 'sending', 'sent', 'failed', 'dead')),
    attempts         integer not null default 0,
    last_error       text check (last_error is null or char_length(last_error) <= 500),
    provider_id      text,                                   -- id the provider returned when it accepted the message
    next_attempt_at  timestamptz not null default now(),
    claimed_at       timestamptz,
    sent_at          timestamptz,
    created_at       timestamptz not null default now(),
    -- One delivery per event, recipient and device: retries update this row instead of adding another.
    constraint notification_deliveries_once unique (event_id, user_id, channel, target)
);
create index if not exists notification_deliveries_queue_idx
    on public.notification_deliveries (next_attempt_at) where status in ('pending', 'failed', 'sending');
create index if not exists notification_deliveries_user_idx on public.notification_deliveries (user_id, id desc);
alter table public.notification_deliveries enable row level security;

create or replace function public.enqueue_notification(
    p_key text, p_category text, p_title text, p_body text, p_code text, p_url text,
    p_subject uuid, p_target uuid, p_actor uuid, p_available timestamptz default now()
) returns void language plpgsql security definer set search_path = public as $$
begin
    insert into notification_events (event_key, category, title, body, code, url, subject_id, target_user_id, actor_id, available_at)
    values (p_key, p_category, left(p_title, 200), left(coalesce(p_body, ''), 500), p_code, p_url, p_subject, p_target, p_actor,
            coalesce(p_available, now()))
    on conflict (event_key) do nothing;
exception when others then
    -- Notifications must never stop an emergency report, a status change or a security action.
    raise warning 'enqueue_notification(%) failed: %', p_key, sqlerrm;
end;
$$;
revoke all on function public.enqueue_notification(text, text, text, text, text, text, uuid, uuid, uuid, timestamptz)
    from public, anon, authenticated;

-- ---------------------------------------------------------------- 3. real events -> outbox

create or replace function public.notify_incident_event()
returns trigger language plpgsql security definer set search_path = public as $$
declare
    code text := 'INC-' || lpad(new.incident_number::text, 6, '0');
    label text;
begin
    if tg_op = 'INSERT' then
        perform enqueue_notification(
            'incident:' || new.id || ':new',
            case when new.priority in (1, 2) then 'emergency_incidents' else 'new_reports' end,
            'New emergency report ' || code, new.emergency_type, new.status, '/dispatch', new.id, null, null);
        return null;
    end if;

    if new.priority in (1, 2) and coalesce(old.priority, 9) not in (1, 2) and new.status not in ('resolved', 'cancelled') then
        perform enqueue_notification(
            'incident:' || new.id || ':escalated:' || new.priority || ':' || extract(epoch from clock_timestamp()),
            'emergency_incidents', code || ' escalated to P' || new.priority, new.emergency_type, new.status,
            '/dispatch', new.id, null, new.updated_by);
    end if;

    if new.status is distinct from old.status then
        label := case new.status
            when 'new' then 'New' when 'acknowledged' then 'Acknowledged' when 'dispatched' then 'Dispatched'
            when 'en_route' then 'Units En Route' when 'on_scene' then 'On Scene'
            when 'resolved' then 'Resolved' when 'cancelled' then 'Cancelled' else new.status end;
        perform enqueue_notification(
            'incident:' || new.id || ':status:' || new.status || ':' || extract(epoch from clock_timestamp()),
            case when new.status in ('dispatched', 'en_route', 'on_scene') then 'dispatch_updates' else 'status_updates' end,
            code || ' is now ' || label, new.emergency_type, new.status, '/dispatch', new.id, null, new.updated_by);
    end if;
    return null;
exception when others then
    raise warning 'notify_incident_event failed: %', sqlerrm;   -- never block an incident write
    return null;
end;
$$;

drop trigger if exists emergency_incidents_notify on public.emergency_incidents;
create trigger emergency_incidents_notify
    after insert or update on public.emergency_incidents
    for each row execute function public.notify_incident_event();

-- An announcement is queued once per publication time. The worker waits until that time and
-- re-checks that it is still live and who its audience is before sending anything.
create or replace function public.notify_announcement_event()
returns trigger language plpgsql security definer set search_path = public as $$
declare
    newly_published boolean;
begin
    if new.status not in ('active', 'scheduled') or new.published_at is null then
        return null;
    end if;
    if tg_op = 'INSERT' then
        newly_published := true;
    else
        newly_published := old.status not in ('active', 'scheduled') or old.published_at is distinct from new.published_at;
    end if;
    if newly_published then
        perform enqueue_notification(
            'announcement:' || new.id || ':' || extract(epoch from new.published_at),
            'system_announcements', 'New announcement', new.title, new.priority, '/announcements', new.id, null,
            coalesce(new.updated_by, new.created_by), greatest(new.published_at, now()));
    end if;
    return null;
exception when others then
    raise warning 'notify_announcement_event failed: %', sqlerrm;   -- never block saving an announcement
    return null;
end;
$$;

drop trigger if exists announcements_notify on public.announcements;
create trigger announcements_notify
    after insert or update on public.announcements
    for each row execute function public.notify_announcement_event();

create or replace function public.notify_security_event()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    perform enqueue_notification('security:' || new.id, 'security_alerts', 'Security alert', '', new.event,
                                 '/account/security', null, new.user_id, null);
    return null;
end;
$$;

drop trigger if exists account_security_events_notify on public.account_security_events;
create trigger account_security_events_notify
    after insert on public.account_security_events
    for each row execute function public.notify_security_event();

alter table public.account_security_events drop constraint if exists account_security_events_event_check;
alter table public.account_security_events add constraint account_security_events_event_check
    check (event in ('password_changed', 'mfa_enabled', 'mfa_disabled', 'sessions_revoked', 'email_change_requested',
                     'mfa_factor_added', 'mfa_factor_removed', 'recovery_codes_generated', 'recovery_code_used',
                     'mfa_reset_requested', 'mfa_reset_by_admin', 'session_revoked', 'all_sessions_revoked'));

-- ---------------------------------------------------------------- 4. MFA recovery

-- Single-use recovery codes. Only a SHA-256 hash of each code is stored; the codes themselves
-- are shown to the user once and never kept.
create table if not exists public.mfa_recovery_codes (
    id          bigint generated always as identity primary key,
    user_id     uuid not null references auth.users (id) on delete cascade,
    code_hash   text not null check (char_length(code_hash) = 64),
    used_at     timestamptz,
    created_at  timestamptz not null default now(),
    unique (user_id, code_hash)
);
alter table public.mfa_recovery_codes enable row level security;

-- Administrator-assisted reset. A request can only be created by someone who has just passed
-- the account's password check, and an administrator can only approve it with the reference
-- code that person was shown (stored hashed).
create table if not exists public.mfa_reset_requests (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null references auth.users (id) on delete cascade,
    code_hash      text not null check (char_length(code_hash) = 64),
    status         text not null default 'pending' check (status in ('pending', 'approved', 'rejected', 'expired')),
    attempts       integer not null default 0,
    user_agent     text check (user_agent is null or char_length(user_agent) <= 300),
    identity_note  text check (identity_note is null or char_length(identity_note) <= 500),
    decided_by     uuid references auth.users (id) on delete set null,
    decided_at     timestamptz,
    expires_at     timestamptz not null default now() + interval '24 hours',
    created_at     timestamptz not null default now()
);
create unique index if not exists mfa_reset_requests_one_pending
    on public.mfa_reset_requests (user_id) where status = 'pending';
alter table public.mfa_reset_requests enable row level security;

-- Kept even if the account is later deleted.
create table if not exists public.mfa_recovery_audit (
    id          bigint generated always as identity primary key,
    user_id     uuid,
    actor_id    uuid,
    action      text not null check (action in ('codes_generated', 'code_used', 'code_rejected', 'reset_requested',
                                                'reset_approved', 'reset_rejected', 'reset_code_mismatch')),
    detail      text check (detail is null or char_length(detail) <= 500),
    created_at  timestamptz not null default now()
);
create index if not exists mfa_recovery_audit_user_idx on public.mfa_recovery_audit (user_id, id desc);
alter table public.mfa_recovery_audit enable row level security;

-- ---------------------------------------------------------------- 5. sessions

-- Supabase Auth has no API that lists a user's sessions, but it keeps them in auth.sessions.
-- These functions expose only what the Security page needs and can only be called with the
-- service role (the Flask server), which always passes the signed-in user's own id.
create or replace function public.account_sessions(p_user_id uuid)
returns table (id uuid, created_at timestamptz, last_active_at timestamptz, user_agent text, aal text)
language sql stable security definer set search_path = '' as $$
    select s.id, s.created_at,
           coalesce(s.refreshed_at at time zone 'utc', s.updated_at, s.created_at),
           s.user_agent, s.aal::text
    from auth.sessions s
    where s.user_id = p_user_id and (s.not_after is null or s.not_after > now())
    order by 3 desc
    limit 50;
$$;

create or replace function public.account_session_active(p_user_id uuid, p_session_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    select exists (
        select 1 from auth.sessions s
        where s.id = p_session_id and s.user_id = p_user_id and (s.not_after is null or s.not_after > now())
    );
$$;

-- Deleting the session row is what Supabase's own sign-out does; its refresh tokens go with it.
create or replace function public.revoke_account_session(p_user_id uuid, p_session_id uuid)
returns boolean language plpgsql security definer set search_path = '' as $$
declare
    removed integer;
begin
    delete from auth.sessions s where s.id = p_session_id and s.user_id = p_user_id;
    get diagnostics removed = row_count;
    return removed > 0;
end;
$$;

revoke all on function public.account_sessions(uuid) from public, anon, authenticated;
revoke all on function public.account_session_active(uuid, uuid) from public, anon, authenticated;
revoke all on function public.revoke_account_session(uuid, uuid) from public, anon, authenticated;
grant execute on function public.account_sessions(uuid) to service_role;
grant execute on function public.account_session_active(uuid, uuid) to service_role;
grant execute on function public.revoke_account_session(uuid, uuid) to service_role;

-- ---------------------------------------------------------------- 6. agency membership

-- Personnel assigned to an agency (the organization account's row) may view its information.
alter table public.users add column if not exists agency_id uuid references public.users (id) on delete set null;
create index if not exists users_agency_idx on public.users (agency_id) where agency_id is not null;

-- The existing "Users can update own profile" policy allows a signed-in user to update any
-- column of their own row with the public key. Keep that policy, but stop it being used to
-- change the columns that carry authority. The Flask server (service role) is unaffected.
create or replace function public.users_protect_columns()
returns trigger language plpgsql as $$
begin
    if auth.uid() is not null then
        new.id := old.id;
        new.email := old.email;
        new.role := old.role;
        new.status := old.status;
        new.agency_id := old.agency_id;
        new.ori_code := old.ori_code;
    end if;
    return new;
end;
$$;

drop trigger if exists users_protect_columns on public.users;
create trigger users_protect_columns
    before update on public.users
    for each row execute function public.users_protect_columns();

-- ---------------------------------------------------------------- 7. refresh the API schema cache

notify pgrst, 'reload schema';
