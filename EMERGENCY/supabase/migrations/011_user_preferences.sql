-- ESRMS Settings -> Display: each account's own display size.
-- Run in Supabase Dashboard -> SQL Editor after 010_organizations.sql.
-- Additive and safe to re-run: no table is dropped and no existing row is changed.
--
-- Access model (same as 006): RLS is enabled with NO policies, so only the Flask server
-- (service role) reads and writes this, always filtered to the signed-in user's id.
-- One row per account of any kind (dispatcher, organization, hospital, admin, citizen).
-- Notification choices stay in public.notification_preferences; this table is for the
-- interface itself.

create table if not exists public.user_preferences (
    user_id        uuid primary key references auth.users (id) on delete cascade,
    display_scale  smallint not null default 90 check (display_scale in (80, 90, 100, 110, 125)),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

drop trigger if exists user_preferences_set_updated_at on public.user_preferences;
create trigger user_preferences_set_updated_at
    before update on public.user_preferences
    for each row execute function public.set_updated_at();

alter table public.user_preferences enable row level security;

notify pgrst, 'reload schema';
