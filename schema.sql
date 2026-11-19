create table if not exists users (
    login text primary key,
    name text,
    aliases text[] not null default '{}'
);

create table if not exists agents (
    owner text not null references users (login),
    name text not null,
    description text not null default '',
    last_seen timestamptz,
    primary key (owner, name)
);

create table if not exists messages (
    id bigint generated always as identity primary key,
    sender text not null references users (login),
    sender_agent text,
    recipient text not null references users (login),
    recipient_agent text,
    body text not null,
    reply_to bigint references messages (id),
    created_at timestamptz not null default now(),
    read_at timestamptz
);

create index if not exists messages_unread
    on messages (recipient, recipient_agent, id) where read_at is null;

-- FastMCP OAuth proxy state (key_value PostgreSQLStore layout), encrypted values.
create table if not exists oauth_state (
    collection text not null,
    key text not null,
    value jsonb not null,
    ttl double precision,
    created_at timestamptz,
    expires_at timestamptz,
    primary key (collection, key)
);

create index if not exists idx_oauth_state_expires_at
    on oauth_state (expires_at) where expires_at is not null;

-- MCP Events webhook subscriptions, so deliveries survive restarts.
-- state is the Fernet-encrypted subscription, signing secrets included.
create table if not exists event_subscriptions (
    id text primary key,
    expires_at timestamptz not null,
    state bytea not null
);
