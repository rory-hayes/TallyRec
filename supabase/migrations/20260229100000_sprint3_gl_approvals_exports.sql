begin;

alter type app.job_type add value if not exists 'reconcile_gl';
alter type app.job_type add value if not exists 'export_pack';

alter type app.run_status add value if not exists 'ready_for_review';
alter type app.run_status add value if not exists 'approved';

do $$
begin
  if not exists (
    select 1
    from pg_type t
    join pg_namespace n on n.oid = t.typnamespace
    where n.nspname = 'app' and t.typname = 'gl_bucket'
  ) then
    create type app.gl_bucket as enum ('net_pay_control', 'taxes', 'pension', 'other');
  end if;
end
$$;

do $$
begin
  if not exists (
    select 1
    from pg_type t
    join pg_namespace n on n.oid = t.typnamespace
    where n.nspname = 'app' and t.typname = 'variance_resolution_action'
  ) then
    create type app.variance_resolution_action as enum ('matched', 'explained', 'expected_later', 'ignored');
  end if;
end
$$;

alter table public.payroll_expected
  add column if not exists tax_amount numeric(14,2) not null default 0,
  add column if not exists pension_amount numeric(14,2) not null default 0,
  add column if not exists other_amount numeric(14,2) not null default 0;

alter table public.runs
  add column if not exists locked_at timestamptz,
  add column if not exists locked_by uuid references auth.users(id) on delete set null,
  add column if not exists lock_reason text;

alter table public.approvals
  add column if not exists prepared_by uuid references auth.users(id) on delete set null,
  add column if not exists prepared_at timestamptz,
  add column if not exists reviewer_id uuid references auth.users(id) on delete set null,
  add column if not exists reviewed_at timestamptz;

create unique index if not exists approvals_run_uidx on public.approvals(run_id);

create table if not exists public.client_gl_bucket_accounts (
  id uuid primary key default gen_random_uuid(),
  client_id uuid not null references public.clients(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  bucket app.gl_bucket not null,
  account_code text not null,
  created_at timestamptz not null default now(),
  unique (client_id, bucket, account_code)
);

create table if not exists public.run_gl_tieout_summaries (
  run_id uuid primary key references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  payroll_totals jsonb not null,
  gl_totals jsonb not null,
  deltas jsonb not null,
  is_balanced boolean not null,
  status app.tieout_status not null,
  rules_used jsonb not null,
  computed_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.variances
  add column if not exists resolution_action app.variance_resolution_action,
  add column if not exists note text,
  add column if not exists changed_by uuid references auth.users(id) on delete set null,
  add column if not exists changed_at timestamptz,
  add column if not exists ignored_needs_reviewer_approval boolean not null default false,
  add column if not exists ignored_approved_by uuid references auth.users(id) on delete set null,
  add column if not exists ignored_approved_at timestamptz;

create table if not exists public.variance_resolution_events (
  id uuid primary key default gen_random_uuid(),
  variance_id uuid not null references public.variances(id) on delete cascade,
  run_id uuid not null references public.runs(id) on delete cascade,
  firm_id uuid not null references public.firms(id) on delete cascade,
  client_id uuid not null references public.clients(id) on delete cascade,
  action text not null,
  note text,
  actor_user_id uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

alter table public.export_packs
  add column if not exists pack_hash text,
  add column if not exists storage_bucket text,
  add column if not exists storage_path text,
  add column if not exists deterministic_manifest jsonb,
  add column if not exists generated_at timestamptz,
  add column if not exists error jsonb,
  add column if not exists retry_count integer not null default 0;

create unique index if not exists export_packs_run_hash_uidx
  on public.export_packs(run_id, pack_hash)
  where pack_hash is not null;

create index if not exists client_gl_bucket_accounts_client_bucket_idx
  on public.client_gl_bucket_accounts(client_id, bucket);
create index if not exists run_gl_tieout_summaries_status_idx
  on public.run_gl_tieout_summaries(status);
create index if not exists variances_resolution_idx
  on public.variances(run_id, resolution_action, status);
create index if not exists variance_resolution_events_variance_idx
  on public.variance_resolution_events(variance_id, created_at desc);

alter table public.client_gl_bucket_accounts enable row level security;
alter table public.run_gl_tieout_summaries enable row level security;
alter table public.variance_resolution_events enable row level security;

create policy client_gl_bucket_accounts_access on public.client_gl_bucket_accounts
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy run_gl_tieout_summaries_access on public.run_gl_tieout_summaries
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

create policy variance_resolution_events_access on public.variance_resolution_events
  using (app.is_firm_member(firm_id))
  with check (app.is_firm_member(firm_id));

grant select, insert, update, delete on public.client_gl_bucket_accounts to authenticated;
grant select, insert, update, delete on public.run_gl_tieout_summaries to authenticated;
grant select, insert, update, delete on public.variance_resolution_events to authenticated;

commit;
