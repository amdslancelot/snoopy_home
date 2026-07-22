# Plan — Postgres role isolation for a shared multi-app cluster

Scope: three apps — **snoopy** (this repo), **transigen**, **gelp** — sharing one
Postgres per environment, in both **staging** and **prod**. Goal: each app can
reach *only its own* database, with least-privilege credentials, on shared
infrastructure.

Status: PLAN. Nothing here is applied yet. Snoopy's staging DB currently runs
as the `postgres` superuser (see "Snoopy staging migration" below).

## Decisions (locked with the user)

- **Naming**: uniform `<app>_rw` login roles. Databases keep their app names.
- **Shared Postgres placement**: a neutral **`data`** namespace, reached in-cluster
  at `postgres.data.svc:5432` — not inside any one app's namespace.
- **Prod topology**: **k3s on the OCI VM** hosts all app services *and* the shared
  Postgres. This **supersedes** the Podman/Quadlet prod-Postgres path in
  `prod-provisioning.md` §1 and `deploy/setup-vm.sh`.
- **Staging topology**: minikube on the Mac, same shape (Postgres in `data`).
- **Two separate clusters** (prod-isolation rule: prod never shares data with
  staging) ⇒ identical DB/role **names in each cluster**, no `_staging` suffix;
  the cluster you're in *is* the environment.

## Industry-standard principles (the "how to design this" answer)

When several apps share one Postgres server, isolation is done at the **database
and role** layer, never by network/namespace alone (any pod that resolves the
Service DNS can attempt to connect):

1. **Database-per-app.** One database per app (or bounded context). No shared
   tables across apps; Postgres has no cross-database foreign keys anyway, which
   reinforces the boundary. Each app runs its own migrations against its own DB.
2. **One least-privilege login role per app.** `LOGIN` only — **not** superuser,
   and no `CREATEDB` / `CREATEROLE` / `REPLICATION` / `BYPASSRLS`.
3. **Confine each role to its own database.** By default `PUBLIC` has `CONNECT`
   on every database, so role A *can* connect to app B's DB. Fix per DB:
   `REVOKE CONNECT ON DATABASE <db> FROM PUBLIC;` then grant `CONNECT` only to the
   owning role. This is the actual isolation boundary.
4. **The `postgres` superuser is admin-only.** Used once to provision roles and
   databases; never an application's runtime credential.
5. **Distinct credentials per app per env**, each in that app's own secret store,
   rotatable independently. Six passwords total here (3 apps × 2 envs).
6. **Defense in depth** (not blocking): a `NetworkPolicy` in `data` that admits
   `:5432` only from app namespaces; a connection pooler (PgBouncer) once the app
   or replica count risks the default `max_connections = 100`.
7. **Optional two-tier roles** for sensitive apps later: an `<app>_owner` role that
   owns the schema and runs migrations (DDL), plus an `<app>_rw` runtime role with
   table-level DML only (via `ALTER DEFAULT PRIVILEGES`). Shrinks the blast radius
   if the runtime credential leaks. Baseline below uses a single owner-role per app
   for simplicity.

One caveat to name explicitly: a shared server is **logical**, not physical,
isolation — one failure domain, and a runaway query in one DB can starve CPU/IO
or exhaust connections for the others. That's the accepted trade-off at this
scale; PgBouncer + `NetworkPolicy` + per-DB roles are the standard mitigations.

## Target matrix (identical in each cluster)

| App | Namespace (staging / prod) | Database | Role | In-cluster DATABASE_URL |
|---|---|---|---|---|
| snoopy | `snoopy-staging` / `snoopy` | `snoopy_home` | `snoopy_rw` | `postgresql://snoopy_rw:<pw>@postgres.data.svc:5432/snoopy_home` |
| transigen | `transigen-staging` / `transigen` | `transigen` | `transigen_rw` | `postgresql://transigen_rw:<pw>@postgres.data.svc:5432/transigen` |
| gelp | `gelp-staging` / `gelp` | `gelp` | `gelp_rw` | `postgresql://gelp_rw:<pw>@postgres.data.svc:5432/gelp` |

Shared Postgres itself: namespace `data`, Service `postgres` (so `postgres.data.svc`),
superuser `postgres` with a bootstrap-only password in `data`'s `postgres-secret`.

## Provisioning SQL (once per cluster, as the `postgres` superuser)

Per-app template:

```sql
CREATE ROLE <role> LOGIN PASSWORD '<distinct-strong-pw>';
CREATE DATABASE <db> OWNER <role>;              -- owner ⇒ migrations run cleanly (PG15 public-schema gotcha avoided)
REVOKE CONNECT ON DATABASE <db> FROM PUBLIC;    -- no other role may connect
GRANT  CONNECT ON DATABASE <db> TO <role>;
```

Then, connected to each new DB, harden the `public` schema:

```sql
\c <db>
ALTER SCHEMA public OWNER TO <role>;
REVOKE ALL ON SCHEMA public FROM PUBLIC;        -- owner keeps full rights
```

Concretely for the three (transigen, gelp are fresh; snoopy is a migration — next
section):

```sql
-- transigen
CREATE ROLE transigen_rw LOGIN PASSWORD '<pw>';
CREATE DATABASE transigen OWNER transigen_rw;
REVOKE CONNECT ON DATABASE transigen FROM PUBLIC;
GRANT  CONNECT ON DATABASE transigen TO transigen_rw;
-- gelp
CREATE ROLE gelp_rw LOGIN PASSWORD '<pw>';
CREATE DATABASE gelp OWNER gelp_rw;
REVOKE CONNECT ON DATABASE gelp FROM PUBLIC;
GRANT  CONNECT ON DATABASE gelp TO gelp_rw;
```

## Snoopy staging migration (DB already exists, owned by `postgres`)

Snoopy's staging `snoopy_home` exists and is owned by the superuser `postgres`;
the bot currently connects as `postgres`. Isolate in place, preserving data:

```sql
CREATE ROLE snoopy_rw LOGIN PASSWORD '<pw>';
ALTER DATABASE snoopy_home OWNER TO snoopy_rw;
\c snoopy_home
REASSIGN OWNED BY postgres TO snoopy_rw;        -- moves existing tables/sequences to the app role
ALTER SCHEMA public OWNER TO snoopy_rw;
REVOKE CONNECT ON DATABASE snoopy_home FROM PUBLIC;
GRANT  CONNECT ON DATABASE snoopy_home TO snoopy_rw;
```

Then flip the bot's `DATABASE_URL` from `postgres:…` to `snoopy_rw:…` (dev `.env`
and the staging Secret) and restart.

This also finalizes the earlier `snoopy_chores` → `snoopy_rw` rename: `snoopy_chores`
was only ever written into docs/scripts (never created in staging), so under the
unified convention the live role is `snoopy_rw` and those docs must be updated —
see Impact.

## Secrets & config

- Each app namespace holds its own `Secret` (e.g. `<app>-secrets`) whose
  `DATABASE_URL` uses that app's role+password against `postgres.data.svc:5432/<db>`.
- Generate 6 distinct strong passwords (3 apps × 2 clusters). Store only in
  Secrets (staging) or an env-file → Secret on the node (prod). Never in git.
- The `postgres` superuser password lives only in `data`'s `postgres-secret`,
  used for provisioning/admin, never by an app.

## Rollout order

Staging first (minikube), prove isolation, then prod (k3s).

**Staging (minikube):**
1. Move the shared Postgres into a `data` namespace: `pg_dump snoopy_home` →
   create `data` ns + Postgres → restore → repoint snoopy at `postgres.data.svc` →
   delete the old `snoopy-staging` Postgres. (Same backup/restore dance already
   used for the `chores-staging` → `snoopy-staging` move.)
2. Run the provisioning SQL for `transigen_rw`/`gelp_rw` and their DBs; migrate
   snoopy via the section above.
3. Update each app's Secret/.env, restart, and run the verification below.

**Prod (k3s on the OCI VM):**
4. Stand up k3s + a `data`-namespace Postgres StatefulSet. This **revives**
   `deploy/PLAN-DEPLOY-K3S.md`, extended from one app to three plus the shared
   Postgres in `data`.
5. Run the provisioning SQL; create per-app namespaces and Secrets.
6. Deploy the three apps; run the same verification.

## Verification — prove the isolation actually holds

For each role, confirm it reaches its own DB and is *refused* on another app's:

```bash
# MUST FAIL — no CONNECT privilege:
psql "postgresql://snoopy_rw:<pw>@<host>:5432/transigen" -c '\q'
# MUST SUCCEED:
psql "postgresql://snoopy_rw:<pw>@<host>:5432/snoopy_home" -c 'select 1'
```

Also confirm no app role has superuser: `\du` shows only `LOGIN` (no Superuser /
Create DB / Create role attributes) for `*_rw`.

## Impact on the existing repo (execution follow-ups — not done in this plan)

- **`prod-provisioning.md` §1** (Podman/Quadlet `snoopy-pg`): superseded for prod
  by the k3s `data` Postgres. Retain only as history, or delete.
- **`deploy/setup-vm.sh`**: its Podman prod path (incl. `CREATE ROLE snoopy_rw`)
  is superseded; prod provisioning becomes k3s manifests + the SQL above.
- **`snoopy_chores` → `snoopy_rw`** — ✅ DONE (2026-07-22) across
  `deploy/env.snoopy.example`, `deploy/setup-vm.sh`, `docs/prod-provisioning.md`,
  `docs/storage.md`, `deploy/PLAN-DEPLOY-K3S.md`, `docs/UPGRADE-PLAN.md`
  (`snoopy_chores_staging` → `snoopy_rw_staging` in the k3s plan).
- **`deploy/PLAN-DEPLOY-K3S.md`**: revived and extended — from one `snoopy` app to
  three, add `data`-namespace Postgres provisioning and the per-app roles above.
- **`deploy/k8s/postgres.yaml`**: ✅ DONE — retargeted from `snoopy-staging` to the
  `data` namespace (still a Deployment; consider a StatefulSet for prod parity).

## Open items

- Are **transigen** and **gelp** k8s-deployable the same way (container image,
  migrations-at-startup, a `DATABASE_URL` env)? Their wiring above assumes it.
- Do transigen/gelp have **staging on the same minikube** as snoopy? (Assumed yes.)
- **OCI VM sizing**: the A1.Flex node budget in `PLAN-DEPLOY-K3S.md` assumed roughly
  snoopy alone; re-check CPU/RAM for k3s + 3 apps + Postgres.
- Decide whether any app needs the **two-tier owner/runtime** role split (§7) rather
  than the single owner-role baseline.
