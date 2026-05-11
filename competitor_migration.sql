-- Run this in your Supabase SQL editor

-- 1. Create competitor_ads table
CREATE TABLE IF NOT EXISTS competitor_ads (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  project_id uuid REFERENCES projects(id) ON DELETE CASCADE NOT NULL,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  ad_id text NOT NULL,
  page_name text,
  body text,
  snapshot_url text,
  source_type text,
  source_value text,
  is_active boolean DEFAULT true,
  is_saved boolean DEFAULT false,
  atlas_analysis jsonb,
  first_seen timestamptz DEFAULT now(),
  last_seen timestamptz DEFAULT now(),
  created_at timestamptz DEFAULT now(),
  UNIQUE(project_id, ad_id)
);

ALTER TABLE competitor_ads ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users manage their own competitor ads"
  ON competitor_ads FOR ALL
  USING (auth.uid() = user_id);

-- 2. After running the SQL above, go to Storage in Supabase dashboard and:
--    - Create a new bucket called: competitor-ads
--    - Set it to PUBLIC (so images are accessible in the app)
