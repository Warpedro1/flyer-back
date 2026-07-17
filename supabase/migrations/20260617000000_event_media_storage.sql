-- Private Storage bucket for event media (uploads) + RLS policies.
-- Apply in Supabase SQL editor or via CLI. Backend signs read URLs with service_role.

-- Private bucket with size/mime limits as defense-in-depth.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'event-media',
  'event-media',
  false,
  52428800,  -- 50 MB
  array['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'video/mp4', 'video/webm']
)
on conflict (id) do update
  set public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

-- Authenticated users may upload only into their OWN folder: event-media/{uid}/file
drop policy if exists "event_media_insert_own" on storage.objects;
create policy "event_media_insert_own"
on storage.objects for insert to authenticated
with check (
  bucket_id = 'event-media'
  and (storage.foldername(name))[1] = auth.uid()::text
);

-- (Opcional) gerenciar os próprios arquivos.
drop policy if exists "event_media_update_own" on storage.objects;
create policy "event_media_update_own"
on storage.objects for update to authenticated
using (bucket_id = 'event-media' and (storage.foldername(name))[1] = auth.uid()::text);

drop policy if exists "event_media_delete_own" on storage.objects;
create policy "event_media_delete_own"
on storage.objects for delete to authenticated
using (bucket_id = 'event-media' and (storage.foldername(name))[1] = auth.uid()::text);

-- NO SELECT policy on purpose: objects are not readable directly (nor by other users).
-- Reads always go through the backend, which mints short-lived signed URLs via service_role.

-- Remove legacy policies (created before this feature) that conflicted with the design:
--  * public SELECT made the bucket world-readable (the security hole);
--  * Business-only INSERT is redundant now that any authenticated user uploads to
--    their own folder via event_media_insert_own.
drop policy if exists "Visualização de media de eventos" on storage.objects;
drop policy if exists "Empresas carregam media" on storage.objects;
