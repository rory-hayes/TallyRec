begin;

do $$
begin
  if not exists (
    select 1
    from pg_type t
    join pg_namespace n on n.oid = t.typnamespace
    where n.nspname = 'app' and t.typname = 'batch_status'
  ) then
    create type app.batch_status as enum ('queued', 'running', 'partially_failed', 'succeeded', 'failed');
  end if;
end
$$;

do $$
begin
  if not exists (
    select 1
    from pg_type t
    join pg_namespace n on n.oid = t.typnamespace
    where n.nspname = 'app' and t.typname = 'import_health_band'
  ) then
    create type app.import_health_band as enum ('green', 'amber', 'red');
  end if;
end
$$;

alter table public.runs
  add column if not exists payday_date date,
  add column if not exists must_close_by_date date,
  add column if not exists sla_reminder_state text;

create table if not exists public.run_batches (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references public.firms(id) on delete cascade,
  created_by uuid references auth.users(id) on delete set null,
  period_start date not null,
  period_end date not null,
  status app.batch_status not null default 'queued',
  requested_clients integer not null default 0 check (requested_clients >= 0),
  created_runs integer not null default 0 check (created_runs >= 0),
  queued_jobs integer not null default 0 check (queued_jobs >= 0),
  succeeded_runs integer not null default 0 check (succeeded_runs >= 0),
  failed_runs integer not null default 0 check (failed_runs >= 0),
  options jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.run_batch_items (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.run_batches(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  run_id uuid references public.runs(id) on delete set null,
  status app.batch_status not null default 'queued',
  bank_job_id uuid references public.jobs(id) on delete set null,
  gl_job_id uuid references public.jobs(id) on delete set null,
  error jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (batch_id, client_id)
);

create table if not exists public.client_uk_timing_policies (
  client_id uuid primary key references public.clients(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  tax_due_day integer not null default 22 check (tax_due_day between 1 and 31),
  pension_due_day integer not null default 22 check (pension_due_day between 1 and 31),
  bacs_visibility_business_days integer not null default 3 check (bacs_visibility_business_days between 0 and 10),
  holiday_calendar text not null default 'GB',
  enabled boolean not null default true,
  updated_at timestamptz not null default now()
);

alter table public.mapping_templates
  add column if not exists expected_headers jsonb,
  add column if not exists expected_header_hash text;

alter table public.source_files
  add column if not exists observed_headers jsonb,
  add column if not exists observed_header_hash text,
  add column if not exists import_validation_status text not null default 'pending',
  add column if not exists import_health_score integer;

create table if not exists public.run_import_health_summaries (
  run_id uuid primary key references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  health_score integer not null check (health_score between 0 and 100),
  health_band app.import_health_band not null,
  factors jsonb not null default '{}'::jsonb,
  computed_at timestamptz not null default now()
);

create index if not exists runs_firm_due_status_idx
  on public.runs(firm_id, must_close_by_date, status);
create index if not exists run_batches_firm_status_created_idx
  on public.run_batches(firm_id, status, created_at desc);
create index if not exists run_batch_items_batch_status_idx
  on public.run_batch_items(batch_id, status);
create index if not exists run_import_health_summaries_firm_band_idx
  on public.run_import_health_summaries(firm_id, health_band);
create index if not exists source_files_run_validation_idx
  on public.source_files(run_id, import_validation_status);

alter table public.run_batches enable row level security;
alter table public.run_batch_items enable row level security;
alter table public.client_uk_timing_policies enable row level security;
alter table public.run_import_health_summaries enable row level security;

create policy run_batches_access on public.run_batches
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy run_batch_items_access on public.run_batch_items
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy client_uk_timing_policies_access on public.client_uk_timing_policies
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy run_import_health_summaries_access on public.run_import_health_summaries
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

grant select, insert, update, delete on public.run_batches to authenticated;
grant select, insert, update, delete on public.run_batch_items to authenticated;
grant select, insert, update, delete on public.client_uk_timing_policies to authenticated;
grant select, insert, update, delete on public.run_import_health_summaries to authenticated;

commit;
