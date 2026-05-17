-- =============================================================================
--  PATCH-13: Supabase Row Level Security (RLS) Policy — Mobile Store Edition
--
--  This script:
--    1. Drops & recreates the 'leads' table (clean slate).
--    2. Enables RLS so no one can access data without a policy.
--    3. Creates an INSERT-ONLY policy for the service_role.
--    4. Creates a READ policy for authenticated dashboard users.
--
--  Run this in your Supabase SQL Editor:
--    Dashboard → SQL Editor → New Query → Paste & Run
-- =============================================================================

-- Step 1: Fresh table
DROP TABLE IF EXISTS leads;

CREATE TABLE leads (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name              TEXT NOT NULL,
  phone             TEXT NOT NULL,
  lead_type         TEXT NOT NULL DEFAULT 'product_inquiry',
  product_interest  TEXT,
  budget_range      TEXT,
  status            TEXT DEFAULT 'new_lead',
  created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Step 2: Enable Row Level Security
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;

-- Step 3: INSERT-ONLY policy for the backend (service_role)
-- Your Python app uses the service_role key, which bypasses RLS by default.
-- But if you ever switch to the anon key, this policy ensures it can ONLY
-- insert rows — never read, update, or delete existing lead data.
CREATE POLICY "Allow backend to insert leads"
  ON leads
  FOR INSERT
  TO anon
  WITH CHECK (true);

-- Step 4: READ policy — only authenticated users (e.g., store staff on dashboard)
-- can view lead data. The anon key (used by the public chat widget) CANNOT read.
CREATE POLICY "Allow authenticated users to read leads"
  ON leads
  FOR SELECT
  TO authenticated
  USING (true);

-- Step 5: UPDATE policy — staff can update lead status (e.g., 'contacted', 'converted', 'closed')
CREATE POLICY "Allow authenticated users to update lead status"
  ON leads
  FOR UPDATE
  TO authenticated
  USING (true)
  WITH CHECK (true);

-- Step 6: No DELETE policy — leads are never deleted, only archived.
-- This is intentional. If you need deletion, add a policy explicitly.

-- =============================================================================
--  VERIFICATION: Run this query to confirm RLS is active
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables WHERE tablename = 'leads';
-- Expected output: leads | true
