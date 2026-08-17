-- ─── Meta token at rest ──────────────────────────────────────────────────────
-- 2026-08-16.
--
-- projects.meta_access_token held the customer's Meta access token in plaintext.
-- That token can create campaigns and spend money on their ad account, and the
-- App Review data-handling answer we are about to submit says, in writing,
-- "encrypted at rest in Supabase". Postgres disk encryption is not that, and
-- submitting the claim while the column is readable in the dashboard is the
-- kind of thing that is worse than not claiming it.
--
-- The new column holds AES-256-GCM in iv.tag.body base64url form, the same
-- construction reveal/lib/sites/crypto.ts already uses for Shopify admin
-- tokens, under the same COMMERCE_ENCRYPTION_KEY.
--
-- ⚠️ BOTH SIDES MUST AGREE. reveal/lib/ads/meta/oauth.ts encrypts,
-- adstra-backend/crypto.py decrypts. Changing either alone locks every customer
-- out of their own ad account.

begin;

alter table public.projects
  add column if not exists meta_access_token_enc text;

comment on column public.projects.meta_access_token_enc is
  'AES-256-GCM iv.tag.body base64url, key COMMERCE_ENCRYPTION_KEY. Written by '
  'reveal/lib/ads/meta/oauth.ts, read by adstra-backend/crypto.py. The plaintext '
  'meta_access_token column is legacy and is cleared on every reconnect.';

comment on column public.projects.meta_access_token is
  'LEGACY, plaintext. Do not write to this. New connections write '
  'meta_access_token_enc instead and null this out. Kept only so an account '
  'connected before 2026-08-16 keeps working until it reconnects.';

commit;
