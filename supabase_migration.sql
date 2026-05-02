-- ============================================================
-- Adstra Phase 3 — Rules Engine Tables
-- Run this in Supabase SQL editor
-- ============================================================

-- 1. Rules
CREATE TABLE IF NOT EXISTS rules (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name text NOT NULL,
  level text NOT NULL CHECK (level IN ('campaign', 'adset', 'ad')),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
  conditions jsonb NOT NULL DEFAULT '[]',
  action text NOT NULL,
  action_value numeric,
  trigger_count integer NOT NULL DEFAULT 0,
  last_triggered_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE rules ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users manage own rules"
  ON rules FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- 2. Rule action log
CREATE TABLE IF NOT EXISTS rule_action_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  rule_id uuid REFERENCES rules(id) ON DELETE SET NULL,
  rule_name text NOT NULL,
  action text NOT NULL,
  target_id text,
  target_name text,
  metric text,
  value_before numeric,
  value_after numeric,
  roas numeric,
  spend numeric,
  purchases integer,
  triggered_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE rule_action_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users view own rule log"
  ON rule_action_log FOR SELECT
  USING (
    project_id IN (
      SELECT id FROM projects WHERE user_id = auth.uid()
    )
  );

-- 3. Budget changes (24h cooldown tracking)
CREATE TABLE IF NOT EXISTS budget_changes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  adset_id text NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE budget_changes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users view own budget changes"
  ON budget_changes FOR SELECT
  USING (
    project_id IN (
      SELECT id FROM projects WHERE user_id = auth.uid()
    )
  );
