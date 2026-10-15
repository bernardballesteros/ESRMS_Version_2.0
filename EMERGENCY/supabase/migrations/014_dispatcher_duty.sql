-- ESRMS dispatcher duty status (Console Status) and incident assignment.
-- Run in Supabase Dashboard -> SQL Editor after 013_incident_photos.sql. Safe to re-run; deletes no data.
--
--   * public.dispatcher_duty: one row per dispatcher account. No row = off_duty, so new accounts
--     receive nothing until they mark themselves On Duty.
--   * public.dispatcher_duty_events: audit trail of every status change.
--   * emergency_incidents.assigned_to / assigned_at: who is handling an incident. Assignment changes are
--     written to incident_events (event 'assigned' / 'reassigned' / 'unassigned') by the existing log trigger.
--   * set_dispatcher_duty() and assign_incident() run each change in one transaction with row locks, so
--     an assignment can never land on a dispatcher whose status changed at the same moment, and two
--     dispatchers can never both take the same incident.
-- All access is server side (service role) after Flask checks the user's role; no browser policies.

create table if not exists public.dispatcher_duty (
    user_id     uuid primary key references auth.users (id) on delete cascade,
    status      text not null default 'off_duty' check (status in ('on_duty', 'on_break', 'off_duty')),
    changed_at  timestamptz not null default now()
);
alter table public.dispatcher_duty enable row level security;

create table if not exists public.dispatcher_duty_events (
    id           bigint generated always as identity primary key,
    user_id      uuid not null references auth.users (id) on delete cascade,
    from_status  text,
    to_status    text not null,
    actor_id     uuid references auth.users (id) on delete set null,
    active_incidents integer not null default 0,   -- incidents still assigned to them at the time
    created_at   timestamptz not null default now()
);
create index if not exists dispatcher_duty_events_user_idx on public.dispatcher_duty_events (user_id, created_at desc);
alter table public.dispatcher_duty_events enable row level security;

alter table public.emergency_incidents add column if not exists assigned_to uuid references auth.users (id) on delete set null;
alter table public.emergency_incidents add column if not exists assigned_at timestamptz;
create index if not exists emergency_incidents_assigned_idx
    on public.emergency_incidents (assigned_to) where archived_at is null;

alter table public.incident_events add column if not exists from_assignee uuid;
alter table public.incident_events add column if not exists to_assignee uuid;

-- The Citizen App must not be able to file a report pre-assigned to someone.
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
        and assigned_to is null and assigned_at is null
    );

-- Same as 003's log trigger, plus assignment changes.
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
        if new.assigned_to is distinct from old.assigned_to then
            insert into incident_events (incident_id, event, to_status, actor_id, from_assignee, to_assignee)
            values (new.id,
                    case when old.assigned_to is null then 'assigned'
                         when new.assigned_to is null then 'unassigned' else 'reassigned' end,
                    new.status, new.updated_by, old.assigned_to, new.assigned_to);
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

-- Change a dispatcher's own status and record it. Locks their duty row, so it waits for (and blocks)
-- any assignment to them that is in progress.
create or replace function public.set_dispatcher_duty(p_user uuid, p_status text, p_actor uuid)
returns table (status text, changed_at timestamptz, previous text, active_incidents integer)
language plpgsql security definer set search_path = public as $$
declare
    v_old text;
    v_active integer;
begin
    if p_status not in ('on_duty', 'on_break', 'off_duty') then
        raise exception 'ESRMS_INVALID_DUTY_STATUS' using errcode = '22023';
    end if;
    insert into dispatcher_duty (user_id, status) values (p_user, 'off_duty') on conflict (user_id) do nothing;
    select d.status into v_old from dispatcher_duty d where d.user_id = p_user for update;
    select count(*) into v_active from emergency_incidents e
     where e.assigned_to = p_user and e.archived_at is null and e.status not in ('resolved', 'cancelled');
    if v_old is distinct from p_status then
        update dispatcher_duty d set status = p_status, changed_at = now() where d.user_id = p_user;
        insert into dispatcher_duty_events (user_id, from_status, to_status, actor_id, active_incidents)
        values (p_user, v_old, p_status, p_actor, v_active);
    end if;
    return query select d.status, d.changed_at, v_old, v_active from dispatcher_duty d where d.user_id = p_user;
end;
$$;

-- Assign (claim or reassign) an open incident. Succeeds only if the assignee is On Duty at this moment
-- and the incident is still assigned to p_expected (null = unassigned), so concurrent claims cannot both win.
create or replace function public.assign_incident(p_incident uuid, p_assignee uuid, p_actor uuid, p_expected uuid)
returns setof public.emergency_incidents
language plpgsql security definer set search_path = public as $$
begin
    perform 1 from dispatcher_duty d where d.user_id = p_assignee and d.status = 'on_duty' for share;
    if not found then
        raise exception 'ESRMS_NOT_ON_DUTY' using errcode = 'P0001';
    end if;
    return query
        update emergency_incidents e
           set assigned_to = p_assignee, assigned_at = now(), updated_by = p_actor
         where e.id = p_incident
           and e.archived_at is null
           and e.status not in ('resolved', 'cancelled')
           and e.assigned_to is not distinct from p_expected
        returning e.*;
    if not found then
        raise exception 'ESRMS_ASSIGN_CONFLICT' using errcode = 'P0001';
    end if;
end;
$$;

revoke all on function public.set_dispatcher_duty(uuid, text, uuid) from public, anon, authenticated;
revoke all on function public.assign_incident(uuid, uuid, uuid, uuid) from public, anon, authenticated;
grant execute on function public.set_dispatcher_duty(uuid, text, uuid) to service_role;
grant execute on function public.assign_incident(uuid, uuid, uuid, uuid) to service_role;
