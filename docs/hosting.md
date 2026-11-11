# Hosting Vekro on vekro.me (M26, M27)

Everything runs on one Azure VM with Docker Compose: Postgres, the API, the scheduler and the web
app. The files are in `deploy/`. Steps marked **(Cyprian)** create cloud resources, cost money or
change DNS, and are yours to do. The full order is the checklist in section 15.

**The live setup (`vekro-vm`, #291)** uses the VM's own `cloudflared` systemd service (section 4.3):
no `cloudflared` or Caddy container, and the web app and API published on `127.0.0.1` only through
`deploy/compose.vm.yml`. Sections 4.1 and 4.2 are the alternatives the files still support.

```
                      Cloudflare (DNS, TLS, proxy)
browser ──https──> app.vekro.me ─┐                       ┌──> web:8080 (nginx: app + /api proxy) ──> api:8000
Daraja, Pesapal,   api.vekro.me ─┼── tunnel (outbound) ──┤                                            │
Africa's Talking   ssh.vekro.me ─┘   from cloudflared    ├──> api:8000 (provider callbacks)       postgres
                                                         └──> VM sshd (CD and admin, via Access)   scheduler
```

## 1. Decisions

| Topic | Decision |
|---|---|
| Domain | `vekro.me` (Namecheap, paid until Nov 2027). DNS moves to Cloudflare (Free plan). |
| Names | `app.vekro.me` (the app, `/api` proxied), `api.vekro.me` (provider callbacks, direct API checks), `ssh.vekro.me` (tunnel SSH for CD and admin). The apex and `www` stay on GitHub Pages. |
| Server | `vekro-vm` (resource group `vekro`), South Africa North, `Standard_B2als_v2` (2 vCPU, 4 GB) plus 2 GB swap, 32 GB Standard SSD, Ubuntu 24.04, timezone Africa/Nairobi. Auto-shutdown 23:00 EAT. |
| Way in | **Host cloudflared** (section 4.3): tunnel `vekro-vm`, dashboard-managed, a systemd service on the VM. No inbound port is open (the NSG has no inbound rule). The Compose `tunnel` profile and Caddy remain alternatives. |
| Images | Built by GitHub Actions and published to GHCR: `ghcr.io/ckmellow/vekro-api`, `ghcr.io/ckmellow/vekro-web`. The VM never builds. |
| Deploys | By hand over SSH with `deploy/deploy.sh` (section 7.1). CD stays off (`DEPLOY_ENABLED` unset) until Cloudflare Access service tokens exist. Migrations run before the new API starts. |
| Public record | Base Sepolia registry `0xD8094faf2B44711dD372A59C288130FA8113E4DB`; the VM takes over the `vekro_base_sepolia` database that owns it (section 6). |
| Card payments | Pesapal stays a **redirect**: the buyer leaves for Pesapal's page and comes back to `https://app.vekro.me/pay/return`. Nothing is embedded, so the CSP keeps `frame-ancestors 'none'` and needs no Pesapal frame source. |

**One name or two?** One host also works: only `app.vekro.me`, with callbacks at
`https://app.vekro.me/api/payments/mpesa/callback` and so on, because the web app proxies `/api` to
the API. Two names are recommended: callbacks then never depend on the web container, and
Cloudflare rules (rate limits, logs) can treat provider traffic separately. The rest of this document
uses two.

## 2. Domain and DNS: Namecheap to Cloudflare (Cyprian)

Today `vekro.me` uses Namecheap's nameservers (`dns1/dns2.registrar-servers.com`):

| Name | Type | Value | Keep? |
|---|---|---|---|
| `vekro.me` | A ×4 | `185.199.108.153`, `.109.153`, `.110.153`, `.111.153` (GitHub Pages) | yes, DNS only |
| `www` | CNAME | `vekro.me` | yes, DNS only |
| `vekro.me` | MX ×5 | `eforward1…5.registrar-servers.com` (Namecheap e-mail forwarding) | see below |
| `vekro.me` | TXT | `v=spf1 include:spf.efwd.registrar-servers.com ~all` | see below |

DNSSEC is off (no DS record), so nothing has to be undone first.

1. Cloudflare dashboard → **Add a domain** → `vekro.me` → **Free** plan. Cloudflare imports the
   records above. Check that all of them are there. Set the GitHub Pages A records and `www` to
   **DNS only** (grey cloud), so GitHub keeps issuing the Pages certificate.
2. Cloudflare then shows **two nameservers** for this zone, for example `ada.ns.cloudflare.com` and
   `bob.ns.cloudflare.com` (yours will differ).
3. Namecheap → **Domain List** → `vekro.me` → **Manage** → **Nameservers** → change *Namecheap
   BasicDNS* to **Custom DNS** → enter the two Cloudflare nameservers → save (green tick).
4. Wait until Cloudflare e-mails that the zone is **Active** (minutes to 24 hours). Check:
   `curl -s -H 'accept: application/dns-json' 'https://cloudflare-dns.com/dns-query?name=vekro.me&type=NS'`.
5. E-mail: Namecheap's forwarding is configured at Namecheap. Once the nameservers move it may stop,
   even with the MX records copied. If you use `@vekro.me` addresses, switch to Cloudflare **Email
   Routing** (free; it replaces the MX and SPF records), or delete the MX and TXT records if unused.
6. Zone settings for this app:
   - **SSL/TLS → Edge Certificates**: Always Use HTTPS on, minimum TLS 1.2.
   - **Security → Bots**: **Bot Fight Mode off**. It can challenge Safaricom's, Pesapal's and
     Africa's Talking's servers, and on the Free plan it cannot be skipped per path.
   - **Scrape Shield → Email Address Obfuscation off** and **Speed → Rocket Loader off**. Both
     inject scripts that the app's CSP blocks.

The app, API and SSH records are created by the tunnel (section 4.1). For Caddy instead, see 4.2.

## 3. The Azure VM (Cyprian)

Azure portal (Azure for Students subscription) → **Virtual machines → Create**:

| Setting | Value |
|---|---|
| Resource group | `vekro` (new) |
| Name | `vekro-vm` |
| Region | South Africa North |
| Image | Ubuntu Server 24.04 LTS, x64 |
| Size | about 2 GB RAM, for example `Standard_B1ms` (pick what the subscription allows) |
| Authentication | SSH public key only, user `vekro` |
| Inbound ports | **None** (tunnel). For Caddy: HTTP and HTTPS, and SSH only from your own IP. |
| OS disk | Standard SSD, 30 GB, delete with VM: off |
| Public IP | Standard SKU. Keep it with the tunnel too: it gives the VM outbound internet, and with no inbound rule nobody can reach the VM through it. |
| Auto-shutdown | on, for example 23:00 East Africa Time (section 14) |

Then on the VM (with a temporary NSG rule for SSH from your IP until the tunnel's SSH works):

```sh
sudo apt-get update && sudo apt-get -y upgrade && sudo apt-get -y install unattended-upgrades git
# Docker Engine and the Compose plugin from Docker's apt repository (docs.docker.com/engine/install/ubuntu)
sudo usermod -aG docker vekro
# 2 GB RAM: add 2 GB of swap so a migration or image pull never runs out of memory
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
# SSH: keys only
sudo sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config && sudo systemctl reload ssh
```

The whole stack uses about 700 MB to 1 GB at rest (Postgres, API, scheduler, nginx, cloudflared).

## 4. The way in

### 4.1 Cloudflare Tunnel (recommended, `COMPOSE_PROFILES=tunnel`)

`cloudflared` runs as a container and connects **out** to Cloudflare. Requests for the public
hostnames come back through that connection. The VM opens no port, and its IP never appears in DNS.

1. **(Cyprian)** Cloudflare → **Zero Trust** (Free plan; Cloudflare may ask for billing details to
   activate it, which is your call) → **Networks → Tunnels → Create a tunnel** → *Cloudflared* →
   name `vekro` → copy the **token** from the install command into `deploy/.env` as
   `CLOUDFLARE_TUNNEL_TOKEN` (it is a secret).
2. **Public hostnames** of the tunnel (each creates a proxied CNAME to `<id>.cfargotunnel.com`):

   | Hostname | Service |
   |---|---|
   | `app.vekro.me` | `HTTP` → `web:8080` |
   | `api.vekro.me` | `HTTP` → `api:8000` |
   | `ssh.vekro.me` | `SSH` → `host.docker.internal:22` |

3. **SSH through Access** (`ssh.vekro.me`): Zero Trust → **Access → Applications → Add →
   Self-hosted**, domain `ssh.vekro.me`, two policies: *Allow* your e-mail (for you), and *Service
   Auth* with a new **service token** `github-deploy` (for CD; copy its Client ID and Secret, section
   7). On your laptop, install `cloudflared` and add to `~/.ssh/config`:

   ```
   Host ssh.vekro.me
     User vekro
     ProxyCommand cloudflared access ssh --hostname %h
   ```

   Then remove the temporary SSH rule from the VM's network security group.

TLS is Cloudflare's, at the edge. Inside the VM the tunnel speaks plain HTTP to `web` and `api` on
the private Compose network.

### 4.3 Host cloudflared (the live setup on `vekro-vm`)

`cloudflared` is installed from Cloudflare's apt repository and runs as the systemd service
`cloudflared` with the dashboard-managed tunnel `vekro-vm`. Compose runs no tunnel and no Caddy.
`deploy/compose.vm.yml` publishes the two services the tunnel needs on the loopback interface only:

| Container | Published on | Tunnel public hostname (Zero Trust → Networks → Tunnels → `vekro-vm`) |
|---|---|---|
| `web` | `127.0.0.1:8080` | `app.vekro.me` → `http://localhost:8080` |
| `api` | `127.0.0.1:8000` | `api.vekro.me` → `http://localhost:8000` |
| VM sshd | `0.0.0.0:22` (no NSG rule: reachable only through the tunnel) | `ssh.vekro.me` → `ssh://localhost:22` |

In `deploy/.env`: `COMPOSE_PROFILES=` (empty) and `VEKRO_COMPOSE_OVERRIDE=compose.vm.yml`.
`deploy.sh`, `backup.sh` and `restore.sh` read that setting and add the override, so a rollout or a
restore never drops the published ports. Check after any change: `sudo ss -tlnp` lists nothing on
`0.0.0.0` or `[::]` except sshd.

SSH from the laptop (WSL) is `ssh ssh.vekro.me` (`~/.ssh/config` runs `cloudflared access ssh`). It
fails on networks that intercept TLS (Strathmore Wi-Fi: `x509: certificate signed by unknown
authority`); use a phone hotspot. Never turn TLS verification off to get around it.

### 4.2 Caddy (alternative, `COMPOSE_PROFILES=caddy`)

Caddy publishes 80 and 443 and requests the certificates itself. In Cloudflare, add A records `app`
and `api` pointing at the VM's public IP, set to **DNS only** (grey cloud), set `ACME_EMAIL` in
`deploy/.env`, and allow HTTP and HTTPS in the VM's network security group. CD then needs SSH open
(port 22 from GitHub's runner ranges, or the tunnel's SSH only).

## 5. Settings (`deploy/.env`)

Start from `deploy/env.production.example` (every value is explained there). Values that come from
your laptop, never from git:

| Setting | From |
|---|---|
| `ANCHOR_PRIVATE_KEY` | the laptop's backend `.env` (anchorer `0x7fA0…E391`) |
| `ANCHOR_ADMIN_REF_SECRET` | the laptop's `.env.base-sepolia` (the key that database was written with) |
| `CLOUDFLARE_TUNNEL_TOKEN` | section 4.1 |
| `POSTGRES_PASSWORD`, `SECRET_KEY`, `PAYMENT_CALLBACK_SECRET`, `USSD_CALLBACK_TOKEN` | new, `openssl rand -hex 32` |
| Daraja, Pesapal, Africa's Talking keys | the providers' portals, sandbox until each live go-ahead |

`chmod 600 deploy/.env`. Keep a copy in a password manager: it is not in any backup.

## 6. First boot, and taking over the Base Sepolia database

```sh
git clone https://github.com/CKmellow/Vekro-backend.git ~/vekro && cd ~/vekro
cp deploy/env.production.example deploy/.env && chmod 600 deploy/.env && $EDITOR deploy/.env
# the GHCR images are public (no docker login); on vekro-vm, with the loopback override:
alias vk='docker compose -f deploy/compose.yaml -f deploy/compose.vm.yml --env-file deploy/.env'
vk up -d postgres
```

The registry belongs to the `vekro_base_sepolia` database (`docs/anchoring.md` section 8), so the
VM restores that database **before anything else runs**:

```sh
# Laptop: stop anything using it (API, worker, scheduler), then dump it (-U: your local DB user)
docker exec vekro-postgres pg_dump -U <local db user> -Fc vekro_base_sepolia > vekro_base_sepolia.dump
scp vekro_base_sepolia.dump ssh.vekro.me:
# Laptop: from now on the VM is the only writer. In .env.base-sepolia set ANCHOR_ENABLED=false.

# VM
deploy/restore.sh ~/vekro_base_sepolia.dump --replace   # type REPLACE
vk up -d                                                            # migrate, api, scheduler, web, cloudflared
vk ps
vk exec api python scripts/audit_anchoring.py                         # CLEAN against Base Sepolia
```

`migrate` should show `Exited (0)` and the rest `Up` (`api` and `postgres` `healthy`). If the API
refuses to start, it names the broken setting: `vk logs api`.

Checks (after the first boot and after every rollout):

```sh
curl -s https://api.vekro.me/health                       # {"status":"ok"}
curl -s https://app.vekro.me/api/health                   # same, through the web proxy
curl -sI https://app.vekro.me/ | grep -iE 'content-security-policy|strict-transport'   # connect-src has https://sepolia.base.org
vk logs --since 10m scheduler | grep scheduler_job_ran     # jobs are running
vk exec api python -m app.cli payment-rails               # which rails are on, sandbox or live
```

In a browser: log in, open an order, and check that the "Verified record" panel says the record
checks out. The support admin is created with the password read from the environment, never a flag:

```sh
read -rs VEKRO_ADMIN_PASSWORD && export VEKRO_ADMIN_PASSWORD
vk exec -e VEKRO_ADMIN_PASSWORD api python -m app.cli create-admin --phone +2547XXXXXXXX --name "Vekro Support"
unset VEKRO_ADMIN_PASSWORD
```

## 7. Continuous deployment (GitHub Actions)

Each repo has `.github/workflows/deploy.yml`. On every merge to `main`:

1. **image**: build and push `ghcr.io/ckmellow/vekro-api:<sha>` (backend) or
   `ghcr.io/ckmellow/vekro-web:<sha>` (frontend), plus `:latest`. Always runs.
2. **roll-out**: only when the repository variable `DEPLOY_ENABLED` is `true`. It connects over SSH
   (through Cloudflare Access when the service-token secrets are set). The backend first checks out
   the same commit on the VM, so the Compose file and scripts match the image. Both then run
   `deploy/deploy.sh api|web <sha>`, which:
   - pulls the new image;
   - runs `alembic upgrade head` with the **new** image while the old API keeps serving;
   - restarts the services;
   - waits up to 3 minutes for the API health check and `app.vekro.me/api/health` inside the VM;
   - if they fail, puts the previous images back and fails the workflow.

   Rollouts take a lock, so two merges never deploy at once.

Settings in **both** repositories (Settings → Secrets and variables → Actions), and an environment
named `production` (optionally with you as required reviewer, so every rollout waits for a click):

| Kind | Name | Value |
|---|---|---|
| variable | `DEPLOY_ENABLED` | `true` once section 6 works; leave unset until then |
| variable | `DEPLOY_HOST` | `ssh.vekro.me` (tunnel) or the VM's IP (Caddy) |
| variable | `DEPLOY_USER` | `vekro` |
| variable | `DEPLOY_PATH` | `vekro` (the checkout in the user's home) |
| secret | `DEPLOY_SSH_KEY` | a new ed25519 private key used only for deploys (`ssh-keygen -t ed25519 -C github-deploy -f deploy_key`); its `.pub` goes in the VM's `~vekro/.ssh/authorized_keys` |
| secret | `DEPLOY_KNOWN_HOSTS` | the VM's host key line: on the VM, `ssh-keyscan -t ed25519 localhost`, with `localhost` replaced by the `DEPLOY_HOST` value |
| secret | `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | the Access service token (section 4.1); leave unset with Caddy |

**Images on GHCR** are private when first published. Either make both packages public (GitHub →
your profile → Packages → `vekro-api` / `vekro-web` → Package settings → Change visibility; they hold
no secrets, only public build values), or log the VM in once with a token that has only
`read:packages` (`docker login ghcr.io -u CKmellow`).

Rolling back by hand: `deploy/deploy.sh api <older sha>` (or `web`), or `both <api-sha> <web-sha>`.

### 7.1 Manual deploys (the live setup until CD is switched on)

`DEPLOY_ENABLED` stays unset in both repositories until Cloudflare Access service tokens exist, so a
merge only publishes the images. Roll them out from the laptop, after the **Deploy** workflow of the
merged commit is green in each repository:

```sh
api=$(gh run list -R CKmellow/Vekro-backend -w Deploy -b main -s success -L 1 --json headSha -q '.[0].headSha')
web=$(gh run list -R CKmellow/Vekro -w Deploy -b main -s success -L 1 --json headSha -q '.[0].headSha')
ssh ssh.vekro.me "cd ~/vekro && git fetch --quiet origin main && git checkout --quiet --detach $api && deploy/deploy.sh both $api $web"
```

`deploy.sh api <sha>` or `web <sha>` rolls out one side only. The backend checkout on the VM must be
the same commit as the API image, so the Compose file and scripts match it. The script prints
`deploy: done`, or rolls back and exits 1.

## 8. Provider callbacks: from ngrok to the domain

Each provider is told the public URL once. All use `api.vekro.me`. With the tunnel, nothing else is
needed: there is no ngrok on the VM.

| Provider | Where | URL |
|---|---|---|
| Daraja (M-Pesa) | `DARAJA_CALLBACK_URL` in `deploy/.env` (sent with every STK push) | `https://api.vekro.me/payments/mpesa/callback` |
| Pesapal IPN | register once, put the id in `PESAPAL_IPN_ID`, then `vk up -d api scheduler` | `https://api.vekro.me/payments/pesapal/ipn` |
| Pesapal return (redirect) | `FRONTEND_URL` (the buyer comes back to `/pay/return`) | `https://app.vekro.me/pay/return` |
| Africa's Talking USSD | the service code's callback in the AT dashboard | `https://api.vekro.me/ussd/africastalking/<USSD_CALLBACK_TOKEN>` |

Pesapal IPN registration (with `PESAPAL_ENABLED=true` and the keys set; it prints the id). The
hostname must already resolve: add the `api.vekro.me` tunnel route first, or Pesapal refuses with
`invalid_url`. On the VM, write the printed id into `deploy/.env` as `PESAPAL_IPN_ID`:

```sh
vk exec api python -m app.services.custody.pesapal_ipn_registration \
  --callback-url https://api.vekro.me/payments/pesapal/ipn --notification-type GET \
  --env-file /tmp/pesapal.env
```

Daraja callbacks are only triggers: Vekro confirms every payment with its own query
(`docs/daraja-observed.md`), and the scheduler confirms any whose callback never arrives.

`USSD_ALLOWED_IPS` is optional. Behind the tunnel set `USSD_CLIENT_IP_HEADER=cf-connecting-ip`: the
caller is then read from `CF-Connecting-IP` (Cloudflare overwrites it), never from `X-Forwarded-For`,
which a caller can set (#289). Without that setting the allowlist is off and the API logs
`ussd_allowlist_off` at start-up. The path token is what protects the USSD callback either way.

## 9. Upgrades, migrations and rollback

Normally CD does this (section 7). By hand: `git pull && deploy/deploy.sh both <api-sha> <web-sha>`.

Migrations run while the previous API is still serving, so each migration must work with the
previous release too: add columns and tables first, and remove them in a later release. CI checks that
every migration downgrades cleanly. To undo one by hand:

1. Take a backup (section 10).
2. `vk run --rm migrate alembic downgrade <previous revision>`.
3. `deploy/deploy.sh api <previous sha>`.

Never downgrade past a migration that dropped data: restore a backup instead.

## 10. Backups

`deploy/backup.sh` takes a consistent `pg_dump` (custom format, compressed) without downtime,
refuses to keep an empty dump, and deletes dumps older than `KEEP_DAYS` (default 14). Files go to
`deploy/backups/` (git-ignored, mode 700) or `BACKUP_DIR`.

Nightly, with `crontab -e` for `vekro`, keeping 7 days on the VM. `vekro-vm` runs on East Africa Time
and is deallocated at 23:00 EAT, so its backup runs at 22:30 EAT, while it is still up:

```
30 22 * * * cd /home/vekro/vekro && KEEP_DAYS=7 deploy/backup.sh >> /home/vekro/vekro-backup.log 2>&1
```

Copy the newest dump to the laptop (WSL), for example after a demo:

```sh
mkdir -p ~/vekro-backups && chmod 700 ~/vekro-backups
latest=$(ssh ssh.vekro.me 'ls -1t ~/vekro/deploy/backups/vekro-*.dump | head -n 1')
scp "ssh.vekro.me:$latest" ~/vekro-backups/
```

Keep a copy off the VM too (for example `rclone copy deploy/backups remote:vekro-backups` to an
encrypted remote, or Azure Blob Storage). A backup that only lives on the VM is lost with the VM.
While the VM is stopped (section 14), the nightly backup does not run.

What is in a backup: users, orders, the ledger, disputes, notifications, audit rows and the anchor
event chain. The anchored roots on Base Sepolia need no backup, and they are what proves a restored
database was not altered.

## 11. Restore

Monthly drill (proves the last file is good; production is untouched):
`deploy/restore.sh deploy/backups/vekro-<time>.dump`. It restores into a scratch database, prints the
schema revision and row counts, and drops it.

Real restore (stops `api`, `scheduler` and `web`, replaces the database, starts everything again;
asks you to type `REPLACE`): `deploy/restore.sh deploy/backups/vekro-<time>.dump --replace`.

Afterwards run `vk exec api python scripts/audit_anchoring.py` and check `GET
/admin/anchoring/status`. A database restored to an older point than the last anchored batch can no
longer anchor to the same registry: the registry already holds later batches (RB-8). Payments that
arrived after the backup was taken are re-confirmed by the scheduler from the providers only within
the 24-hour reconcile window; check the provider portals for anything older.

## 12. Logs and monitoring

- `vk logs -f api`, `vk logs -f scheduler`, `vk logs -f cloudflared`. Logs rotate at 10 MB, five
  files per service.
- Money events appear in the logs as `money_audit action=...` and in `GET /admin/audit/money-events`.
- Alerts that need a person are anchored money audit events: `reserves_shortfall` (RB-12),
  `mpesa_payment_after_card_switch` (RB-13), card refunds waiting (RB-11). See `docs/runbooks.md`.
- An external uptime check on `https://api.vekro.me/health` and `https://app.vekro.me/api/health`
  (any free monitor). Cloudflare shows tunnel health under Zero Trust → Networks → Tunnels.
- The anchorer's Base Sepolia balance (`docs/anchoring.md` section 8.1).
- Disk: `docker system df`, `df -h`. `deploy.sh` prunes dangling images after each rollout.

## 13. Security notes

- `deploy/.env` holds every secret: mode 600, never committed, backed up separately from the
  database dumps.
- With the tunnel, the VM has no inbound rule at all. SSH works only through Cloudflare Access (you,
  or the GitHub deploy service token). With Caddy, only 80 and 443 are open, and SSH only from your IP.
- The API trusts `X-Forwarded-*` because it is reachable only through cloudflared, Caddy or the web
  proxy on the private network. Never publish port 8000.
- Containers run as a non-root user. The deploy key can log in as `vekro` (the docker group), which is
  effectively root on the VM: keep it only in the two repositories' secrets.
- Rotate `SECRET_KEY` and `PAYMENT_CALLBACK_SECRET` by changing them and restarting (sessions end).
  `ANCHOR_ADMIN_REF_SECRET` accepts several comma-separated keys: add the new one first, and never drop
  one that existing records were made with.

## 14. Costs: stopping the VM between demos

Azure for Students gives a credit (US$100 for 12 months at the time of writing). Check current
prices in the Azure pricing calculator for South Africa North:

| Item | Charged while running | Charged while **deallocated** |
|---|---|---|
| VM compute (`B1ms`-class) | yes, the largest part | **no** |
| OS disk (30 GB Standard SSD) | yes | yes (small) |
| Standard public IP | yes | yes (small) |
| Cloudflare (Free plan, tunnel, Access) | no | no |
| Base Sepolia anchoring | test ETH only (about 0.000001 per batch) | nothing is sent |

Stop it so compute stops billing. Use **Stop** in the portal, which deallocates. Shutting down from
inside the VM does **not** deallocate, and compute keeps billing.

```sh
az vm deallocate -g vekro -n vekro-vm      # between demos
az vm start -g vekro -n vekro-vm           # about 1 to 2 minutes; the stack restarts by itself
```

Auto-shutdown (VM → Operations → Auto-shutdown) deallocates every night at a set time. It does not
start the VM again: start it before a demo. The public IP stays the same while it is deallocated
(Standard static).

**What stops while the VM is off**

- `app.vekro.me` and `api.vekro.me` answer with a Cloudflare error page (the tunnel is down).
- M-Pesa callbacks and Pesapal IPNs are lost. When the VM starts, the scheduler asks Daraja and
  Pesapal for every prompt from the last 24 hours, so payments made in that window are still
  confirmed. Do not take payments while it is off.
- USSD sessions fail, and no SMS goes out.
- Timers keep counting in real time. On start, the scheduler handles everything that fell due
  meanwhile: dispatch timeouts, inspection holds released to sellers, return windows. Avoid
  stopping the VM while orders are at the door or on hold, or expect them to move on at once.
- Anchoring pauses. Events wait in the database and are anchored in the first batches after the
  start. Payouts with `ANCHOR_GATE_PAYOUTS=true` wait for that too.
- The nightly backup and the liabilities and reserves snapshots skip the time it was off.
- CD rollouts fail while it is off (the workflow fails at SSH). Re-run the workflow after starting
  it, or run `deploy/deploy.sh` by hand.

## 15. Go-live checklist (in order)

Each step is yours unless it says otherwise. Steps that cost money or change DNS are marked $ or DNS.

1. [ ] **DNS** Cloudflare: add `vekro.me` (Free), check the imported records, GitHub Pages records
       DNS only (section 2, steps 1 and 2).
2. [ ] **DNS** Namecheap: nameservers → Custom DNS → the two Cloudflare nameservers (step 3). Wait for
       *Active*. Decide on e-mail forwarding (step 5). Zone settings (step 6).
3. [ ] **$** Azure: create the VM as in section 3 (no inbound ports; temporary SSH rule from your IP),
       auto-shutdown on. Install Docker, swap and SSH hardening.
4. [ ] Cloudflare Zero Trust: create the tunnel `vekro` and its three public hostnames; the Access
       application for `ssh.vekro.me` with your e-mail and a `github-deploy` service token
       (section 4.1). SSH in through `ssh.vekro.me`, then delete the temporary SSH rule.
5. [ ] GHCR: make `vekro-api` and `vekro-web` public, or keep them private and `docker login` on the VM
       (section 7). `vekro-web` already exists; `vekro-api` appears after this PR's first merge.
6. [ ] VM: clone, fill in `deploy/.env` (section 5) with `COMPOSE_PROFILES=tunnel`, sandbox provider
       keys, and the anchoring values from your laptop.
7. [ ] Laptop: stop local servers and dump `vekro_base_sepolia`; copy it to the VM; on the laptop set
       `ANCHOR_ENABLED=false` in `.env.base-sepolia` (section 6).
8. [ ] VM: `deploy/restore.sh … --replace`, then `vk up -d`; run the checks and the audit (section 6).
9. [ ] Create the support admin (section 6).
10. [ ] Providers, still sandbox: Daraja callback URL, Pesapal IPN registration, AT USSD callback
        (section 8). Test one sandbox STK push, one sandbox card payment and one USSD session.
11. [ ] GitHub, both repos: the `production` environment, variables and secrets (section 7), then
        `DEPLOY_ENABLED=true`. Re-run the latest **Deploy** workflow in each repo and watch it roll out.
12. [ ] Monitoring: an uptime check on both health URLs; nightly backup cron plus an off-VM copy
        (sections 10 and 12).
13. [ ] Live credentials, each switched on only after its own go-ahead: Daraja production, Pesapal
        live, Africa's Talking live (and a registered sender ID), eConfirm after the KES 100 smoke
        test, and the `ECONFIRM_MAX_AMOUNT` decision.
14. [ ] Support contact values for the web build (`VITE_SUPPORT_*` repository variables in the
        frontend, `docs/DEPLOY.md` there), then re-run its Deploy workflow.
15. [ ] Between demos: `az vm deallocate` (section 14).
