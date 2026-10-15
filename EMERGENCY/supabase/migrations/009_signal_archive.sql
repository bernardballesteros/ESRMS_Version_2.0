-- 009: dispatchers archive finished hospital signals into Incident & Signal History → Signal History.
-- The signal stays the same row; archiving only stamps it. Run once in the Supabase SQL Editor (safe to re-run).

alter table public.hospital_signals
    add column if not exists archived_at timestamptz,
    add column if not exists archived_by uuid references auth.users (id) on delete set null;

create index if not exists hospital_signals_archived_idx on public.hospital_signals (archived_at desc)
    where archived_at is not null;

-- The signal's own history records the move in and out of the archive.
alter table public.hospital_signal_events drop constraint if exists hospital_signal_events_event_check;
alter table public.hospital_signal_events add constraint hospital_signal_events_event_check
    check (event in ('REPORT_RECEIVED', 'REPORT_ACKNOWLEDGED', 'BAY_PREPARATION_STARTED',
                     'TRAUMA_BAY_ASSIGNED', 'TRAUMA_BAY_RELEASED', 'PATIENT_ARRIVED',
                     'HANDOFF_STARTED', 'HANDOFF_COMPLETED', 'REPORT_ARCHIVED',
                     'UNABLE_TO_RECEIVE', 'REPORT_CANCELLED', 'SIGNAL_ARCHIVED', 'SIGNAL_RESTORED'));

notify pgrst, 'reload schema';
