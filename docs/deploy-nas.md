# Deploy to NAS

Pushes to `main` build the GHCR image and deploy it to the NAS over Tailscale. Configure repository secrets before enabling deployment.

## How it works

`.github/workflows/deploy.yml` serializes runs per branch without cancelling an active run. The existing `docker` job publishes `latest` and `sha-<commit>` tags. The `deploy` job runs only on `main`, after the build succeeds, for pushes and manual runs.

The runner joins the tailnet as `tag:ci`, then uses SSH to run Compose in `/volume1/docker/maillard`. It logs into GHCR using the workflow token, pulls and starts only `maillard`, and logs out. It polls `/health` up to 12 times, five seconds apart, prints `/api/version`, and records the deployed SHA in `deploy.log`. Failed health checks print the last 50 container log lines.

After joining Tailscale, the runner tries to mint a single-use, preauthorized, non-ephemeral key for `tag:maillard`, expiring in one hour. It masks the key and passes it to SSH through `MAILLARD_TS_AUTHKEY`. Key mint failures warn with the ACL fix and continue with an empty key. After the app passes its health check, the NAS script writes the sidecar override and Serve config from embedded heredocs, checked byte-for-byte against the repository by pytest.

If required secrets are missing, deployment skips with a warning listing only their names; the image build still runs.

## Repository secrets

Set these in GitHub **Settings → Secrets and variables → Actions**:

| Secret | Value |
| --- | --- |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth client ID. |
| `TS_OAUTH_SECRET` | OAuth client secret with `auth_keys` write scope, assigned `tag:ci`, and permission to issue keys for `tag:maillard` through the tag ownership below. |
| `NAS_HOST` | Tailnet address `nas` or `100.x.y.z`; do not use a LAN IP. |
| `NAS_PORT` | Optional SSH port; defaults to `22`. |
| `NAS_USER` | NAS SSH user with passwordless sudo for `/usr/local/bin/docker`. |
| `NAS_SSH_KEY` | Private SSH key authorized for that NAS user. |
| `MAILLARD_INGEST_SECRET` | Shared ingest secret used by the application and its ingest client. Use a single-line, dotenv-safe value, such as a long random hex string. |

`GITHUB_TOKEN` is supplied automatically. Ensure the repository token can read the GHCR package. the NAS must already be in the tailnet with SSH enabled and the Compose files installed.

The NAS user's sudoers rule must allow `/usr/local/bin/docker` without a password (for example, `NAS_USER ALL=(root) NOPASSWD: /usr/local/bin/docker`, replacing `NAS_USER`).

Optional repository **variable** `MAILLARD_TS_TAG` overrides `tag:maillard`; update policy ownership and access rules for that tag too. The NAS script writes the tag into `tailscale/ts.env` and passes `--env-file .env --env-file tailscale/ts.env` to Compose. This works with `sudo /usr/local/bin/docker compose` even when sudo strips environment variables. Auth keys are never written into `.env` or `ts.env`.

## Tailscale policy

You must add `"tag:maillard": ["autogroup:admin", "tag:ci"]` to `tagOwners`. This lets the CI OAuth client mint the sidecar's tagged key. Merge these owners and the CI SSH rule into the existing policy. If the tailnet is not already allow-all, also add the member access rule for the dashboard:

```json
{
  "tagOwners": {
    "tag:ci": ["autogroup:admin"],
    "tag:maillard": ["autogroup:admin", "tag:ci"]
  },
  "hosts": {
    "nas": "100.x.y.z"
  },
  "acls": [
    {"action": "accept", "src": ["tag:ci"], "dst": ["nas:22"]},
    {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:maillard:80,8080"]}
  ]
}
```

Use the configured `NAS_PORT` instead of `22` if necessary. This permits ordinary SSH using the NAS key; it does not require Tailscale SSH.

## Dashboard hostname and sidecar

Enable **MagicDNS** in the Tailscale admin console and use tailnet DNS on the client. Keep the hostname `maillard` available in the tailnet. From a connected member device, verify:

```bash
curl -sf http://maillard/health
curl -sf http://maillard:8080/health
```

`docker-compose.tailscale.yml` adds `maillard-ts` to the same Compose project as `docker-compose.yml`. The image is pinned to `tailscale/tailscale:v1.102.5`. It runs in userspace with no published host ports, TUN device, or `NET_ADMIN` capability. `tailscale/serve.json` forwards raw TCP on tailnet ports 80 and 8080 to `maillard:8080` over the Compose network, so short-name HTTP Host headers and WebSockets pass through. The existing NAS host port 8080 continues working.

The sidecar starts when an auth key is provided or a saved sidecar container/volume exists. First enrollment with no key skips gracefully. Identity persists in the `tailscale-state` named volume (normally `maillard_tailscale-state`), and `TS_AUTH_ONCE=true` lets subsequent restarts use saved state. The NAS writes a provided key to `tailscale/run/authkey` with mode 600 and removes it after the startup attempt, including failure. The directory is mounted read-only into the root sidecar; the key is needed only for enrollment. A revoked or expired node may require a new enrollment key.

The public sidecar image is pulled after GHCR logout. The script waits about 60 seconds for `BackendState: Running`, then prints only its hostname, DNSName, and tailnet IPs. Failures print the last 30 sidecar log lines and warn; the already healthy app deployment still succeeds. Check end-to-end forwarding with the curl commands above; Running alone does not prove DNS, ACL, or Serve routing works.

Inspect it on the NAS, after ensuring no roast is active:

```bash
cd /volume1/docker/maillard
sudo /usr/local/bin/docker compose --env-file .env --env-file tailscale/ts.env -f docker-compose.yml -f docker-compose.tailscale.yml logs --tail 30 maillard-ts
sudo /usr/local/bin/docker compose --env-file .env --env-file tailscale/ts.env -f docker-compose.yml -f docker-compose.tailscale.yml exec -T maillard-ts tailscale status
```

To remove only the sidecar, stop automatic deploys first (disable this workflow in Actions), then run:

```bash
cd /volume1/docker/maillard
sudo /usr/local/bin/docker compose --env-file .env --env-file tailscale/ts.env -f docker-compose.yml -f docker-compose.tailscale.yml rm -s -f maillard-ts
# Optional: discard its saved identity; next enrollment creates a new node.
sudo /usr/local/bin/docker volume rm maillard_tailscale-state
rm -f tailscale/run/authkey tailscale/ts.env tailscale/serve.json docker-compose.tailscale.yml
```

Delete the `maillard` device in the Tailscale admin console if discarding its identity. Keep the state volume to preserve the node for later use. Re-enabling this deploy workflow provisions the sidecar again when minting succeeds or saved state exists. Do not use `compose down -v`, which would also remove application/database volumes.

## Environment and roast protection

Compose loads `.env` using `env_file: .env`. Before updating it, the job creates `.env` if absent and backs it up as `.env.bak.YYYYMMDDHHMMSS`, retaining the five newest backups. Temporary files use `umask 077`; the replacement `.env` has mode `600`.

The rewrite removes every `SUPABASE_` assignment (including `export SUPABASE_...`) and replaces these assignments:

- `MAILLARD_SITE_URL=https://roastery.example.com`
- `MAILLARD_AI_AUTO_CONTROL_ENABLED=true`
- `MAILLARD_INGEST_SECRET` from the repository secret

Other lines are preserved. Secrets are passed through SSH action environment inputs and written using builtin `printf`, without printing them.

Before changing files or restarting, the job reads `/api/status`. If `roast.active` is true, it fails with **Roast in progress; not restarting**. Python parses JSON when installed; a sed/grep fallback checks the roast object otherwise. If the request fails because the container is down, deployment continues. No POST or roaster command endpoint is called.

Maillard requires operator supervision. The guard checks one moment in time: do not start a roast while deploying. Real NAS connectivity, sudo access, and restart behavior still require operator validation.

## Re-run and roll back

In GitHub **Actions → Build and Deploy Maillard → Run workflow**, select `main` to build and deploy again (`workflow_dispatch`). Wait until any roast has ended.

For rollback, first ensure no roast is active, then SSH to the NAS. Restore the desired backup and edit `docker-compose.yml` to use a previously published image, such as `ghcr.io/jtalborough/maillard:sha-abcdef0` (the build uses a short commit SHA):

```bash
cd /volume1/docker/maillard
cp .env.bak.YYYYMMDDHHMMSS .env
chmod 600 .env
# Set the maillard image tag in docker-compose.yml to the previous sha-<commit>.
# Log into ghcr.io with a package-read credential if required.
sudo /usr/local/bin/docker compose pull maillard
sudo /usr/local/bin/docker compose up -d maillard
curl -sf http://localhost:8080/health
curl -s http://localhost:8080/api/version
```

Restore the Compose image tag to `latest` when ready to resume normal automated updates. A failed deployment does not automatically roll back the image or `.env`.
