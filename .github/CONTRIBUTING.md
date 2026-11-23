# Contributing

## Run the tests

The tests need a Postgres database they are allowed to empty:

```sh
cd server
uv sync
DATABASE_URL='postgresql://localhost/peerbox?sslmode=disable' uv run pytest
```

They apply `schema.sql` to that database and truncate its tables. `sslmode=disable` skips a TLS handshake a local database doesn't need, which can otherwise fail a test that cancels a query just before its pool closes. Set `PEERBOX_TEST_ORG` to an organisation your `gh` login belongs to, and one more test checks a real membership. CI runs the same tests on every pull request.

The attachment tests also need a MinIO with a bucket named `peerbox`, and wait up to a minute for it to appear; without one they fail and the rest still pass:

```sh
docker run -d --rm --name peerbox-s3 -p 9000:9000 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  -e MINIO_DEFAULT_BUCKETS=peerbox bitnamilegacy/minio:2025.5.24
```

## Pull requests

- Title: a [Conventional Commits](https://www.conventionalcommits.org/) header, such as `fix: ...` or `docs: ...`. It becomes the commit title on `main`.
- Description: a `## Summary` and a `## Test plan` section. It becomes the commit body.

CI checks both on every pull request.

## Scope

peerbox uses only what the MCP specification offers, so it behaves the same in every client. Changes that depend on one client's plugin system or private API are out of scope; propose those to the MCP specification instead.
