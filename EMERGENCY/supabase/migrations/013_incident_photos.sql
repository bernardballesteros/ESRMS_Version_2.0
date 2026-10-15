-- ESRMS Citizen App: let a signed-in citizen attach a photo to their own SOS report.
-- Run in Supabase Dashboard -> SQL Editor after 007_citizen_avatars.sql. Additive and safe to re-run.
--
-- Photos go to the existing public 'esrms-media' bucket (2 MB limit, PNG/JPEG/WebP only) as
-- incidents/<own user id>/<random id>.<ext>. The app writes the public URL into the report's
-- description, so emergency_incidents needs no new column. Insert only: a citizen cannot replace
-- or delete a photo once it has been sent, and cannot write into another citizen's folder.

drop policy if exists "Citizens upload own incident photo" on storage.objects;
create policy "Citizens upload own incident photo"
    on storage.objects for insert to authenticated
    with check (bucket_id = 'esrms-media' and name like 'incidents/' || auth.uid() || '/%');
