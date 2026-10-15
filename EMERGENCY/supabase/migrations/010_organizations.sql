-- 010: organization-based dispatch. The hospitals table becomes the list of every verified emergency
-- organization BASE (EMS, fire, police, barangay, hospital, other); existing rows stay hospitals.
-- hospital_signals also carries response requests sent to those organizations from the Dispatch Center.
-- No individual responder is tracked anywhere: an organization has one fixed base latitude/longitude.
-- Run once in the Supabase SQL Editor after 009_signal_archive.sql (safe to re-run).

-- ---------------------------------------------------------------- 1. organization bases

alter table public.hospitals add column if not exists org_type text not null default 'hospital';
alter table public.hospitals drop constraint if exists hospitals_org_type_check;
alter table public.hospitals add constraint hospitals_org_type_check
    check (org_type in ('ems', 'fire', 'police', 'barangay', 'hospital', 'other'));
alter table public.hospitals add column if not exists base_name text
    check (base_name is null or char_length(base_name) <= 120);
create index if not exists hospitals_org_type_idx on public.hospitals (org_type) where is_active;

-- ---------------------------------------------------------------- 2. response requests

alter table public.hospital_signals drop constraint if exists hospital_signals_signal_type_check;
alter table public.hospital_signals add constraint hospital_signals_signal_type_check
    check (signal_type in ('incoming_patient', 'emergency_admission', 'bed_request', 'critical_patient', 'other',
                           'medical_response', 'fire_response', 'police_response', 'barangay_response', 'response_request'));

-- Response request: sent -> acknowledged -> accepted -> responding -> on_scene -> completed.
alter table public.hospital_signals drop constraint if exists hospital_signals_status_check;
alter table public.hospital_signals add constraint hospital_signals_status_check
    check (status in ('sent', 'acknowledged', 'accepted', 'arrived', 'handoff', 'responding', 'on_scene',
                      'completed', 'received', 'unable', 'cancelled'));

alter table public.hospital_signals add column if not exists responding_by uuid references auth.users (id) on delete set null;
alter table public.hospital_signals add column if not exists responding_at timestamptz;
alter table public.hospital_signals add column if not exists on_scene_by   uuid references auth.users (id) on delete set null;
alter table public.hospital_signals add column if not exists on_scene_at   timestamptz;

create index if not exists hospital_signals_incident_idx on public.hospital_signals (incident_id) where incident_id is not null;

alter table public.hospital_signal_events drop constraint if exists hospital_signal_events_event_check;
alter table public.hospital_signal_events add constraint hospital_signal_events_event_check
    check (event in ('REPORT_RECEIVED', 'REPORT_ACKNOWLEDGED', 'BAY_PREPARATION_STARTED',
                     'TRAUMA_BAY_ASSIGNED', 'TRAUMA_BAY_RELEASED', 'PATIENT_ARRIVED',
                     'HANDOFF_STARTED', 'HANDOFF_COMPLETED', 'REPORT_ARCHIVED',
                     'UNABLE_TO_RECEIVE', 'REPORT_CANCELLED', 'SIGNAL_ARCHIVED', 'SIGNAL_RESTORED',
                     'REQUEST_ACCEPTED', 'ORG_RESPONDING', 'ORG_ON_SCENE', 'RESPONSE_COMPLETED'));

-- ---------------------------------------------------------------- 3. notifications (no patient or caller detail in the text)

create or replace function public.notify_hospital_signal_event()
returns trigger language plpgsql security definer set search_path = public as $$
declare
    code text := 'HS-' || lpad(new.signal_number::text, 6, '0');
    org_name text;
    type_label text;
    status_label text;
    is_response boolean := new.signal_type in ('medical_response', 'fire_response', 'police_response',
                                               'barangay_response', 'response_request');
    staff record;
begin
    select h.name into org_name from hospitals h where h.id = new.hospital_id;
    type_label := case new.signal_type
        when 'incoming_patient' then 'Incoming Patient' when 'emergency_admission' then 'Emergency Admission'
        when 'bed_request' then 'Bed Request' when 'critical_patient' then 'Critical Patient'
        when 'medical_response' then 'Medical Response Request' when 'fire_response' then 'Fire Response Request'
        when 'police_response' then 'Police Response Request' when 'barangay_response' then 'Barangay Response Request'
        when 'response_request' then 'Emergency Response Request' else 'Hospital Signal' end;

    if tg_op = 'INSERT' then
        for staff in select s.user_id from hospital_staff s where s.hospital_id = new.hospital_id loop
            perform enqueue_notification(
                'hospital_signal:' || new.id || ':sent:' || staff.user_id, 'dispatch_updates',
                case when is_response then 'New emergency signal ' else 'New incoming patient report ' end || code || ': ' || type_label,
                'A dispatcher sent a signal to ' || coalesce(org_name, 'your organization') || '. Open the portal to view and acknowledge it.',
                new.status, '/hospital', new.id, staff.user_id, new.sent_by);
        end loop;
        return null;
    end if;

    if new.status is distinct from old.status then
        status_label := case new.status
            when 'acknowledged' then 'acknowledged the signal'
            when 'accepted' then case when is_response then 'accepted the request' else 'is preparing a bay' end
            when 'responding' then 'is responding' when 'on_scene' then 'is on scene'
            when 'arrived' then 'reports the patient has arrived' when 'handoff' then 'started the handoff'
            when 'completed' then case when is_response then 'completed the response' else 'completed the handoff' end
            when 'received' then 'received the patient'
            when 'unable' then case when is_response then 'is unable to respond' else 'is unable to receive' end
            when 'cancelled' then 'cancelled' else new.status end;
        if new.status = 'cancelled' then
            for staff in select s.user_id from hospital_staff s where s.hospital_id = new.hospital_id loop
                perform enqueue_notification(
                    'hospital_signal:' || new.id || ':cancelled:' || staff.user_id, 'dispatch_updates',
                    'Signal ' || code || ' cancelled', 'The dispatcher cancelled this signal.',
                    new.status, '/hospital', new.id, staff.user_id, new.cancelled_by);
            end loop;
        elsif new.sent_by is not null then
            perform enqueue_notification(
                'hospital_signal:' || new.id || ':' || new.status, 'dispatch_updates',
                code || ': ' || coalesce(org_name, 'Organization') || ' ' || status_label, type_label,
                new.status, '/dispatch', new.id, new.sent_by, null);
        end if;
    end if;
    return null;
exception when others then
    raise warning 'notify_hospital_signal_event failed: %', sqlerrm;   -- never block a signal
    return null;
end;
$$;

notify pgrst, 'reload schema';
