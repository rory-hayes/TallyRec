begin;

create extension if not exists pgcrypto;
create extension if not exists citext;

create schema if not exists app;
create schema if not exists auth;

create table if not exists auth.users (
  id uuid primary key,
  email text,
  created_at timestamptz not null default now()
);

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
end
$$;

create type app.membership_role as enum ('owner', 'admin', 'analyst', 'viewer');
create type app.run_status as enum ('draft', 'queued', 'processing', 'completed', 'failed');
create type app.file_kind as enum ('bank', 'payroll', 'gl');
create type app.job_type as enum ('ingest', 'reconcile', 'noop', 'reconcile_bank');
create type app.job_status as enum ('queued', 'running', 'succeeded', 'failed');
create type app.match_group_kind as enum ('one_to_one', 'one_to_many', 'many_to_one');
create type app.match_confidence as enum ('deterministic', 'ambiguous');
create type app.match_member_type as enum ('payroll_expected', 'bank_transaction');
create type app.variance_status as enum ('open', 'resolved', 'ignored');
create type app.variance_severity as enum ('blocker', 'review');
create type app.approval_status as enum ('pending', 'approved', 'rejected');
create type app.export_pack_status as enum ('pending', 'generated', 'failed');
create type app.tieout_status as enum ('Tied', 'Not tied', 'Needs review');

create or replace function app.current_user_id()
returns uuid
language sql
stable
as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;

create table public.firms (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  slug citext not null unique,
  created_at timestamptz not null default now()
);

create table public.firm_memberships (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references public.firms(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role app.membership_role not null default 'viewer',
  created_at timestamptz not null default now(),
  unique (firm_id, user_id)
);

create or replace function app.is_firm_member(check_firm_id uuid)
returns boolean
language sql
stable
as $$
  select exists (
    select 1
    from public.firm_memberships fm
    where fm.firm_id = check_firm_id
      and fm.user_id = app.current_user_id()
  )
$$;

create table public.clients (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references public.firms(id) on delete cascade,
  name text not null,
  external_ref text,
  created_at timestamptz not null default now(),
  unique (firm_id, name)
);

create table public.client_recon_policies (
  client_id uuid primary key references public.clients(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  amount_tolerance numeric(14,2) not null default 0.01 check (amount_tolerance >= 0),
  date_window_days integer not null default 5 check (date_window_days between 0 and 60),
  max_group_size integer not null default 5 check (max_group_size between 1 and 10),
  enable_one_to_many boolean not null default true,
  enable_many_to_one boolean not null default true,
  require_allowed_account boolean not null default true,
  updated_at timestamptz not null default now()
);

create table public.client_bank_accounts (
  id uuid primary key default gen_random_uuid(),
  client_id uuid not null references public.clients(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  account_ref text not null,
  label text,
  created_at timestamptz not null default now(),
  unique (client_id, account_ref)
);

create table public.runs (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  period_start date not null,
  period_end date not null,
  status app.run_status not null default 'draft',
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (period_start <= period_end)
);

create table public.mapping_templates (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid references public.clients(id) on delete cascade,
  name text not null,
  file_kind app.file_kind not null,
  mapping jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (firm_id, name, file_kind)
);

create table public.source_files (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  file_kind app.file_kind not null,
  filename text not null,
  uri text not null,
  checksum_sha256 text not null check (checksum_sha256 ~ '^[a-f0-9]{64}$'),
  byte_size bigint not null check (byte_size >= 0),
  mapping_template_id uuid references public.mapping_templates(id) on delete set null,
  registered_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

create table public.payroll_expected (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  employee_ref text,
  payment_date date not null,
  net_amount numeric(14,2) not null check (net_amount >= 0),
  currency text not null default 'GBP',
  source_file_id uuid references public.source_files(id) on delete set null,
  source_row_number integer,
  deterministic_hash text not null,
  created_at timestamptz not null default now(),
  unique (run_id, deterministic_hash),
  unique (source_file_id, source_row_number)
);

create table public.bank_transactions (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  posted_date date not null,
  amount_signed numeric(14,2) not null,
  currency text not null default 'GBP',
  description text,
  account_ref text,
  source_file_id uuid references public.source_files(id) on delete set null,
  source_row_number integer,
  deterministic_hash text not null,
  created_at timestamptz not null default now(),
  unique (run_id, deterministic_hash),
  unique (source_file_id, source_row_number)
);

create table public.gl_journal_lines (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  entry_date date not null,
  account_code text not null,
  description text,
  debit_amount numeric(14,2) not null default 0 check (debit_amount >= 0),
  credit_amount numeric(14,2) not null default 0 check (credit_amount >= 0),
  currency text not null default 'GBP',
  source_file_id uuid references public.source_files(id) on delete set null,
  source_row_number integer,
  deterministic_hash text not null,
  created_at timestamptz not null default now(),
  unique (run_id, deterministic_hash),
  unique (source_file_id, source_row_number)
);

create table public.jobs (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  job_type app.job_type not null,
  status app.job_status not null default 'queued',
  payload jsonb not null default '{}'::jsonb,
  idempotency_key text,
  attempt_count integer not null default 0,
  max_attempts integer not null default 3 check (max_attempts > 0),
  queued_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  result jsonb,
  error jsonb,
  created_by uuid references auth.users(id) on delete set null
);

create unique index jobs_run_idempotency_uidx
  on public.jobs(run_id, idempotency_key)
  where idempotency_key is not null;

create table public.match_groups (
  id uuid primary key,
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  group_kind app.match_group_kind not null,
  match_confidence app.match_confidence not null default 'deterministic',
  expected_total numeric(14,2) not null,
  bank_total numeric(14,2) not null,
  delta numeric(14,2) not null,
  matched_on date,
  algorithm_version text not null default 'bank_v1',
  created_at timestamptz not null default now()
);

create table public.match_group_members (
  id uuid primary key default gen_random_uuid(),
  match_group_id uuid not null references public.match_groups(id) on delete cascade,
  member_type app.match_member_type not null,
  member_id uuid not null,
  signed_amount numeric(14,2) not null,
  created_at timestamptz not null default now(),
  unique (match_group_id, member_type, member_id)
);

create table public.variances (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  code text not null,
  category text not null,
  severity app.variance_severity not null,
  status app.variance_status not null default 'open',
  message text not null,
  amount numeric(14,2),
  event_date date,
  account_ref text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  resolved_at timestamptz,
  resolved_by uuid references auth.users(id) on delete set null
);

create table public.approvals (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  status app.approval_status not null default 'pending',
  approved_by uuid references auth.users(id) on delete set null,
  approved_at timestamptz,
  notes text,
  created_at timestamptz not null default now()
);

create table public.audit_events (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid references public.firms(id) on delete cascade,
  client_id uuid references public.clients(id) on delete cascade,
  run_id uuid references public.runs(id) on delete cascade,
  event_type text not null,
  actor_user_id uuid references auth.users(id) on delete set null,
  entity_type text,
  entity_id uuid,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table public.export_packs (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  status app.export_pack_status not null default 'pending',
  artifact_uri text,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.run_bank_tieout_summaries (
  run_id uuid primary key references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  expected_net_pay numeric(14,2) not null,
  matched_bank_total numeric(14,2) not null,
  delta numeric(14,2) not null,
  status app.tieout_status not null,
  policy_snapshot jsonb not null default '{}'::jsonb,
  computed_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index firm_memberships_user_idx on public.firm_memberships(user_id);
create index clients_firm_idx on public.clients(firm_id);
create index runs_firm_client_status_idx on public.runs(firm_id, client_id, status);
create index source_files_run_kind_idx on public.source_files(run_id, file_kind);
create index payroll_expected_run_idx on public.payroll_expected(run_id);
create index bank_transactions_run_idx on public.bank_transactions(run_id);
create index jobs_queue_idx on public.jobs(status, queued_at, id);
create index variances_run_code_status_idx on public.variances(run_id, code, status);
create index variances_run_severity_status_idx on public.variances(run_id, severity, status);
create index match_groups_run_idx on public.match_groups(run_id);
create index match_group_members_group_idx on public.match_group_members(match_group_id);
create index audit_events_run_created_idx on public.audit_events(run_id, created_at desc);
create index client_recon_policies_firm_idx on public.client_recon_policies(firm_id);
create index client_bank_accounts_firm_idx on public.client_bank_accounts(firm_id);

grant usage on schema app to authenticated;
grant usage on schema public to authenticated;
grant select, insert, update, delete on all tables in schema public to authenticated;

alter table public.firms enable row level security;
alter table public.firm_memberships enable row level security;
alter table public.clients enable row level security;
alter table public.client_recon_policies enable row level security;
alter table public.client_bank_accounts enable row level security;
alter table public.runs enable row level security;
alter table public.mapping_templates enable row level security;
alter table public.source_files enable row level security;
alter table public.payroll_expected enable row level security;
alter table public.bank_transactions enable row level security;
alter table public.gl_journal_lines enable row level security;
alter table public.jobs enable row level security;
alter table public.match_groups enable row level security;
alter table public.match_group_members enable row level security;
alter table public.variances enable row level security;
alter table public.approvals enable row level security;
alter table public.audit_events enable row level security;
alter table public.export_packs enable row level security;
alter table public.run_bank_tieout_summaries enable row level security;

create policy firms_select on public.firms
  for select
  using (app.is_firm_member(id));

create policy firms_insert on public.firms
  for insert
  with check (app.current_user_id() is not null);

create policy firms_update on public.firms
  for update
  using (app.is_firm_member(id))
  with check (app.is_firm_member(id));

create policy firms_delete on public.firms
  for delete
  using (app.is_firm_member(id));

create policy firm_memberships_select on public.firm_memberships
  for select
  using (app.is_firm_member(firm_id));

create policy firm_memberships_insert on public.firm_memberships
  for insert
  with check (app.is_firm_member(firm_id) or user_id = app.current_user_id());

create policy firm_memberships_update on public.firm_memberships
  for update
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy firm_memberships_delete on public.firm_memberships
  for delete
  using (app.is_firm_member(firm_id));

create policy clients_access on public.clients
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy client_recon_policies_access on public.client_recon_policies
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy client_bank_accounts_access on public.client_bank_accounts
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy runs_access on public.runs
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy mapping_templates_access on public.mapping_templates
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy source_files_access on public.source_files
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy payroll_expected_access on public.payroll_expected
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy bank_transactions_access on public.bank_transactions
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy gl_journal_lines_access on public.gl_journal_lines
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy jobs_access on public.jobs
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy match_groups_access on public.match_groups
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy match_group_members_access on public.match_group_members
  using (
    exists (
      select 1 from public.match_groups mg
      where mg.id = match_group_id
      and app.is_firm_member(mg.firm_id)
    )
  )
  with check (
    exists (
      select 1 from public.match_groups mg
      where mg.id = match_group_id
      and app.is_firm_member(mg.firm_id)
    )
  );

create policy variances_access on public.variances
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy approvals_access on public.approvals
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy audit_events_access on public.audit_events
  using (firm_id is null or app.is_firm_member(firm_id))
  with check (firm_id is null or app.is_firm_member(firm_id));

create policy export_packs_access on public.export_packs
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy run_bank_tieout_summaries_access on public.run_bank_tieout_summaries
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

commit;
