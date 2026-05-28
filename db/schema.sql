-- Prospect Engine — Contacts Table
-- Run this in your Supabase SQL editor

create table if not exists contacts (
  id                bigserial primary key,
  
  -- Core identity
  company           text,
  website           text unique,            -- dedup key
  industry          text,
  location          text,
  employee_count    text,
  
  -- Contact details
  email             text,
  first_name        text,
  last_name         text,
  phone             text,
  job_title         text,
  linkedin_url      text,
  
  -- Scoring
  opportunity_score integer default 0,
  icp_score         integer default 0,
  website_score     integer default 0,
  icp_tier          text default 'D',       -- A/B/C/D
  revenue_leak      boolean default false,
  intel_pills       jsonb default '[]',
  size_signals      jsonb default '[]',
  
  -- Pipeline status
  -- new → scored → enriched → report_sent → nurture → archived
  status            text default 'new',
  
  -- Report
  report_url        text,
  report_slug       text,
  
  -- Outreach tracking
  instantly_id      text,                   -- Instantly lead ID
  sequence_name     text,                   -- which Instantly sequence
  sent_at           timestamptz,
  replied_at        timestamptz,
  
  -- Timestamps
  scored_at         timestamptz,
  enriched_at       timestamptz,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);

-- Indexes for common queries
create index if not exists contacts_status_idx on contacts(status);
create index if not exists contacts_industry_idx on contacts(industry);
create index if not exists contacts_score_idx on contacts(opportunity_score desc);
create index if not exists contacts_icp_tier_idx on contacts(icp_tier);

-- Auto-update updated_at
create or replace function update_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger contacts_updated_at
  before update on contacts
  for each row execute function update_updated_at();

-- Row Level Security (enable if you want per-user isolation)
-- alter table contacts enable row level security;

-- Useful views
create or replace view contact_pipeline as
select
  status,
  count(*) as total,
  avg(opportunity_score) as avg_score,
  count(*) filter (where revenue_leak = true) as revenue_leaks,
  count(*) filter (where icp_tier in ('A','B')) as icp_qualified
from contacts
group by status
order by 
  case status 
    when 'new' then 1
    when 'scored' then 2
    when 'enriched' then 3
    when 'report_sent' then 4
    when 'nurture' then 5
    when 'archived' then 6
  end;
