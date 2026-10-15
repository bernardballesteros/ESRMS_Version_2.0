-- ESRMS: public-safe copy of High-Risk Area Detection for the Citizen App's Safety Radar.
-- Run in Supabase Dashboard -> SQL Editor after 011_user_preferences.sql.
-- Additive and safe to re-run: no existing table or row is changed.
--
-- Data flow: emergency_incidents -> Analytics (app/risk.py, Flask, service role)
--            -> app/public_risk.py removes everything private -> the two tables below
--            -> Citizen App (signed-in citizens, read only).
-- Citizens never read emergency_incidents for this; these tables hold no incident, person,
-- responder or organization data: only generalized 1 km zones with a level and a sentence.

create table if not exists public.public_risk_areas (
    area_id           text primary key check (char_length(area_id) <= 40),
    area_name         text check (area_name is null or char_length(area_name) <= 120),
    risk_level        text not null check (risk_level in ('low', 'moderate', 'high')),
    center_latitude   double precision not null check (center_latitude between -90 and 90),
    center_longitude  double precision not null check (center_longitude between -180 and 180),
    radius_km         double precision not null check (radius_km > 0 and radius_km <= 50),
    public_summary    text not null check (char_length(public_summary) <= 300),
    updated_at        timestamptz not null default now()
);

-- One row: when the copy was last published and whether there was enough data to grade areas.
-- The Citizen App checks `version` to know when to refetch.
create table if not exists public.public_risk_feed (
    id            smallint primary key default 1 check (id = 1),
    version       bigint not null default 0,
    published_at  timestamptz,
    sufficient    boolean not null default false,
    period_days   integer,
    cell_km       double precision
);
insert into public.public_risk_feed (id) values (1) on conflict (id) do nothing;

alter table public.public_risk_areas enable row level security;
alter table public.public_risk_feed enable row level security;

-- Read only, signed-in users only. No insert/update/delete policy: only the server writes.
drop policy if exists "Signed-in users can read public risk areas" on public.public_risk_areas;
create policy "Signed-in users can read public risk areas"
    on public.public_risk_areas for select to authenticated using (true);

drop policy if exists "Signed-in users can read the public risk feed" on public.public_risk_feed;
create policy "Signed-in users can read the public risk feed"
    on public.public_risk_feed for select to authenticated using (true);

-- Replace the whole public copy in one transaction, so the app never sees half of an update.
-- p_areas: array of {area_id, area_name, risk_level, center_latitude, center_longitude, radius_km,
-- public_summary}. p_meta: {sufficient, period_days, cell_km}. Returns the new version.
create or replace function public.publish_public_risk_areas(p_areas jsonb, p_meta jsonb)
returns bigint language plpgsql security definer set search_path = public as $$
declare
    new_version bigint;
begin
    delete from public_risk_areas where true;
    insert into public_risk_areas (area_id, area_name, risk_level, center_latitude, center_longitude,
                                   radius_km, public_summary, updated_at)
    select a->>'area_id', nullif(a->>'area_name', ''), a->>'risk_level',
           (a->>'center_latitude')::double precision, (a->>'center_longitude')::double precision,
           (a->>'radius_km')::double precision, a->>'public_summary', now()
    from jsonb_array_elements(coalesce(p_areas, '[]'::jsonb)) as a;
    update public_risk_feed
       set version = version + 1, published_at = now(),
           sufficient = coalesce((p_meta->>'sufficient')::boolean, false),
           period_days = (p_meta->>'period_days')::integer,
           cell_km = (p_meta->>'cell_km')::double precision
     where id = 1
    returning version into new_version;
    return new_version;
end;
$$;

revoke all on function public.publish_public_risk_areas(jsonb, jsonb) from public, anon, authenticated;
grant execute on function public.publish_public_risk_areas(jsonb, jsonb) to service_role;

notify pgrst, 'reload schema';
