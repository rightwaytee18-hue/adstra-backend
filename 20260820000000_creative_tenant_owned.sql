-- Creative generation and competitor scraping for tenant-owned projects.
--
-- The 2026-08-15 bridge migration made projects.user_id nullable so a Reveal
-- tenant could own a project without inventing a real, loginable auth user, and
-- dropped NOT NULL on the five engine tables that hang off it. It missed these
-- two, because they were created by their own standalone migrations rather than
-- by the phases-8-9-10 batch.
--
-- The consequence was silent and total: creative_engine writes
-- project['user_id'], which is NULL for every tenant-owned project, so the
-- insert raised 23502 on every attempt. bootstrap_project catches that as
-- "Creative generation failed", appends a warning, and marks bootstrap complete
-- anyway. So every Reveal customer's account was bootstrapped with campaigns
-- carrying no generated creative, and the autopilot could pause a losing ad but
-- could never build the replacement it had already queued. No creative has ever
-- been generated for a Reveal tenant.
--
-- Same reasoning as the bridge migration: RLS on both tables is
-- `auth.uid() = user_id`, so a null user_id is invisible to every anon and
-- authenticated client and reachable only by the service role, which is exactly
-- how Reveal reads them.

alter table public.creative_generations alter column user_id drop not null;
alter table public.competitor_ads       alter column user_id drop not null;

comment on column public.creative_generations.user_id is
  'Adstra owner. NULL for Reveal tenant-owned projects, which are reached only by the service role. See projects.tenant_id.';

comment on column public.competitor_ads.user_id is
  'Adstra owner. NULL for Reveal tenant-owned projects, which are reached only by the service role. See projects.tenant_id.';
