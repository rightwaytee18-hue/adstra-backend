-- Run this in your Supabase SQL editor
-- Security fix: the original "FOR ALL" policy on competitor_ads had USING but
-- no WITH CHECK, so anon/authed clients could INSERT/UPDATE rows with a
-- spoofed user_id. Recreate the policy with WITH CHECK enforcing ownership.

DROP POLICY IF EXISTS "Users manage their own competitor ads" ON competitor_ads;

CREATE POLICY "Users manage their own competitor ads"
  ON competitor_ads FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);
