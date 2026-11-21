#!/bin/sh
# Updates the public instance (docs/adr/0003-public-instance-on-azure.md) to a
# release's image, given its tag without the leading v:
#
#   GITHUB_CLIENT_ID=Iv23... server/deploy-azure.sh 0.3.0
#
# The instance answers at https://app.$zone, where zone is the domain below. Run it
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
# The domain, $zone (set below), lives in an Azure DNS zone of that name. At the
# registrar, set the domain's nameservers to the four this prints:
#
#   az network dns zone create --resource-group peerbox --name "$zone"
#   az network dns zone show --resource-group peerbox --name "$zone" \
#     --query nameServers
#
# Point app at the container app, and prove to Container Apps that the domain is
# ours with the TXT record asuid.app:
#
#   az network dns record-set cname set-record --resource-group peerbox \
#     --zone-name "$zone" --record-set-name app --cname "$(az containerapp show \
#     --resource-group peerbox --name peerbox \
#     --query properties.configuration.ingress.fqdn --output tsv)"
#   az network dns record-set txt add-record --resource-group peerbox \
#     --zone-name "$zone" --record-set-name asuid.app --value "$(az containerapp show \
#     --resource-group peerbox --name peerbox \
#     --query properties.customDomainVerificationId --output tsv)"
#
# Once both resolve, serve app.$zone with a certificate Azure issues and renews:
#
#   az containerapp hostname add --resource-group peerbox --name peerbox \
#     --hostname "app.$zone"
#   az containerapp hostname bind --resource-group peerbox --name peerbox \
#     --hostname "app.$zone" --environment peerbox-env-cus --validation-method CNAME
#
# The GitHub App's callback URLs are https://app.$zone/auth/callback and
# https://app.$zone/login/callback.
#
# For attachments, create an R2 bucket whose files are deleted 30 days after
# upload, as docs/PRIVACY.md says, and give the app an R2 API token that can read
# and write it:
#
#   npx wrangler r2 bucket create BUCKET
#   npx wrangler r2 bucket lifecycle add BUCKET delete-after-30-days --expire-days 30
#   az containerapp secret set --resource-group peerbox --name peerbox --secrets \
#     s3-secret-access-key='THE R2 TOKEN SECRET'
#   az containerapp update --resource-group peerbox --name peerbox --set-env-vars \
#     S3_ENDPOINT=https://ACCOUNT_ID.r2.cloudflarestorage.com S3_BUCKET=BUCKET \
#     S3_ACCESS_KEY_ID=KEY_ID S3_SECRET_ACCESS_KEY=secretref:s3-secret-access-key
set -eu

zone=peerbox.net
tag=${1:?usage: deploy-azure.sh TAG, such as 0.3.0}
: "${GITHUB_CLIENT_ID:?set GITHUB_CLIENT_ID to the client ID of the GitHub App}"

az containerapp update --resource-group peerbox --name peerbox \
  --image "ghcr.io/bukely/peerbox:$tag" \
  --set-env-vars \
    "BASE_URL=https://app.$zone" \
    "GITHUB_CLIENT_ID=$GITHUB_CLIENT_ID" \
    DATABASE_URL=secretref:database-url \
    GITHUB_CLIENT_SECRET=secretref:github-client-secret
