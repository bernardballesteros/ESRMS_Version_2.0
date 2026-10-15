-- ESRMS Hospital Signal: receiving hospitals, their staff accounts, and patient signals sent
-- to them by dispatchers. Run in Supabase Dashboard -> SQL Editor after
-- 006_delivery_recovery_sessions.sql. Additive and safe to re-run: no existing table or row changes.
--
-- Access model (same as 003_dispatch.sql and 006):
--   * Every table here has RLS enabled and NO policies, so the public key and signed-in
--     browsers cannot read or write patient information. Only the Flask server (service role)
--     does, after checking the signed-in user's role or hospital membership.
--   * Browsers subscribe to public.hospital_signal_feed, a change counter with no content,
--     and refetch from Flask when it moves.
--
-- No hospitals are inserted here. Dispatchers and administrators add the real receiving
-- hospitals from the Hospital Signal tab.

-- ---------------------------------------------------------------- 1. hospitals

create table if not exists public.hospitals (
    id            uuid primary key default gen_random_uuid(),
    name          text not null check (char_length(name) between 1 and 120),
    address       text check (address is null or char_length(address) <= 300),
    phone_number  text check (phone_number is null or phone_number ~ '^\+?[0-9][0-9 ()-]{2,24}$'),
    latitude      double precision check (latitude between -90 and 90),
    longitude     double precision check (longitude between -180 and 180),
    -- Declared by the hospital's own staff (or a dispatcher after a phone call). Not a bed count.
    availability  text not null default 'ready' check (availability in ('ready', 'limited', 'full', 'offline')),
    availability_updated_at timestamptz,
    availability_updated_by uuid references auth.users (id) on delete set null,
    is_active     boolean not null default true,
    created_by    uuid references auth.users (id) on delete set null,
    updated_by    uuid references auth.users (id) on delete set null,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint hospitals_coords_pair check ((latitude is null) = (longitude is null))
);
create unique index if not exists hospitals_name_idx on public.hospitals (lower(name));

drop trigger if exists hospitals_set_updated_at on public.hospitals;
create trigger hospitals_set_updated_at
    before update on public.hospitals
    for each row execute function public.set_updated_at();

-- Existing ESRMS accounts that may receive and answer signals for a hospital.
-- A person's sign-in and role are unchanged; this row is what makes them hospital staff.
create table if not exists public.hospital_staff (
    hospital_id  uuid not null references public.hospitals (id) on delete cascade,
    user_id      uuid not null references auth.users (id) on delete cascade,
    added_by     uuid references auth.users (id) on delete set null,
    created_at   timestamptz not null default now(),
    primary key (hospital_id, user_id)
);
create index if not exists hospital_staff_user_idx on public.hospital_staff (user_id);

-- ---------------------------------------------------------------- 2. signals

create table if not exists public.hospital_signals (
    id                 uuid primary key default gen_random_uuid(),
    signal_number      bigint generated always as identity unique,        -- shown as HS-000123
    hospital_id        uuid not null references public.hospitals (id) on delete restrict,
    -- The ESRMS incident this patient comes from. The incident row is never copied.
    incident_id        uuid references public.emergency_incidents (id) on delete set null,
    signal_type        text not null check (signal_type in ('incoming_patient', 'emergency_admission', 'bed_request',
                                                            'critical_patient', 'other')),
    patient_name       text check (patient_name is null or char_length(patient_name) <= 120),
    patient_age        smallint check (patient_age is null or patient_age between 0 and 130),
    patient_gender     text check (patient_gender is null or patient_gender in ('male', 'female', 'other', 'undisclosed')),
    patient_condition  text not null check (patient_condition in ('stable', 'serious', 'critical', 'unconscious',
                                                                  'injured', 'other')),
    eta                timestamptz,
    additional_notes   text check (additional_notes is null or char_length(additional_notes) <= 500),
    -- Signal status. Separate from emergency_incidents.status.
    status             text not null default 'sent' check (status in ('sent', 'acknowledged', 'accepted', 'received',
                                                                      'unable', 'cancelled')),
    sent_by            uuid references auth.users (id) on delete set null,
    sent_at            timestamptz not null default now(),
    acknowledged_by    uuid references auth.users (id) on delete set null,
    acknowledged_at    timestamptz,
    accepted_by        uuid references auth.users (id) on delete set null,
    accepted_at        timestamptz,
    received_by        uuid references auth.users (id) on delete set null,
    received_at        timestamptz,
    rejected_by        uuid references auth.users (id) on delete set null,
    rejected_at        timestamptz,
    rejection_reason   text check (rejection_reason is null or char_length(rejection_reason) between 1 and 300),
    cancelled_by       uuid references auth.users (id) on delete set null,
    cancelled_at       timestamptz,
    -- Generated by the browser once per Send. A double click or a retry reuses it, so one signal is stored.
    client_request_id  uuid,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    constraint hospital_signals_reject_reason check (status <> 'unable' or rejection_reason is not null)
);
create unique index if not exists hospital_signals_client_request_idx
    on public.hospital_signals (client_request_id) where client_request_id is not null;
create index if not exists hospital_signals_sent_idx on public.hospital_signals (sent_at desc);
create index if not exists hospital_signals_hospital_idx on public.hospital_signals (hospital_id, sent_at desc);
create index if not exists hospital_signals_incident_idx on public.hospital_signals (incident_id) where incident_id is not null;

drop trigger if exists hospital_signals_set_updated_at on public.hospital_signals;
create trigger hospital_signals_set_updated_at
    before update on public.hospital_signals
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------- 3. RLS

alter table public.hospitals enable row level security;
alter table public.hospital_staff enable row level security;
alter table public.hospital_signals enable row level security;
-- No policies: server-side access only (see the access model above).

-- ---------------------------------------------------------------- 4. realtime counter

create table if not exists public.hospital_signal_feed (
    id          smallint primary key default 1 check (id = 1),
    version     bigint not null default 0,
    changed_at  timestamptz not null default now()
);
insert into public.hospital_signal_feed (id) values (1) on conflict (id) do nothing;

alter table public.hospital_signal_feed enable row level security;
drop policy if exists "Anyone can read the hospital signal change counter" on public.hospital_signal_feed;
create policy "Anyone can read the hospital signal change counter"
    on public.hospital_signal_feed for select to anon, authenticated using (true);

create or replace function public.bump_hospital_signal_feed()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    update public.hospital_signal_feed set version = version + 1, changed_at = now() where id = 1;
    return null;
end;
$$;

drop trigger if exists hospital_signals_bump_feed on public.hospital_signals;
create trigger hospital_signals_bump_feed
    after insert or update or delete on public.hospital_signals
    for each statement execute function public.bump_hospital_signal_feed();

drop trigger if exists hospitals_bump_feed on public.hospitals;
create trigger hospitals_bump_feed
    after insert or update or delete on public.hospitals
    for each statement execute function public.bump_hospital_signal_feed();

drop trigger if exists hospital_staff_bump_feed on public.hospital_staff;
create trigger hospital_staff_bump_feed
    after insert or update or delete on public.hospital_staff
    for each statement execute function public.bump_hospital_signal_feed();

do $$
begin
    if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'hospital_signal_feed'
    ) then
        alter publication supabase_realtime add table public.hospital_signal_feed;
    end if;
end;
$$;

-- ---------------------------------------------------------------- 5. email / push notifications

-- Uses the existing outbox (006). Each event is addressed to one account, so only that
-- hospital's staff, or the dispatcher who sent the signal, are notified. Titles and bodies
-- carry no patient name, age, condition or notes: those are only shown inside ESRMS after sign-in.
create or replace function public.notify_hospital_signal_event()
returns trigger language plpgsql security definer set search_path = public as $$
declare
    code text := 'HS-' || lpad(new.signal_number::text, 6, '0');
    hospital_name text;
    type_label text;
    status_label text;
    staff record;
begin
    select h.name into hospital_name from hospitals h where h.id = new.hospital_id;
    type_label := case new.signal_type
        when 'incoming_patient' then 'Incoming Patient' when 'emergency_admission' then 'Emergency Admission'
        when 'bed_request' then 'Bed Request' when 'critical_patient' then 'Critical Patient' else 'Hospital Signal' end;

    if tg_op = 'INSERT' then
        for staff in select s.user_id from hospital_staff s where s.hospital_id = new.hospital_id loop
            perform enqueue_notification(
                'hospital_signal:' || new.id || ':sent:' || staff.user_id, 'dispatch_updates',
                'Hospital signal ' || code || ': ' || type_label,
                'A dispatcher sent a signal to ' || coalesce(hospital_name, 'your hospital') || '. Open ESRMS to view and acknowledge it.',
                new.status, '/hospital', new.id, staff.user_id, new.sent_by);
        end loop;
        return null;
    end if;

    if new.status is distinct from old.status then
        status_label := case new.status
            when 'acknowledged' then 'acknowledged' when 'accepted' then 'accepted' when 'received' then 'marked patient received'
            when 'unable' then 'is unable to receive' when 'cancelled' then 'cancelled' else new.status end;
        if new.status = 'cancelled' then
            for staff in select s.user_id from hospital_staff s where s.hospital_id = new.hospital_id loop
                perform enqueue_notification(
                    'hospital_signal:' || new.id || ':cancelled:' || staff.user_id, 'dispatch_updates',
                    'Hospital signal ' || code || ' cancelled', 'The dispatcher cancelled this signal.',
                    new.status, '/hospital', new.id, staff.user_id, new.cancelled_by);
            end loop;
        elsif new.sent_by is not null then
            perform enqueue_notification(
                'hospital_signal:' || new.id || ':' || new.status, 'dispatch_updates',
                code || ': ' || coalesce(hospital_name, 'Hospital') || ' ' || status_label, type_label,
                new.status, '/dispatch', new.id, new.sent_by, null);
        end if;
    end if;
    return null;
exception when others then
    raise warning 'notify_hospital_signal_event failed: %', sqlerrm;   -- never block a signal
    return null;
end;
$$;

drop trigger if exists hospital_signals_notify on public.hospital_signals;
create trigger hospital_signals_notify
    after insert or update on public.hospital_signals
    for each row execute function public.notify_hospital_signal_event();

-- ---------------------------------------------------------------- 6. refresh the API schema cache

notify pgrst, 'reload schema';
