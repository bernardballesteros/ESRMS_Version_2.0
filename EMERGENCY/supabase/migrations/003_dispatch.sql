-- ESRMS dispatch: emergency incidents (written by the separate Citizen Emergency App,
-- read and updated by the Dispatcher Dashboard) and responder organization hotlines.
-- Run in Supabase Dashboard -> SQL Editor after 002_announcements.sql. Safe to re-run.
--
-- Access model:
--   * Dispatcher reads/updates go through the Flask server (service-role key, server side)
--     after it checks the user's role. Browsers cannot read incidents or contact numbers
--     with the public key.
--   * The Citizen Emergency App (signed in with Supabase Auth) may INSERT its own new
--     reports and SELECT its own reports, nothing else. See the policies below.
--   * Browsers subscribe to public.dispatch_feed, a content-free change counter, and
--     refetch from Flask when it moves.

create table if not exists public.emergency_incidents (
    id                 uuid primary key default gen_random_uuid(),
    incident_number    bigint generated always as identity unique,   -- shown as INC-000123
    emergency_type     text not null check (char_length(emergency_type) between 1 and 80),
    description        text check (description is null or char_length(description) <= 4000),
    latitude           double precision check (latitude between -90 and 90),
    longitude          double precision check (longitude between -180 and 180),
    location_accuracy_m real check (location_accuracy_m is null or location_accuracy_m >= 0),
    location_verified  boolean not null default false,
    status             text not null default 'new'
                       check (status in ('new', 'acknowledged', 'dispatched', 'en_route', 'on_scene', 'resolved', 'cancelled')),
    priority           smallint check (priority between 1 and 4),        -- 1 = critical
    citizen_name       text check (citizen_name is null or char_length(citizen_name) <= 120),
    contact_number     text check (contact_number is null or char_length(contact_number) <= 32),
    reporter_id        uuid references auth.users (id) on delete set null,
    acknowledged_at    timestamptz,
    acknowledged_by    uuid references auth.users (id) on delete set null,
    resolved_at        timestamptz,
    archived_at        timestamptz,
    archived_by        uuid references auth.users (id) on delete set null,
    updated_by         uuid references auth.users (id) on delete set null,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    constraint emergency_incidents_coords_pair check ((latitude is null) = (longitude is null)),
    constraint emergency_incidents_archive_closed check (archived_at is null or status in ('resolved', 'cancelled'))
);

create index if not exists emergency_incidents_open_idx
    on public.emergency_incidents (status, priority, created_at desc) where archived_at is null;
create index if not exists emergency_incidents_archived_idx
    on public.emergency_incidents (archived_at desc) where archived_at is not null;
create index if not exists emergency_incidents_reporter_idx on public.emergency_incidents (reporter_id);

drop trigger if exists emergency_incidents_set_updated_at on public.emergency_incidents;
create trigger emergency_incidents_set_updated_at
    before update on public.emergency_incidents
    for each row execute function public.set_updated_at();

-- Status history for the Incident Report tab.
create table if not exists public.incident_events (
    id           bigint generated always as identity primary key,
    incident_id  uuid not null references public.emergency_incidents (id) on delete cascade,
    event        text not null,              -- reported, status, archived, restored
    from_status  text,
    to_status    text,
    actor_id     uuid references auth.users (id) on delete set null,
    created_at   timestamptz not null default now()
);
create index if not exists incident_events_incident_idx on public.incident_events (incident_id, created_at);

create or replace function public.log_incident_event()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    if tg_op = 'INSERT' then
        insert into incident_events (incident_id, event, to_status, actor_id)
        values (new.id, 'reported', new.status, new.reporter_id);
    else
        if new.status is distinct from old.status then
            insert into incident_events (incident_id, event, from_status, to_status, actor_id)
            values (new.id, 'status', old.status, new.status, new.updated_by);
        end if;
        if new.archived_at is not null and old.archived_at is null then
            insert into incident_events (incident_id, event, to_status, actor_id)
            values (new.id, 'archived', new.status, new.archived_by);
        elsif new.archived_at is null and old.archived_at is not null then
            insert into incident_events (incident_id, event, to_status, actor_id)
            values (new.id, 'restored', new.status, new.updated_by);
        end if;
    end if;
    return null;
end;
$$;

drop trigger if exists emergency_incidents_log on public.emergency_incidents;
create trigger emergency_incidents_log
    after insert or update on public.emergency_incidents
    for each row execute function public.log_incident_event();

-- Responder organization hotlines (bottom-right panel). Entered manually by dispatchers.
create table if not exists public.responder_hotlines (
    id                 uuid primary key default gen_random_uuid(),
    organization_name  text not null check (char_length(organization_name) between 1 and 120),
    phone_number       text not null check (phone_number ~ '^\+?[0-9][0-9 ()-]{2,24}$'),
    category           text not null check (category in ('police', 'fire', 'medical', 'rescue', 'disaster', 'coast_guard', 'utility', 'other')),
    description        text check (description is null or char_length(description) <= 500),
    operating_hours    text check (operating_hours is null or char_length(operating_hours) <= 80),
    created_by         uuid references auth.users (id) on delete set null,
    updated_by         uuid references auth.users (id) on delete set null,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

drop trigger if exists responder_hotlines_set_updated_at on public.responder_hotlines;
create trigger responder_hotlines_set_updated_at
    before update on public.responder_hotlines
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------- RLS

alter table public.emergency_incidents enable row level security;
alter table public.incident_events enable row level security;
alter table public.responder_hotlines enable row level security;
-- incident_events and responder_hotlines: no policies, server-side access only.

-- Citizen Emergency App: a signed-in citizen may file a NEW report as themselves.
-- Dispatcher-only columns must be left empty. Priority may be suggested by the app.
drop policy if exists "Citizens can submit their own reports" on public.emergency_incidents;
create policy "Citizens can submit their own reports"
    on public.emergency_incidents for insert
    to authenticated
    with check (
        reporter_id = auth.uid()
        and status = 'new'
        and acknowledged_at is null and acknowledged_by is null
        and resolved_at is null and archived_at is null and archived_by is null
        and updated_by is null
        and location_verified = false
    );

-- ...and read back only their own reports (for status updates in their app).
drop policy if exists "Citizens can view their own reports" on public.emergency_incidents;
create policy "Citizens can view their own reports"
    on public.emergency_incidents for select
    to authenticated
    using (reporter_id = auth.uid());
-- No update/delete policies: only the dispatcher server can change incidents.

-- ---------------------------------------------------------------- realtime counter

create table if not exists public.dispatch_feed (
    id          smallint primary key default 1 check (id = 1),
    version     bigint not null default 0,
    changed_at  timestamptz not null default now()
);
insert into public.dispatch_feed (id) values (1) on conflict (id) do nothing;

alter table public.dispatch_feed enable row level security;
drop policy if exists "Anyone can read the dispatch change counter" on public.dispatch_feed;
create policy "Anyone can read the dispatch change counter"
    on public.dispatch_feed for select to anon, authenticated using (true);

create or replace function public.bump_dispatch_feed()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    update public.dispatch_feed set version = version + 1, changed_at = now() where id = 1;
    return null;
end;
$$;

drop trigger if exists emergency_incidents_bump_feed on public.emergency_incidents;
create trigger emergency_incidents_bump_feed
    after insert or update or delete on public.emergency_incidents
    for each statement execute function public.bump_dispatch_feed();

drop trigger if exists responder_hotlines_bump_feed on public.responder_hotlines;
create trigger responder_hotlines_bump_feed
    after insert or update or delete on public.responder_hotlines
    for each statement execute function public.bump_dispatch_feed();

do $$
begin
    if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'dispatch_feed'
    ) then
        alter publication supabase_realtime add table public.dispatch_feed;
    end if;
end;
$$;
