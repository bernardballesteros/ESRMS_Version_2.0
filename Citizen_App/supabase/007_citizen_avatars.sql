-- ESRMS Citizen App: let a signed-in citizen upload, replace and delete ONLY their own profile picture.
-- Run in Supabase Dashboard -> SQL Editor after 005_account_settings.sql (which creates the public
-- 'esrms-media' bucket, 2 MB limit, PNG/JPEG/WebP only). Additive and safe to re-run.
--
-- Until now uploads went only through the Flask dashboard (service role). The app has just the
-- public key, so Storage needs policies. The object name is fixed to avatars/<own user id>.<ext>,
-- the same name the dashboard uses, so nobody can write or delete another person's file.

drop policy if exists "Citizens upload own avatar" on storage.objects;
create policy "Citizens upload own avatar"
    on storage.objects for insert to authenticated
    with check (bucket_id = 'esrms-media'
                and name in ('avatars/' || auth.uid() || '.png', 'avatars/' || auth.uid() || '.jpg',
                             'avatars/' || auth.uid() || '.webp'));

drop policy if exists "Citizens replace own avatar" on storage.objects;
create policy "Citizens replace own avatar"
    on storage.objects for update to authenticated
    using (bucket_id = 'esrms-media' and name like 'avatars/' || auth.uid() || '.%')
    with check (bucket_id = 'esrms-media'
                and name in ('avatars/' || auth.uid() || '.png', 'avatars/' || auth.uid() || '.jpg',
                             'avatars/' || auth.uid() || '.webp'));

drop policy if exists "Citizens delete own avatar" on storage.objects;
create policy "Citizens delete own avatar"
    on storage.objects for delete to authenticated
    using (bucket_id = 'esrms-media' and name like 'avatars/' || auth.uid() || '.%');

-- Replacing a picture (upsert) also needs to see the existing object.
drop policy if exists "Citizens see own avatar object" on storage.objects;
create policy "Citizens see own avatar object"
    on storage.objects for select to authenticated
    using (bucket_id = 'esrms-media' and name like 'avatars/' || auth.uid() || '.%');
