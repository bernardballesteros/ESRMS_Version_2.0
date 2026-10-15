-- ESRMS Hospital Portal: hospital self-registration, staff roles, trauma bays, the patient
-- handoff workflow and its event history. Run in Supabase Dashboard -> SQL Editor after
-- 007_hospital_signals.sql. Additive and safe to re-run: no table is dropped and no existing
-- row is changed.
--
-- One patient report is one row of public.hospital_signals from the moment the dispatcher
-- sends it until the handoff is completed and archived. Nothing here copies it: Hospital
-- Signal, Incoming Reports, Completed Handoffs and History are views of that same row.
--
-- Access model is unchanged (see 007): RLS is on with no policies, so only the Flask server
-- reads or writes these tables, after checking the signed-in user's hospital membership.

-- ---------------------------------------------------------------- 1. hospital accounts

alter table public.hospitals add column if not exists hospital_type text
    check (hospital_type is null or hospital_type in ('government', 'private', 'specialty', 'clinic', 'military', 'other'));
alter table public.hospitals add column if not exists city           text check (city is null or char_length(city) <= 100);
alter table public.hospitals add column if not exists province       text check (province is null or char_length(province) <= 100);
alter table public.hospitals add column if not exists country        text check (country is null or char_length(country) <= 100);
alter table public.hospitals add column if not exists official_email text check (official_email is null or char_length(official_email) <= 254);
alter table public.hospitals add column if not exists ed_contact     text check (ed_contact is null or char_length(ed_contact) <= 120);
alter table public.hospitals add column if not exists license_number text check (license_number is null or char_length(license_number) <= 60);
-- A hospital that registered itself cannot be sent patient information until a dispatcher or
-- administrator has verified it. Hospitals added by dispatchers (007) are verified already.
alter table public.hospitals add column if not exists verified       boolean not null default true;
alter table public.hospitals add column if not exists verified_at    timestamptz;
alter table public.hospitals add column if not exists verified_by    uuid references auth.users (id) on delete set null;
alter table public.hospitals add column if not exists registered_by  uuid references auth.users (id) on delete set null;

create unique index if not exists hospitals_official_email_idx
    on public.hospitals (lower(official_email)) where official_email is not null;

-- Role of each account inside its hospital.
alter table public.hospital_staff add column if not exists staff_role text not null default 'reception'
    check (staff_role in ('admin', 'reception', 'nurse', 'physician', 'receiving'));
alter table public.hospital_staff add column if not exists position text check (position is null or char_length(position) <= 80);

-- Accounts created through the Hospital Portal carry role 'hospital'.
alter table public.users drop constraint if exists users_role_check;
alter table public.users
    add constraint users_role_check check (role in ('citizen', 'responder', 'organization', 'hospital')) not valid;

-- ---------------------------------------------------------------- 2. trauma bays

-- 'ready' or 'cleaning' is what staff set. Preparing and Occupied are not stored: a bay is
-- Preparing while an active report is assigned to it and Occupied once that patient has arrived.
create table if not exists public.hospital_bays (
    id           uuid primary key default gen_random_uuid(),
    hospital_id  uuid not null references public.hospitals (id) on delete cascade,
    name         text not null check (char_length(name) between 1 and 60),
    kind         text not null default 'trauma' check (kind in ('trauma', 'er', 'other')),
    status       text not null default 'ready' check (status in ('ready', 'cleaning')),
    updated_by   uuid references auth.users (id) on delete set null,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);
create unique index if not exists hospital_bays_name_idx on public.hospital_bays (hospital_id, lower(name));

drop trigger if exists hospital_bays_set_updated_at on public.hospital_bays;
create trigger hospital_bays_set_updated_at
    before update on public.hospital_bays
    for each row execute function public.set_updated_at();

alter table public.hospital_bays enable row level security;

drop trigger if exists hospital_bays_bump_feed on public.hospital_bays;
create trigger hospital_bays_bump_feed
    after insert or update or delete on public.hospital_bays
    for each statement execute function public.bump_hospital_signal_feed();

-- ---------------------------------------------------------------- 3. the report: EMS detail + handoff

alter table public.hospital_signals add column if not exists medic_unit       text check (medic_unit is null or char_length(medic_unit) <= 40);
alter table public.hospital_signals add column if not exists transport_type   text
    check (transport_type is null or transport_type in ('ground_als', 'ground_bls', 'air', 'private', 'other'));
alter table public.hospital_signals add column if not exists triage_priority  smallint check (triage_priority is null or triage_priority between 1 and 4);
alter table public.hospital_signals add column if not exists chief_complaint  text check (chief_complaint is null or char_length(chief_complaint) <= 200);
alter table public.hospital_signals add column if not exists heart_rate       smallint check (heart_rate is null or heart_rate between 0 and 300);
alter table public.hospital_signals add column if not exists bp_systolic      smallint check (bp_systolic is null or bp_systolic between 0 and 350);
alter table public.hospital_signals add column if not exists bp_diastolic     smallint check (bp_diastolic is null or bp_diastolic between 0 and 250);
alter table public.hospital_signals add column if not exists spo2             smallint check (spo2 is null or spo2 between 0 and 100);
alter table public.hospital_signals add column if not exists temperature      numeric(4, 1) check (temperature is null or temperature between 20 and 46);
alter table public.hospital_signals add column if not exists respiratory_rate smallint check (respiratory_rate is null or respiratory_rate between 0 and 120);

alter table public.hospital_signals add column if not exists bay_id             uuid references public.hospital_bays (id) on delete set null;
alter table public.hospital_signals add column if not exists receiving_team     text check (receiving_team is null or char_length(receiving_team) <= 120);
alter table public.hospital_signals add column if not exists arrived_by         uuid references auth.users (id) on delete set null;
alter table public.hospital_signals add column if not exists arrived_at         timestamptz;
alter table public.hospital_signals add column if not exists handoff_started_by uuid references auth.users (id) on delete set null;
alter table public.hospital_signals add column if not exists handoff_started_at timestamptz;
alter table public.hospital_signals add column if not exists completed_by       uuid references auth.users (id) on delete set null;
alter table public.hospital_signals add column if not exists completed_at       timestamptz;
alter table public.hospital_signals add column if not exists receiving_staff    text check (receiving_staff is null or char_length(receiving_staff) <= 120);
alter table public.hospital_signals add column if not exists handoff_notes      text check (handoff_notes is null or char_length(handoff_notes) <= 1000);

-- sent -> acknowledged -> accepted (preparing) -> arrived -> handoff -> completed.
-- 'received' is kept so signals closed under 007 stay valid; it means the same as completed.
alter table public.hospital_signals drop constraint if exists hospital_signals_status_check;
alter table public.hospital_signals add constraint hospital_signals_status_check
    check (status in ('sent', 'acknowledged', 'accepted', 'arrived', 'handoff', 'completed', 'received', 'unable', 'cancelled'));

-- Two active patients can never hold the same bay, even if two people assign at the same moment.
create unique index if not exists hospital_signals_active_bay_idx
    on public.hospital_signals (bay_id)
    where bay_id is not null and status in ('sent', 'acknowledged', 'accepted', 'arrived', 'handoff');

create index if not exists hospital_signals_completed_idx
    on public.hospital_signals (hospital_id, completed_at desc) where completed_at is not null;

-- ---------------------------------------------------------------- 4. report event history

create table if not exists public.hospital_signal_events (
    id           bigint generated always as identity primary key,
    signal_id    uuid not null references public.hospital_signals (id) on delete cascade,
    hospital_id  uuid not null references public.hospitals (id) on delete cascade,
    event        text not null check (event in ('REPORT_RECEIVED', 'REPORT_ACKNOWLEDGED', 'BAY_PREPARATION_STARTED',
                                                'TRAUMA_BAY_ASSIGNED', 'TRAUMA_BAY_RELEASED', 'PATIENT_ARRIVED',
                                                'HANDOFF_STARTED', 'HANDOFF_COMPLETED', 'REPORT_ARCHIVED',
                                                'UNABLE_TO_RECEIVE', 'REPORT_CANCELLED')),
    detail       text check (detail is null or char_length(detail) <= 300),
    actor_id     uuid references auth.users (id) on delete set null,
    created_at   timestamptz not null default now()
);
create index if not exists hospital_signal_events_signal_idx on public.hospital_signal_events (signal_id, id);
alter table public.hospital_signal_events enable row level security;

-- ---------------------------------------------------------------- 5. notifications for the new statuses

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
                'New incoming patient report ' || code || ': ' || type_label,
                'A dispatcher sent a report to ' || coalesce(hospital_name, 'your hospital') || '. Open the Hospital Portal to view and acknowledge it.',
                new.status, '/hospital', new.id, staff.user_id, new.sent_by);
        end loop;
        return null;
    end if;

    if new.status is distinct from old.status then
        status_label := case new.status
            when 'acknowledged' then 'acknowledged the report' when 'accepted' then 'is preparing a bay'
            when 'arrived' then 'reports the patient has arrived' when 'handoff' then 'started the handoff'
            when 'completed' then 'completed the handoff' when 'received' then 'received the patient'
            when 'unable' then 'is unable to receive' when 'cancelled' then 'cancelled' else new.status end;
        if new.status = 'cancelled' then
            for staff in select s.user_id from hospital_staff s where s.hospital_id = new.hospital_id loop
                perform enqueue_notification(
                    'hospital_signal:' || new.id || ':cancelled:' || staff.user_id, 'dispatch_updates',
                    'Patient report ' || code || ' cancelled', 'The dispatcher cancelled this report.',
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
    raise warning 'notify_hospital_signal_event failed: %', sqlerrm;   -- never block a report
    return null;
end;
$$;

-- ---------------------------------------------------------------- 6. refresh the API schema cache

notify pgrst, 'reload schema';
