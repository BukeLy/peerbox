# Contributing

## Run the tests

The tests need a Postgres database they are allowed to empty:

```sh
cd server
uv sync
DATABASE_URL=postgresql://localhost/peerbox uv run pytest
```

They apply `schema.sql` to that database and truncate its tables. Set `PEERBOX_TEST_ORG` to an organisation your `gh` login belongs to, and one more test checks a real membership. CI runs the same tests on every pull request.

## Pull requests

- Title: a [Conventional Commits](https://www.conventionalcommits.org/) header, such as `fix: ...` or `docs: ...`. It becomes the commit title on `main`.
- Description: a `## Summary` and a `## Test plan` section. It becomes the commit body.

CI checks both on every pull request.

## Scope

peerbox uses only what the MCP specification offers, so it behaves the same in every client. Changes that depend on one client's plugin system or private API are out of scope; propose those to the MCP specification instead.
