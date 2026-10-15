-- ESRMS users table for Supabase (PostgreSQL).
-- Run in Supabase Dashboard -> SQL Editor.
-- One row per auth account; filled automatically on sign-up from the
-- metadata the registration forms send (see app/routes/auth.py).

create table if not exists public.users (
    id            uuid primary key references auth.users (id) on delete cascade,
    email         text not null unique,
    role          text check (role in ('responder', 'organization')),
    full_name     text,
    call_sign     text,
    branch        text check (branch in ('ems', 'fire', 'law', 'sar')),
    badge_number  text,
    station       text,
    agency_name   text,
    agency_type   text check (agency_type in ('psap-primary', 'psap-secondary', 'regional-e911', 'law-dispatch', 'tri-services')),
    ori_code      varchar(9),
    jurisdiction  text,
    cad_cert      text,
    psap_phone    text,
    mfa_type      text check (mfa_type in ('fido', 'piv', 'totp')),
    status        text not null default 'pending' check (status in ('pending', 'verified', 'suspended')),
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create index if not exists users_role_idx on public.users (role);

-- Keep updated_at current.
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists users_set_updated_at on public.users;
create trigger users_set_updated_at
    before update on public.users
    for each row execute function public.set_updated_at();

-- Copy new sign-ups (email/password or Google) into public.users.
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
declare
    meta jsonb := coalesce(new.raw_user_meta_data, '{}'::jsonb);
begin
    insert into public.users (
        id, email, role, full_name,
        call_sign, branch, badge_number, station,
        agency_name, agency_type, ori_code, jurisdiction, cad_cert, psap_phone, mfa_type
    ) values (
        new.id, new.email, meta->>'role', coalesce(meta->>'full_name', meta->>'name'),
        meta->>'call_sign', meta->>'branch', meta->>'badge_number', meta->>'station',
        meta->>'agency_name', meta->>'agency_type', meta->>'ori_code', meta->>'jurisdiction',
        meta->>'cad_cert', meta->>'psap_phone', meta->>'mfa_type'
    )
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- Row Level Security: signed-in users can read and edit only their own row.
alter table public.users enable row level security;

drop policy if exists "Users can view own profile" on public.users;
create policy "Users can view own profile"
    on public.users for select
    using (auth.uid() = id);

drop policy if exists "Users can update own profile" on public.users;
create policy "Users can update own profile"
    on public.users for update
    using (auth.uid() = id)
    with check (auth.uid() = id);
