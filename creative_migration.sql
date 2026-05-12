-- Run this in your Supabase SQL editor

-- 1. Create creative_generations table
CREATE TABLE IF NOT EXISTS creative_generations (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  project_id uuid REFERENCES projects(id) ON DELETE CASCADE NOT NULL,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  mode text NOT NULL,
  prompt text,
  image_url text NOT NULL,
  is_saved boolean DEFAULT false,
  metadata jsonb,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE creative_generations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users manage their own creatives"
  ON creative_generations FOR ALL
  USING (auth.uid() = user_id);

-- 2. After running the SQL above, go to Storage in Supabase dashboard and:
--    - Create a new bucket called: creatives
--    - Set it to PUBLIC (so images are accessible in the app)
