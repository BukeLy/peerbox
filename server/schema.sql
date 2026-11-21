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

-- The owner is an address: a person (owner_agent null) or one of their agents, and
-- none once nobody is left in the group.
create table if not exists groups (
    id text primary key,
    name text not null,
    owner text references users (login),
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

-- A message goes to one address (recipient, recipient_agent) or to a group, and is
-- deleted after 30 days, leaving replies to it in place.
create table if not exists messages (
    id bigint generated always as identity primary key,
    sender text not null references users (login),
    sender_agent text,
    recipient text references users (login),
    recipient_agent text,
    group_id text references groups (id) on delete cascade,
    body text not null,
    reply_to bigint references messages (id) on delete set null,
    created_at timestamptz not null default now()
);

create index if not exists messages_reply_to on messages (reply_to);

-- Each recipient's copy of a message, read or unread on its own.
create table if not exists deliveries (
    -- Numbered from the messages' sequence, like requests, so one event position
    -- orders both.
    id bigint primary key default nextval('messages_id_seq'),
    message_id bigint not null references messages (id) on delete cascade,
    recipient text not null references users (login),
    recipient_agent text,
    read_at timestamptz
);

create index if not exists deliveries_unread
    on deliveries (recipient, recipient_agent, id) where read_at is null;
create index if not exists deliveries_message on deliveries (message_id);
-- The panel finds everything one person sent or was sent.
create index if not exists deliveries_recipient on deliveries (recipient, message_id);
create index if not exists messages_sender on messages (sender);

-- A pair of contacts is stored once, a < b.
create table if not exists contacts (
    a text not null references users (login) on delete cascade,
    b text not null references users (login) on delete cascade,
    primary key (a, b),
    check (a < b)
);

create table if not exists blocks (
    blocker text not null references users (login) on delete cascade,
    blocked text not null references users (login) on delete cascade,
    primary key (blocker, blocked)
);

-- Without GITHUB_ORG only: the organisations that installed peerbox where the person
-- is an active member, replaced on each admission check.
create table if not exists memberships (
    login text not null references users (login) on delete cascade,
    org text not null,
    primary key (login, org)
);

-- A pending contact request or group invitation; answering deletes it. Numbered
-- from the messages' sequence like deliveries, so one event position covers both.
create table if not exists requests (
    id bigint primary key default nextval('messages_id_seq'),
    kind text not null check (kind in ('contact_request', 'invitation')),
    asker text not null references users (login) on delete cascade,
    asker_agent text,
    target_login text not null references users (login) on delete cascade,
    target_agent text,
    group_id text references groups (id) on delete cascade,
    greeting text,
    created_at timestamptz not null default now(),
    unique nulls not distinct (kind, asker, target_login, target_agent, group_id)
);

create index if not exists requests_target on requests (target_login, target_agent);

-- An address as text: login, or login/agent for one of their agents.
create or replace function address(login text, agent text)
returns text language sql immutable as $$
    select login || coalesce('/' || agent, '')
$$;

-- A group's address as text: # and its id.
create or replace function group_address(id text)
returns text language sql immutable as $$
    select '#' || id
$$;

-- Whether other is me's contact: always themselves; with everyone (GITHUB_ORG
-- set), every admitted person; otherwise an accepted request or a shared
-- organisation. A block either way overrides all of these.
create or replace function is_contact(me text, other text, everyone boolean)
returns boolean language sql stable as $$
    select me = other or (
        not exists (
            select from blocks where (blocker, blocked) in ((me, other), (other, me))
        )
        and (
            everyone
            or exists (
                select from contacts
                where a = least(me, other) and b = greatest(me, other)
            )
            or exists (
                select from memberships m join memberships o using (org)
                where m.login = me and o.login = other
            )
        )
    )
$$;

-- Files sent with messages. The bytes live in the S3 bucket under key.
create table if not exists attachments (
    id bigint generated always as identity primary key,
    owner text not null references users (login) on delete cascade,
    owner_agent text,
    message_id bigint references messages (id) on delete cascade,  -- null until sent
    name text not null,
    mime text not null,
    size bigint,  -- null for an upload until send checks it
    key text not null,
    created_at timestamptz not null default now()
);

create index if not exists attachments_message on attachments (message_id);

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
