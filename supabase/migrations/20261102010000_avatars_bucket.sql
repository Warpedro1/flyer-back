-- Private Storage bucket for profile photos (#24, decisions 2026-11-01).
--
-- The app resizes the photo to 512×512 in the browser and uploads it with the person's
-- own session into avatars/{uid}/{uuid}.ext; the backend checks the path, moderates the
-- image and only then stores it in profiles.avatar_path. Every logged-in user sees the
-- photo, always through a short-lived URL the backend signs (no SELECT policy here).
-- Kept apart from 20261102000000_public_identity because the integration tests have no
-- `storage` schema.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'avatars',
  'avatars',
  false,
  2097152,  -- 2 MB
  array['image/jpeg', 'image/png', 'image/webp']
)
on conflict (id) do update
  set public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

-- Each person uploads only into their own folder: avatars/{uid}/file. File names are
-- new uuids (upsert off), so there is no UPDATE policy.
drop policy if exists "avatars_insert_own" on storage.objects;
create policy "avatars_insert_own"
on storage.objects for insert to authenticated
with check (
  bucket_id = 'avatars'
  and (storage.foldername(name))[1] = (select auth.uid())::text
);

drop policy if exists "avatars_delete_own" on storage.objects;
create policy "avatars_delete_own"
on storage.objects for delete to authenticated
using (bucket_id = 'avatars' and (storage.foldername(name))[1] = (select auth.uid())::text);

-- NO SELECT policy on purpose: photos are read only through URLs signed by the backend.
