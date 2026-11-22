-- A person's account. Everything of theirs hangs on its id, which no one sees;
-- username is how others address them, and name the display name they chose.
create table if not exists users (
    id bigint generated always as identity primary key,
    username text not null,
    name text,
    username_changed_at timestamptz  -- null until they first choose one
);

create unique index if not exists users_username on users (lower(username));

-- The ways a person signs in, such as ('github', their numeric GitHub user id).
-- login is the provider's own name for them as of their last sign-in, shown so no
-- one passes for someone else by taking their name as a username.
create table if not exists identities (
    provider text not null,
    subject text not null,
    user_id bigint not null references users (id) on delete cascade,
    login text not null,
    primary key (provider, subject)
);

-- Every username a person has held, the current one too: one they gave up stays
-- theirs to take back, and nobody else's until their account is deleted.
create table if not exists usernames (
    username text not null,
    user_id bigint not null references users (id) on delete cascade,
    created_at timestamptz not null default now()
);

create unique index if not exists usernames_key on usernames (lower(username));

-- What owner calls person, seen only by owner; one remark names one person.
create table if not exists remarks (
    owner bigint not null references users (id) on delete cascade,
    person bigint not null references users (id) on delete cascade,
    remark text not null,
    primary key (owner, person)
);

create unique index if not exists remarks_remark on remarks (owner, lower(remark));

create table if not exists agents (
    owner bigint not null references users (id),
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
    owner bigint references users (id),
    owner_agent text,
    created_at timestamptz not null default now()
);

-- A member is a person (agent null) or one of their agents.
create table if not exists group_members (
    group_id text not null references groups (id) on delete cascade,
    person bigint not null references users (id),
    agent text,
    joined_at timestamptz not null default now(),
    unique nulls not distinct (group_id, person, agent)
);

-- A message goes to one address (recipient, recipient_agent) or to a group, and is
-- deleted after 30 days, leaving replies to it in place.
create table if not exists messages (
    id bigint generated always as identity primary key,
    sender bigint not null references users (id),
    sender_agent text,
    recipient bigint references users (id),
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
    recipient bigint not null references users (id),
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
    a bigint not null references users (id) on delete cascade,
    b bigint not null references users (id) on delete cascade,
    primary key (a, b),
    check (a < b)
);

create table if not exists blocks (
    blocker bigint not null references users (id) on delete cascade,
    blocked bigint not null references users (id) on delete cascade,
    primary key (blocker, blocked)
);

-- Without GITHUB_ORG only: the organisations that installed peerbox where the person
-- is an active member, replaced on each admission check.
create table if not exists memberships (
    person bigint not null references users (id) on delete cascade,
    org text not null,
    primary key (person, org)
);

-- A pending contact request or group invitation; answering deletes it. Numbered
-- from the messages' sequence like deliveries, so one event position covers both.
create table if not exists requests (
    id bigint primary key default nextval('messages_id_seq'),
    kind text not null check (kind in ('contact_request', 'invitation')),
    asker bigint not null references users (id) on delete cascade,
    asker_agent text,
    target bigint not null references users (id) on delete cascade,
    target_agent text,
    group_id text references groups (id) on delete cascade,
    greeting text,
    created_at timestamptz not null default now(),
    unique nulls not distinct (kind, asker, target, target_agent, group_id)
);

create index if not exists requests_target on requests (target, target_agent);

-- An address as text: the person's username, or username/agent for one of their
-- agents.
create or replace function address(person bigint, agent text)
returns text language sql stable as $$
    select username || coalesce('/' || agent, '') from users where id = person
$$;

-- The person's GitHub login as of their last sign-in.
create or replace function github(person bigint)
returns text language sql stable as $$
    select login from identities where user_id = person and provider = 'github'
$$;

-- A group's address as text: # and its id.
create or replace function group_address(id text)
returns text language sql immutable as $$
    select '#' || id
$$;

-- Whether other is me's contact: always themselves; with everyone (GITHUB_ORG
-- set), every admitted person; otherwise an accepted request or a shared
-- organisation. A block either way overrides all of these.
create or replace function is_contact(me bigint, other bigint, everyone boolean)
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
                where m.person = me and o.person = other
            )
        )
    )
$$;

-- Files sent with messages. The bytes live in the S3 bucket under key.
create table if not exists attachments (
    id bigint generated always as identity primary key,
    owner bigint not null references users (id) on delete cascade,
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
