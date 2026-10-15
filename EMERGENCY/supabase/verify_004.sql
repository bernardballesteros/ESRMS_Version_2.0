-- Read-only check for 004_citizen_app.sql. Run in the SQL Editor right after the migration,
-- in the SAME project (URL must contain speawbhwvwekhdltcauz). Every row should say ok = true.
select 'column emergency_incidents.client_request_id' as item,
       exists (select 1 from information_schema.columns
               where table_schema = 'public' and table_name = 'emergency_incidents'
                 and column_name = 'client_request_id') as ok
union all
select 'column emergency_incidents.location_source',
       exists (select 1 from information_schema.columns
               where table_schema = 'public' and table_name = 'emergency_incidents'
                 and column_name = 'location_source')
union all
select 'unique index on client_request_id',
       exists (select 1 from pg_indexes where schemaname = 'public'
                 and indexname = 'emergency_incidents_client_request_idx')
union all
select 'trigger emergency_incidents_citizen_guard',
       exists (select 1 from pg_trigger where tgname = 'emergency_incidents_citizen_guard')
union all
select 'function citizen_announcements(text)',
       to_regprocedure('public.citizen_announcements(text)') is not null
union all
select 'authenticated may execute citizen_announcements',
       coalesce(has_function_privilege('authenticated', to_regprocedure('public.citizen_announcements(text)'), 'execute'), false)
union all
select 'anon may NOT execute citizen_announcements',
       not coalesce(has_function_privilege('anon', to_regprocedure('public.citizen_announcements(text)'), 'execute'), true)
union all
select 'users has role/full_name/status columns',
       (select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'users'
          and column_name in ('role', 'full_name', 'status')) = 3
union all
select 'sign-up trigger on_auth_user_created (schema.sql)',
       exists (select 1 from pg_trigger where tgname = 'on_auth_user_created')
union all
select 'users.role accepts citizen',
       exists (select 1 from pg_constraint where conname = 'users_role_check'
                 and pg_get_constraintdef(oid) like '%citizen%')
union all
select 'RLS still enabled on emergency_incidents',
       (select relrowsecurity from pg_class where oid = 'public.emergency_incidents'::regclass);
