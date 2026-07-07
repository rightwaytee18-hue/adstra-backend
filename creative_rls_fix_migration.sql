-- Run this in your Supabase SQL editor
-- Security fix: the original "FOR ALL" policy on creative_generations had
-- USING but no WITH CHECK, so anon/authed clients could INSERT/UPDATE rows
-- with a spoofed user_id. Recreate the policy with WITH CHECK enforcing ownership.

DROP POLICY IF EXISTS "Users manage their own creatives" ON creative_generations;

CREATE POLICY "Users manage their own creatives"
  ON creative_generations FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);
