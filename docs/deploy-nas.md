# Run Maillard on a NAS

The **Build Maillard Image** GitHub Actions workflow publishes Maillard to GHCR and records the image digest in the run summary. Installation and upgrades are manual.

## Compose installation and upgrades

Follow the [Docker quick start](../README.md#quick-start-docker) for a new installation. Before upgrading an existing installation, check `/api/status` and confirm `roast.active` is false. Keep the roaster idle while replacing the container.

Set the Maillard image in `docker-compose.yml` to the desired published digest, such as `ghcr.io/jtalborough/maillard@sha256:<digest>`. Preserve the existing `.env` and database volumes.

```bash
docker compose pull maillard
docker compose up -d maillard
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8080/api/version
```

Compare the reported commit with the build run. To roll back, repeat the procedure with the previous image digest.

## Optional Tailscale sidecar

The repository's `docker-compose.tailscale.yml` and `tailscale/serve.json` add a userspace Tailscale sidecar. The sidecar forwards ports 80 and 8080 to Maillard over the Compose network. It stores its identity in the `tailscale-state` named volume. Follow your Tailscale enrollment and access policy for the sidecar, then check `/health` from an authorized client to verify forwarding.
