# Hosting Vekro on one server (M26)

Everything Vekro needs runs on one Linux VPS with Docker Compose: Postgres, the API, the scheduler,
the web app and Caddy for HTTPS. The files are in `deploy/`. Nothing in this document has been
provisioned yet. Server, domain and DNS steps are marked **(Cyprian)**.

```
Internet ──443──> Caddy ──> web (nginx: app + /api proxy) ──> api:8000 ──> postgres
                    └──────> api:8000 (API_DOMAIN: provider callbacks)
                                         scheduler ─────────────────────> postgres
```

| Service     | What it runs                                                   | Exposed            |
|-------------|----------------------------------------------------------------|--------------------|
| `postgres`  | Postgres 16, data in the `pgdata` volume                       | no                 |
| `migrate`   | `alembic upgrade head`, once per `up`, before the others start | no                 |
| `api`       | `uvicorn app.main:app` (this repo's `Dockerfile`)              | through Caddy/web  |
| `scheduler` | `python -m app.jobs.scheduler` (every periodic job)            | no                 |
| `web`       | `ghcr.io/ckmellow/vekro-web` (frontend #134, `docs/DEPLOY.md`) | through Caddy      |
| `caddy`     | TLS certificates and reverse proxy for both names              | 80, 443            |

The browser only talks to `https://APP_DOMAIN`; the web image proxies `/api` to the API, so the
session and CSRF cookies stay first-party. `API_DOMAIN` is for provider callbacks (M-Pesa, Pesapal,
Africa's Talking) and direct API checks.

## 1. Server (Cyprian)

- Ubuntu 24.04 LTS, 2 vCPU, 4 GB RAM, 40 GB SSD is enough for the class demo and early use.
  Choose a Nairobi or nearby region if offered (Daraja and Africa's Talking latency).
- A non-root user with sudo and SSH keys; password login off.
- Firewall: allow 22, 80 and 443 only (`ufw allow OpenSSH && ufw allow 80,443/tcp && ufw allow 443/udp && ufw enable`).
  Postgres is never published.
- Docker Engine and the Compose plugin from Docker's apt repository.
- Unattended security upgrades on.

## 2. Domain and DNS (Cyprian)

Point two A records (and AAAA if the server has IPv6) at the server: `app.vekro.co.ke` and
`api.vekro.co.ke`. Caddy requests the certificates itself as soon as both names resolve and ports 80
and 443 are reachable. Nothing else is needed for TLS.

## 3. First boot

```sh
git clone https://github.com/CKmellow/Vekro-backend.git vekro && cd vekro
cp deploy/env.production.example deploy/.env
chmod 600 deploy/.env
$EDITOR deploy/.env          # every value; secrets with: openssl rand -hex 32
docker login ghcr.io         # a token with read:packages, unless the web image is public
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build
docker compose -f deploy/compose.yaml --env-file deploy/.env ps
```

`migrate` should show `Exited (0)`, the rest `Up` (`api` and `postgres` `healthy`). If the API
refuses to start it names the broken setting:
`docker compose -f deploy/compose.yaml --env-file deploy/.env logs api`.

To save typing, every command below assumes `alias vk='docker compose -f deploy/compose.yaml --env-file deploy/.env'`.

## 4. Checks after the first boot and after every upgrade

```sh
curl -s https://api.vekro.co.ke/health                       # {"status":"ok"}
curl -s https://app.vekro.co.ke/api/health                   # same, through the web proxy
curl -sI https://app.vekro.co.ke/ | grep -iE 'content-security-policy|strict-transport'
vk logs --since 10m scheduler | grep scheduler_job_ran       # jobs are running
vk exec api python -m app.cli payment-rails                  # which rails are on, sandbox or live
```

Then in a browser: log in as a seller, confirm `/api/auth/me` answers 200 (cookies work), and that an
order tracker updates.

## 5. Support admin

The password is read from `VEKRO_ADMIN_PASSWORD` (never a flag, so it stays out of shell history;
at least 12 characters):

```sh
read -rs VEKRO_ADMIN_PASSWORD && export VEKRO_ADMIN_PASSWORD
vk exec -e VEKRO_ADMIN_PASSWORD api python -m app.cli create-admin --phone +2547XXXXXXXX --name "Vekro Support"
unset VEKRO_ADMIN_PASSWORD
```

## 6. Provider callbacks

Each provider must be told the public URL once. All use `API_DOMAIN`.

| Provider | Setting / action | URL |
|---|---|---|
| Daraja (M-Pesa) | `DARAJA_CALLBACK_URL` | `https://api.vekro.co.ke/payments/mpesa/callback` |
| Pesapal | register the IPN once, then put the id in `PESAPAL_IPN_ID` and restart `api` and `scheduler` | `https://api.vekro.co.ke/payments/pesapal/ipn` |
| Africa's Talking USSD | the service code's callback in the AT dashboard | `https://api.vekro.co.ke/ussd/africastalking/<USSD_CALLBACK_TOKEN>` |

Pesapal IPN registration (with `PESAPAL_ENABLED=true` and the keys set; it prints the id, which you
copy into `deploy/.env` as `PESAPAL_IPN_ID`, then `vk up -d api scheduler`):

```sh
vk exec api python -m app.services.custody.pesapal_ipn_registration \
  --callback-url https://api.vekro.co.ke/payments/pesapal/ipn --notification-type GET \
  --env-file /tmp/pesapal.env
```

Daraja callbacks are only triggers: Vekro confirms every payment with its own query
(`docs/daraja-observed.md`), and the scheduler confirms any whose callback never arrives.

## 7. Upgrades and rollback

```sh
git pull
vk pull web                          # newest web image
vk up -d --build                     # rebuilds the API; migrate runs first, then the rest restart
```

Every migration downgrades cleanly (CI checks it). To roll back:

1. Take a backup (section 8) first.
2. Check out the previous commit or pin the images: `VEKRO_WEB_IMAGE=ghcr.io/ckmellow/vekro-web:<sha>`
   in `deploy/.env` (and `VEKRO_API_IMAGE` once the API image is published).
3. If the release added a migration, downgrade before starting the old code:
   `vk run --rm migrate alembic downgrade <previous revision>`.
4. `vk up -d --build`.

Never downgrade past a migration that dropped data without restoring a backup instead.

## 8. Backups

`deploy/backup.sh` takes a consistent `pg_dump` (custom format, compressed) from the running
database without downtime, refuses to keep an empty dump, and deletes dumps older than `KEEP_DAYS`
(default 14). Files go to `deploy/backups/` (git-ignored, mode 700) or `BACKUP_DIR`.

Nightly at 02:00 Nairobi time (23:00 UTC), with `crontab -e` for the deploy user:

```
0 23 * * * cd /home/deploy/vekro && deploy/backup.sh >> /var/log/vekro-backup.log 2>&1
```

Keep a copy off the server too (for example `rclone copy deploy/backups remote:vekro-backups`
after the backup, with an encrypted rclone remote). A backup that only lives on the server is lost
with the server.

What is in a backup: users, orders, the ledger, disputes, notifications, audit rows and the anchor
event chain. The anchored roots on the public registry need no backup, and they are what proves a
restored database was not altered.

## 9. Restore

Monthly drill (proves the last file is good; production is untouched):

```sh
deploy/restore.sh deploy/backups/vekro-<time>.dump
```

It restores into a scratch database, prints the schema revision and row counts, and drops it.

Real restore (stops `api`, `scheduler` and `web`, replaces the database, starts everything again;
asks you to type `REPLACE`):

```sh
deploy/restore.sh deploy/backups/vekro-<time>.dump --replace
```

After a real restore, run the full audit to confirm the restored data matches the public record
(`vk exec api python scripts/audit_anchoring.py`, or `--no-chain` while anchoring is off; see
`docs/anchoring.md`) and check `GET /admin/anchoring/status`. Payments that
arrived after the backup was taken are re-confirmed by the scheduler from the providers (M-Pesa,
Pesapal) only while their prompts are within the 24-hour reconcile window; check the provider
portals for anything older.

## 10. Logs and monitoring

- `vk logs -f api`, `vk logs -f scheduler`. Logs rotate at 10 MB, five files per service.
- Money events are in the logs as `money_audit action=...` and in `GET /admin/audit/money-events`.
- Alerts that need a person are anchored money audit events: `reserves_shortfall` (RB-12),
  `mpesa_payment_after_card_switch` (RB-13), card refunds waiting (RB-11). See `docs/runbooks.md`.
- Add an external uptime check on `https://api.vekro.co.ke/health` and
  `https://app.vekro.co.ke/api/health` (any free uptime monitor).
- Disk: `docker system df` and `df -h`; prune old images after upgrades with `docker image prune`.

## 11. Security notes

- `deploy/.env` holds every secret: mode 600, never committed, backed up separately from the
  database dumps (a password manager is fine).
- Only Caddy is reachable from outside. The API trusts `X-Forwarded-*` headers because it is only
  reachable through Caddy or the web proxy on the private network; do not publish port 8000.
- The containers run as a non-root user.
- Rotate `SECRET_KEY` and `PAYMENT_CALLBACK_SECRET` by changing them and restarting (sessions end).
  `ANCHOR_ADMIN_REF_SECRET` accepts several comma-separated keys: add the new one first, never drop
  an old one that existing records were made with.

## 12. Go-live checklist (what Cyprian provides)

- [ ] VPS (section 1) and SSH access
- [ ] DNS for `app.vekro.co.ke` and `api.vekro.co.ke` (section 2)
- [ ] GHCR access for the web image, and frontend PR #277 merged (publishes it)
- [ ] Live credentials, each switched on only after its own go-ahead: Daraja production,
      Pesapal live, Africa's Talking live (and a registered sender ID), eConfirm after the KES 100
      smoke test
- [ ] `ECONFIRM_MAX_AMOUNT` decision
- [x] Anchoring: Base Sepolia registry deployed (#279); restore the `vekro_base_sepolia` database
      onto the server before switching it on (`docs/anchoring.md` section 8)
- [ ] Support contact values for the web build (`VITE_SUPPORT_*`, `docs/DEPLOY.md` in the frontend)
- [ ] Off-server backup destination
