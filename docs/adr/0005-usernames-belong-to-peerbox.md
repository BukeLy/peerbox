# A person is a peerbox account with its own username, not a GitHub login

Every person has an internal account id that all their data hangs on. Each way of signing in, such as a GitHub account (identified by GitHub's numeric user id, not the login), is linked to that account. A person's username is peerbox's own. It starts as their GitHub login, and `-2`, `-3` and so on are added when that is taken. After that GitHub renames never touch it. The person may change it once a year. A username they give up no longer reaches them, but it stays theirs to take back, and nobody else can have it until they delete their account. Every username ever held lives in one table whose key enforces this, so two people cannot claim the same name at once.

Keying people on the GitHub login was unsafe. After someone renamed on GitHub, their next sign-in created an empty account. Whoever then registered the freed login would sign in to the old one, with its contacts, groups and messages. Tying identity to GitHub would also have blocked any other way of signing in.

## Considered Options

- GitHub's numeric user id as the key: it fixes renames but still binds every account to GitHub.
- Releasing a username as soon as its owner gives it up: anyone holding the old name would then reach whoever took it next. Keeping old names reserved for their owner follows a design proposed on V2EX.
- A UUID for the account id: instances never exchange data (ADR 0001), so the id only has to be unique within one database. It is a bigint, like every other id.

## Consequences

Because anyone may take a free username, even someone else's GitHub login, `whoami`, `contacts` (for contacts, group members and anyone you gave a remark), the contact requests and invitations `inbox` lists, `add_contact` and the panel show each person's GitHub login as of their last sign-in, which only GitHub can vouch for. Usernames are unique only within an instance. Admission (`GITHUB_ORG`, organisation members, guests) stays GitHub-specific until a second sign-in provider arrives, and linking a second provider to an existing account waits for that provider too. The change re-keyed every table. The public instance started again from an empty database rather than migrating, because nobody's data there needed keeping.
