# The public instance runs on Fly.io with Supabase Postgres

The public instance has to cost nothing and stay allowed once sponsorships come in. Fly.io waives a personal organisation's monthly bill under $5, which covers one always-on machine. That lets the server run unchanged, keeping its Postgres `LISTEN` connection and the background tasks that push to MCP Events subscribers. Supabase's free Postgres doesn't suspend while it is in use, and its session pooler supports `LISTEN`. Both run in US East: Fly `iad`, Supabase `us-east-1`.

## Considered Options

- Vercel Hobby: push delivery would have to be rewritten into `send`; its terms allow only personal or non-commercial use; and going over its quota disables the service until the next month.
- Cloud Run: keeping the CPU always on costs about $44 a month.
- Cloudflare's free plan: would need a TypeScript rewrite.
- Oracle's free VM: gets no hostname without a domain, and can be reclaimed when idle.
- Neon's free plan: its compute hours run out under steady traffic.

## Consequences

The `fly.dev` address goes into every user's client config, so moving to another host later means everyone has to reconnect. Supabase pauses a project after a week without activity. `fly launch` writes `auto_stop_machines = "stop"` and `min_machines_running = 0`, which would stop the machine whenever no request comes in, and the `LISTEN` connection, the webhook deliveries and the hourly deletion with it; `fly.toml` must set `"off"` and `1`. Supabase's free session pool holds 15 connections, so the server keeps its pools within that.
