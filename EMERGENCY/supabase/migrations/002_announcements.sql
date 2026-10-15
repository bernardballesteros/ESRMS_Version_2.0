-- ESRMS announcements (admin dashboard + Citizen Announcement Viewer).
-- Run in Supabase Dashboard -> SQL Editor after schema.sql. Safe to re-run.
--
-- Access model:
--   * All writes go through the Flask server (service-role key, server side only)
--     after it checks the signed-in user's role. RLS has NO insert/update/delete
--     policies, so the public anon key and signed-in browser sessions cannot write.
--   * Reads of announcements also go through Flask, which applies the publication
--     window and audience/barangay rules. RLS has no select policy for anon or
--     authenticated, so barangay-targeted rows can't be read with the public key.
--   * Browsers subscribe (Realtime) to public.announcement_feed, a one-row change
--     counter with no announcement content, and refetch from Flask when it changes.

create table if not exists public.announcements (
    id               uuid primary key default gen_random_uuid(),
    title            text not null check (char_length(title) between 1 and 200),
    message          text not null check (char_length(message) between 1 and 10000),
    category         text not null check (category in ('emergency', 'important', 'advisory', 'general', 'maintenance')),
    priority         text not null default 'medium' check (priority in ('critical', 'high', 'medium', 'low')),
    target_audience  text not null default 'all_citizens'
                     check (target_audience in ('all_citizens', 'barangay', 'emergency_personnel', 'selected_groups')),
    barangay         text,
    target_groups    text[] not null default '{}',
    status           text not null default 'draft' check (status in ('draft', 'scheduled', 'active', 'archived')),
    is_pinned        boolean not null default false,
    published_at     timestamptz,          -- when it becomes / became publicly visible
    scheduled_at     timestamptz,          -- planned publication time for scheduled posts
    expires_at       timestamptz,
    published_by     uuid references auth.users (id) on delete set null,
    created_by       uuid references auth.users (id) on delete set null,
    updated_by       uuid references auth.users (id) on delete set null,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    constraint announcements_barangay_required
        check (target_audience <> 'barangay' or coalesce(btrim(barangay), '') <> ''),
    constraint announcements_groups_required
        check (target_audience <> 'selected_groups' or cardinality(target_groups) > 0),
    constraint announcements_published_at_required
        check (status in ('draft', 'archived') or published_at is not null),
    constraint announcements_expiry_after_publish
        check (expires_at is null or published_at is null or expires_at > published_at)
);

create index if not exists announcements_status_idx on public.announcements (status);
create index if not exists announcements_published_at_idx on public.announcements (published_at desc);
create index if not exists announcements_public_idx
    on public.announcements (published_at desc, expires_at)
    where status in ('active', 'scheduled');
create index if not exists announcements_barangay_idx on public.announcements (lower(barangay)) where barangay is not null;
create index if not exists announcements_pinned_idx on public.announcements (is_pinned) where is_pinned;

-- set_updated_at() is defined in schema.sql; recreate it here so this file runs on its own.
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists announcements_set_updated_at on public.announcements;
create trigger announcements_set_updated_at
    before update on public.announcements
    for each row execute function public.set_updated_at();

alter table public.announcements enable row level security;
-- Intentionally no policies: only the service role (Flask server) can read or write.

-- Reusable message templates for the dashboard's "Manage Templates" action.
create table if not exists public.announcement_templates (
    id          uuid primary key default gen_random_uuid(),
    name        text not null check (char_length(name) between 1 and 120),
    title       text not null default '',
    message     text not null default '',
    category    text not null default 'general' check (category in ('emergency', 'important', 'advisory', 'general', 'maintenance')),
    priority    text not null default 'medium' check (priority in ('critical', 'high', 'medium', 'low')),
    created_by  uuid references auth.users (id) on delete set null,
    created_at  timestamptz not null default now()
);

alter table public.announcement_templates enable row level security;
-- No policies: server-side access only.

-- Change counter that browsers subscribe to. It carries no announcement content.
create table if not exists public.announcement_feed (
    id          smallint primary key default 1 check (id = 1),
    version     bigint not null default 0,
    changed_at  timestamptz not null default now()
);
insert into public.announcement_feed (id) values (1) on conflict (id) do nothing;

alter table public.announcement_feed enable row level security;
drop policy if exists "Anyone can read the announcement change counter" on public.announcement_feed;
create policy "Anyone can read the announcement change counter"
    on public.announcement_feed for select
    to anon, authenticated
    using (true);

create or replace function public.bump_announcement_feed()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    update public.announcement_feed set version = version + 1, changed_at = now() where id = 1;
    return null;
end;
$$;

drop trigger if exists announcements_bump_feed on public.announcements;
create trigger announcements_bump_feed
    after insert or update or delete on public.announcements
    for each statement execute function public.bump_announcement_feed();

-- Realtime: broadcast changes to the counter row.
do $$
begin
    if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'announcement_feed'
    ) then
        alter publication supabase_realtime add table public.announcement_feed;
    end if;
end;
$$;
