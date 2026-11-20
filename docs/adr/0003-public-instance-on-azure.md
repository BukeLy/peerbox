# The public instance runs on Azure, paid from sponsorship credits

The public instance has to cost the maintainer nothing. It runs on Azure Container Apps with one replica always on, so its Postgres `LISTEN` connection, the webhook deliveries and the hourly deletion keep running. Its database is Azure Database for PostgreSQL (Flexible Server, burstable B1ms). Both live in their own resource group, `peerbox`, in East US, and are paid from sponsorship credits that would otherwise go unused. Attachments live in a Cloudflare R2 bucket: Azure Blob Storage doesn't speak the S3 API that `bucket.py` signs for, and R2 is free with no egress fees.

## Considered Options

- Fly.io with Supabase: free under Fly's $5 waiver. It was the first plan, but Supabase pauses idle projects and caps the session pool at 15 connections, while the credits pay for a database that does neither.
- Vercel Hobby: push delivery would have to be rewritten into `send`, its terms allow only personal or non-commercial use, and going over its quota disables the service until the next month.
- Cloud Run: keeping the CPU always on costs about $44 a month.
- Cloudflare's free plan: would need a TypeScript rewrite.

## Consequences

The Container Apps address goes into every user's client config, so moving to another host later means everyone has to reconnect. When the credits end, the instance has to move or be paid for.
