-- ─── Autopilot: schema repair + Reveal bridge ────────────────────────────────
-- 2026-08-15. Applied to the shared "Adstra" project (iuhabzdacuwbrprnfuqo),
-- which holds BOTH the engine tables (projects, autopilot_*, rules) and
-- Reveal's tables (tenants, tenant_modules, module_metrics).
--
-- Four jobs, in order of how badly they were broken.
--
--   1. Repair the approval path, which could never have run.
--   2. Capture schema that existed only in the live database.
--   3. Bridge an engine project to a Reveal tenant.
--   4. Add the safety and reporting surfaces the portal needs.

begin;

-- ─── 1. The approval path was dead on arrival ────────────────────────────────
-- autopilot_engine.execute_approved_action claims an action by flipping
-- pending -> 'executing' in a single conditional update. That value was never
-- in the CHECK constraint, so every approval a customer ever clicked would have
-- raised 23514 before touching Meta. The claim is the concurrency guard that
-- stops a double-click running a pause twice, so it cannot simply be removed.
alter table public.autopilot_actions
  drop constraint if exists autopilot_actions_status_check;

alter table public.autopilot_actions
  add constraint autopilot_actions_status_check
  check (status in ('pending', 'approved', 'executing', 'rejected', 'executed', 'failed'));

-- ─── 2. Schema that lived only in production ─────────────────────────────────
-- These three were added by hand in the dashboard and appear in no .sql file in
-- either repo, so a rebuild from source produced an engine whose bootstrap
-- wrote to columns that did not exist. They are already live; these statements
-- exist so the next rebuild matches.
alter table public.autopilot_settings
  add column if not exists bootstrapped     boolean default false,
  add column if not exists bootstrap_status text default 'pending',
  add column if not exists bootstrap_error  text;

-- published_campaigns is the audit record of what the engine put on Meta.
-- campaign_builder._save_published swallows its own exceptions, so the missing
-- table never crashed anything. It did something quieter and worse: every
-- campaign the autopilot published left no local record of the ids it created,
-- and the portal's "your ads" list has nothing to read.
--
-- campaign_drafts is deliberately NOT recreated. Its only reader was the
-- adstra-app campaign wizard, which is being retired.
create table if not exists public.published_campaigns (
  id                 uuid primary key default gen_random_uuid(),
  project_id         uuid not null references public.projects(id) on delete cascade,
  user_id            uuid references public.profiles(id) on delete cascade,
  draft_id           uuid,
  meta_campaign_id   text not null,
  meta_adset_ids     text[] default '{}',
  meta_ad_ids        text[] default '{}',
  name               text,
  objective          text,
  daily_budget_cents int,
  publish_status     text default 'paused',
  publish_result     jsonb,
  created_at         timestamptz default now()
);

-- user_id is nullable here on purpose: a campaign published for a Reveal tenant
-- has no profiles row behind it. See section 3.
create index if not exists published_campaigns_project_idx
  on public.published_campaigns (project_id, created_at desc);

alter table public.published_campaigns enable row level security;

-- ─── 3. Bridge: an engine project can belong to a Reveal tenant ──────────────
-- projects.user_id was NOT NULL with an FK to profiles, which is an auth.users
-- row. A Reveal tenant has no auth user at all, so a tenant could never own an
-- engine project. Making user_id nullable and adding tenant_id is the whole
-- bridge; the token stays in projects.meta_access_token where it always was,
-- and NEVER goes in tenant_modules.config, which /api/portal/me serves to the
-- browser whole.
alter table public.projects
  alter column user_id drop not null;

alter table public.projects
  add column if not exists tenant_id uuid references public.tenants(id) on delete cascade;

-- What counts as a result for this advertiser. The engine previously counted the
-- Meta action type "purchase" and nothing else, which is right for an ecommerce
-- store and catastrophic for a plumber: a service business emits `lead`,
-- `onsite_conversion.call_confirm` and messaging actions, never a purchase. Its
-- measured ROAS was therefore always 0.0, and the kill rule ("ROAS under 0.5,
-- pause it") matched every ad on the account.
--
-- Defaults to 'lead' because that is the safe direction. Mis-labelling a store as
-- lead-gen under-reports its revenue; mis-labelling a plumber as a store reports
-- zero results forever and switches their account off.
alter table public.projects
  add column if not exists goal text not null default 'lead';

alter table public.projects
  drop constraint if exists projects_goal_ck;

alter table public.projects
  add constraint projects_goal_ck check (goal in ('purchase', 'lead', 'awareness'));

comment on column public.projects.goal is
  'purchase | lead | awareness. Set from the Reveal intake question "Main goal" '
  '(More sales / More leads / Awareness). Decides which Meta action families '
  'count as a result and whether ROAS exists at all. See conversions.py.';

create unique index if not exists projects_tenant_uniq
  on public.projects (tenant_id) where tenant_id is not null;

-- Exactly one owner, never both, never neither. Without this a row with both a
-- user_id and a tenant_id would be reachable from two products with two
-- different ideas about who is allowed to spend the money.
alter table public.projects
  drop constraint if exists projects_one_owner_ck;

alter table public.projects
  add constraint projects_one_owner_ck
  check ((user_id is not null) <> (tenant_id is not null));

-- Every table the engine writes carries a NOT NULL user_id with an FK to
-- profiles, which is an auth.users row. A Reveal tenant has neither, so a
-- tenant-owned project could not have settings saved, an action queued, a
-- briefing written or a notification raised: the entire write path failed at
-- the first insert.
--
-- Nullable rather than inventing a profiles row per tenant. The RLS policies on
-- these tables are all `auth.uid() = user_id`, so a null user_id is invisible
-- to every anon and authenticated client and reachable only by the service role,
-- which is exactly how Reveal reads them. Inventing auth users would instead
-- create real, loginable accounts nobody owns.
alter table public.autopilot_settings  alter column user_id drop not null;
alter table public.autopilot_actions   alter column user_id drop not null;
alter table public.autopilot_briefings alter column user_id drop not null;
alter table public.notifications       alter column user_id drop not null;
alter table public.rules               alter column user_id drop not null;

-- ─── 4a. Safety valves the engine did not have ───────────────────────────────
-- max_daily_budget_usd is a ceiling PER ENTITY, so ten ad sets at the cap is ten
-- times the number the customer believes they set. max_account_daily_usd is the
-- real total and is checked before any increase.
--
-- paused_at is the per-project stop. killed_at on autopilot_control below is the
-- account-wide one. Neither existed; the only off lever was pulling a Railway
-- env var.
alter table public.autopilot_settings
  add column if not exists max_account_daily_usd    numeric default 1000.0,
  add column if not exists min_conversions_to_scale integer default 50,
  add column if not exists scale_cooldown_hours     integer default 72,
  add column if not exists target_cost_per_result   numeric,
  add column if not exists paused_at                timestamptz;

comment on column public.autopilot_settings.target_cost_per_result is
  'What the customer is willing to pay for one enquiry or one sale, in dollars. '
  'This is the only performance number a non-technical owner can actually answer, '
  'which is why it is the one the portal asks for. Null means judge against the '
  'account average instead of an absolute figure.';

comment on column public.autopilot_settings.min_conversions_to_scale is
  'Meta re-enters the learning phase on a budget edit, and a reset costs more '
  'than the edit gains. No budget change is made to an entity below this many '
  'conversions in the trailing window. 50 is Meta''s own stated benchmark.';

comment on column public.autopilot_settings.scale_cooldown_hours is
  'Minimum hours between budget increases on the same entity. Meta guidance is '
  'no more than +20% every 3 to 4 days; the daily loop would otherwise hit the '
  'same entity every single morning.';

-- One row. The stop-everything switch.
create table if not exists public.autopilot_control (
  id         boolean primary key default true,
  killed_at  timestamptz,
  killed_by  text,
  reason     text,
  updated_at timestamptz default now(),
  constraint autopilot_control_singleton check (id)
);

insert into public.autopilot_control (id) values (true) on conflict (id) do nothing;

alter table public.autopilot_control enable row level security;

-- ─── 4b. Cooldown needs to know direction and level ──────────────────────────
-- budget_changes tracked (project, adset, when) only, so it could not express
-- "increases wait 72 hours but decreases may run today". A decrease can only
-- save money and should never be held back by a cooldown meant to protect
-- learning.
alter table public.budget_changes
  add column if not exists entity_level text default 'adset',
  add column if not exists direction    text;

create index if not exists budget_changes_lookup_idx
  on public.budget_changes (project_id, adset_id, changed_at desc);

-- ─── 4c. The daily snapshot the portal reads ─────────────────────────────────
-- The portal must never call Meta on page load. Until App Review grants
-- Advanced Access the app is on the Dev tier, which allows 600 ads_insights
-- calls per hour per ad account against 190,000 on Standard, so a customer
-- refreshing their dashboard could exhaust the quota the autopilot needs to do
-- its job. The engine writes this once per run and the portal reads only this.
create table if not exists public.ad_insights_daily (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references public.projects(id) on delete cascade,
  day           date not null,
  entity_level  text not null check (entity_level in ('account', 'campaign', 'adset', 'ad')),
  entity_id     text not null,
  entity_name   text,
  parent_id     text,
  status        text,
  spend_cents   bigint  not null default 0,
  impressions   bigint  not null default 0,
  clicks        bigint  not null default 0,
  conversions   numeric not null default 0,
  revenue_cents bigint  not null default 0,
  thumbnail_url text,
  fetched_at    timestamptz default now(),
  constraint ad_insights_daily_uniq unique (project_id, day, entity_level, entity_id)
);

-- The portal's default read is "this project, last 30 days, newest first".
create index if not exists ad_insights_daily_read_idx
  on public.ad_insights_daily (project_id, day desc, entity_level);

alter table public.ad_insights_daily enable row level security;

-- Money is stored in cents and conversions as numeric, matching module_metrics.
-- Meta returns spend as a decimal string of major units; converting once at the
-- boundary keeps float arithmetic out of every downstream sum.
comment on table public.ad_insights_daily is
  'One row per entity per day. Written by the engine, read by the Reveal portal. '
  'The portal never calls the Meta API directly.';

commit;
