# The public instance runs on Azure, paid from sponsorship credits

The public instance has to cost the maintainer nothing. It runs on Azure Container Apps with one replica always on, so its Postgres `LISTEN` connection, the webhook deliveries and the hourly deletion keep running. Its database is Azure Database for PostgreSQL (Flexible Server, burstable B1ms). Both live in their own resource group, `peerbox`, in Central US, and are paid from sponsorship credits that would otherwise go unused. Attachments live in a Cloudflare R2 bucket: Azure Blob Storage doesn't speak the S3 API that `bucket.py` signs for, and R2 is free with no egress fees.

Clients reach it at `app.peerbox.net`. The domain's DNS is an Azure DNS zone, `peerbox.net`, in the same resource group, and the registrar's nameservers point at it. A CNAME record `app` points at the container app, and a TXT record `asuid.app` proves to Container Apps that the domain is ours, so it issues and renews the certificate. `server/deploy-azure.sh` lists the commands. A published release deploys itself: after the image is pushed, the release workflow signs in to Azure through GitHub OIDC as the repo's `production` environment and runs that script, so GitHub holds no Azure secret and the repo's Deployments list each release.

## Considered Options

- Fly.io with Supabase: free under Fly's $5 waiver. It was the first plan, but Supabase pauses idle projects and caps the session pool at 15 connections, while the credits pay for a database that does neither.
- Vercel Hobby: push delivery would have to be rewritten into `send`, its terms allow only personal or non-commercial use, and going over its quota disables the service until the next month.
- Cloud Run: keeping the CPU always on costs about $44 a month.
- Cloudflare's free plan: would need a TypeScript rewrite.

## Consequences

Every user's client config holds `app.peerbox.net`, not the Container Apps address, so moving to another host later is a DNS change, not a reconnect. When the credits end, the instance has to move or be paid for.
