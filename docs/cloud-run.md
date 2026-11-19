# peerbox on Google Cloud Run

What SKILL.md's steps need when the host is Cloud Run. Everything else in SKILL.md applies unchanged.

## Step 1: BASE_URL

```
https://peerbox-PROJECT_NUMBER.REGION.run.app
```

Get the number with `gcloud projects describe PROJECT --format='value(projectNumber)'`.

## Step 3: Postgres on Cloud SQL

Cloud Run reaches Cloud SQL through its socket, so the URL has no host:

```
postgresql://USER:PASSWORD@/DATABASE?host=/cloudsql/PROJECT:REGION:INSTANCE
```

## Step 4: deploy

1. Cloud Run pulls only from Google's registries: it cannot reach ghcr.io, and its Docker Hub cache does not hold this image. Proxy Docker Hub through an Artifact Registry remote repository, once per project:

   ```sh
   gcloud artifacts repositories create peerbox --location REGION \
     --repository-format docker --mode remote-repository --remote-docker-repo docker-hub
   ```

2. Store `DATABASE_URL` and `GITHUB_CLIENT_SECRET` in Secret Manager as `peerbox-database-url` and `peerbox-github-secret`.
3. Deploy. Leave out `--add-cloudsql-instances` when the database is not on Cloud SQL:

   ```sh
   gcloud run deploy peerbox --image REGION-docker.pkg.dev/PROJECT/peerbox/bukely/peerbox:VERSION --region REGION \
     --allow-unauthenticated --min-instances 1 --max-instances 1 --no-cpu-throttling \
     --add-cloudsql-instances PROJECT:REGION:INSTANCE \
     --set-env-vars BASE_URL=BASE_URL,GITHUB_ORG=ORG,GITHUB_CLIENT_ID=CLIENT_ID \
     --set-secrets DATABASE_URL=peerbox-database-url:latest,GITHUB_CLIENT_SECRET=peerbox-github-secret:latest
   ```

`--min-instances 1 --max-instances 1 --no-cpu-throttling` is what SKILL.md step 1 requires: one instance with CPU always on.
