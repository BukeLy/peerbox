#!/bin/sh
# Updates the public instance (docs/adr/0003-public-instance-on-azure.md) to a
# release's image, given its tag without the leading v:
#
#   GITHUB_CLIENT_ID=Iv23... server/deploy-azure.sh 0.5.0
#
# BASE_URL defaults to the instance's address, https://app.peerbox.chat. Run it
# with the Azure CLI signed in to the subscription that holds the resource group
# peerbox. Environment variables set earlier and not named here, such as the S3_
# ones, stay as they are.
#
# One-time setup:
#
#   az group create --name peerbox --location centralus
#   az postgres flexible-server create --resource-group peerbox --name peerbox-db \
#     --location centralus --tier Burstable --sku-name Standard_B1ms --version 17 \
#     --storage-size 32 --admin-user peerbox --admin-password "$(openssl rand -hex 24)" \
#     --public-access 0.0.0.0  # only from Azure, the container app among it
#   az postgres flexible-server db create --resource-group peerbox \
#     --server-name peerbox-db --database-name peerbox
#   az containerapp env create --resource-group peerbox --name peerbox-env-cus \
#     --location centralus
#   az containerapp create --resource-group peerbox --name peerbox \
#     --environment peerbox-env-cus --image ghcr.io/bukely/peerbox:TAG \
#     --ingress external --target-port 8000 --min-replicas 1 --max-replicas 1
#   az containerapp secret set --resource-group peerbox --name peerbox --secrets \
#     database-url='postgresql://peerbox:PASSWORD@peerbox-db.postgres.database.azure.com/peerbox?sslmode=require' \
#     github-client-secret='THE GITHUB APP CLIENT SECRET'
#
# Serve it at app.peerbox.chat with a certificate Azure manages. In the DNS of
# peerbox.chat, add a CNAME record `app` pointing to the app's own address, and a
# TXT record `asuid.app` holding its domain verification id:
#
#   az containerapp show --resource-group peerbox --name peerbox --query \
#     '[properties.configuration.ingress.fqdn, properties.customDomainVerificationId]'
#
# Once both records resolve:
#
#   az containerapp hostname add --resource-group peerbox --name peerbox \
#     --hostname app.peerbox.chat
#   az containerapp hostname bind --resource-group peerbox --name peerbox \
#     --hostname app.peerbox.chat --environment peerbox-env-cus --validation-method CNAME
#
# The GitHub App's callback URLs are BASE_URL/auth/callback and
# BASE_URL/login/callback. For attachments, add the R2 bucket once:
#
#   az containerapp secret set --resource-group peerbox --name peerbox --secrets \
#     s3-secret-access-key='THE R2 KEY SECRET'
#   az containerapp update --resource-group peerbox --name peerbox --set-env-vars \
#     S3_ENDPOINT=https://ACCOUNT_ID.r2.cloudflarestorage.com S3_BUCKET=BUCKET \
#     S3_ACCESS_KEY_ID=KEY_ID S3_SECRET_ACCESS_KEY=secretref:s3-secret-access-key
set -eu

tag=${1:?usage: deploy-azure.sh TAG, such as 0.5.0}
: "${GITHUB_CLIENT_ID:?set GITHUB_CLIENT_ID to the client ID of the GitHub App}"
base_url=${BASE_URL:-https://app.peerbox.chat}

az containerapp ingress update --resource-group peerbox --name peerbox \
  --target-port 8000
az containerapp update --resource-group peerbox --name peerbox \
  --image "ghcr.io/bukely/peerbox:$tag" \
  --min-replicas 1 --max-replicas 1 \
  --set-env-vars \
    "BASE_URL=$base_url" \
    "GITHUB_CLIENT_ID=$GITHUB_CLIENT_ID" \
    DATABASE_URL=secretref:database-url \
    GITHUB_CLIENT_SECRET=secretref:github-client-secret
