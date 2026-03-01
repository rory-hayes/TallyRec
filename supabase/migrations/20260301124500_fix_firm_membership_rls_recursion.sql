begin;

create or replace function app.is_firm_member(check_firm_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public, app
as $$
  select exists (
    select 1
    from public.firm_memberships fm
    where fm.firm_id = check_firm_id
      and fm.user_id = app.current_user_id()
  )
$$;

revoke all on function app.is_firm_member(uuid) from public;
grant execute on function app.is_firm_member(uuid) to authenticated;

commit;
