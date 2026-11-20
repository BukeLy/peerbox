-- Runs at every startup as one transaction; the lock makes instances that start
-- together migrate one at a time.
select pg_advisory_xact_lock(hashtext('peerbox schema'));

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

-- The owner is an address: a person (owner_agent null) or one of their agents.
create table if not exists groups (
    id text primary key,
    name text not null,
    owner text not null references users (login),
    owner_agent text,
    created_at timestamptz not null default now()
);

-- A member is a person (agent null) or one of their agents.
create table if not exists group_members (
    group_id text not null references groups (id) on delete cascade,
    login text not null references users (login),
    agent text,
    joined_at timestamptz not null default now(),
    unique nulls not distinct (group_id, login, agent)
);

-- v0.3: a message goes to one address or to a group, and is deleted after 30 days,
-- leaving replies to it in place. messages.read_at is no longer written.
alter table messages
    add column if not exists group_id text references groups (id) on delete cascade,
    alter column recipient drop not null,
    drop constraint if exists messages_reply_to_fkey,
    add constraint messages_reply_to_fkey
        foreign key (reply_to) references messages (id) on delete set null;
drop index if exists messages_unread;
create index if not exists messages_reply_to on messages (reply_to);

-- Each recipient's copy of a message, read or unread on its own.
create table if not exists deliveries (
    -- Numbered from the messages' sequence, so a message's own id is free for its
    -- delivery below and never taken by another.
    id bigint primary key default nextval('messages_id_seq'),
    message_id bigint not null references messages (id) on delete cascade,
    recipient text not null references users (login),
    recipient_agent text,
    read_at timestamptz
);

create index if not exists deliveries_unread
    on deliveries (recipient, recipient_agent, id) where read_at is null;
create index if not exists deliveries_message on deliveries (message_id);

-- Direct messages written before deliveries existed, or by an older version still
-- running, get their delivery, numbered with the message's id so that the event
-- cursors clients saved stay valid.
insert into deliveries (id, message_id, recipient, recipient_agent, read_at)
select id, id, recipient, recipient_agent, read_at from messages m
where group_id is null
    and not exists (select from deliveries where message_id = m.id);

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
