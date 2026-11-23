#!/bin/sh
# Updates the public instance (docs/adr/0003-public-instance-on-azure.md) to a
# release's image, given its tag without the leading v:
#
#   server/deploy-azure.sh 0.3.0
#
# A published release runs it from .github/workflows/image.yml. The instance
# answers at https://app.$zone, where zone is the domain below. Run it with the
# Azure CLI signed in to the subscription that holds the resource group peerbox.
# Environment variables set earlier and not named here, such as the S3_ ones,
# stay as they are.
#
# One-time setup:
#
#   az group create --name peerbox --location centralus
#   az postgres flexible-server create --resource-group peerbox --name peerbox-db \
#     --location centralus --tier Burstable --sku-name Standard_B1ms --version 17 \
#     --storage-size 32 --admin-user peerbox --admin-password "$(openssl rand -hex 24)" \
#     --public-access 0.0.0.0  # only from Azure, the container app among it
#   az postgres flexible-server db create --resource-group peerbox \
#     --server-name peerbox-db --name peerbox
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
#
# The site at https://$zone is this repo's GitHub Pages (docs/CNAME names it):
#
#   for ip in 185.199.108.153 185.199.109.153 185.199.110.153 185.199.111.153; do
#     az network dns record-set a add-record --resource-group peerbox \
#       --zone-name "$zone" --record-set-name @ --ipv4-address "$ip"; done
#   for ip in 2606:50c0:8000::153 2606:50c0:8001::153 2606:50c0:8002::153 \
#     2606:50c0:8003::153; do az network dns record-set aaaa add-record \
#       --resource-group peerbox --zone-name "$zone" --record-set-name @ \
#       --ipv6-address "$ip"; done
#   az network dns record-set cname set-record --resource-group peerbox \
#     --zone-name "$zone" --record-set-name www --cname bukely.github.io
#
# Verify the domain for the account too (GitHub settings → Pages → Add a domain)
# and add the TXT record it names, so no other account can take $zone if the
# site's binding ever lapses: agents follow https://$zone/SKILL.md.
#
# The release's deploy job signs in to Azure through GitHub OIDC as this repo's
# production environment, as an identity that may change only the container app.
# No Azure secret is stored in GitHub; the environment's variables hold its IDs.
# GitHub's token names the owner and the repo with their IDs too
# (gh api users/BukeLy -q .id; gh api repos/BukeLy/peerbox -q .id), so a repo
# recreated under the same name can't pass for this one:
#
#   app=$(az ad app create --display-name peerbox-deploy --query appId --output tsv)
#   az ad sp create --id "$app"
#   az ad app federated-credential create --id "$app" --parameters '{"name":
#     "github-production", "issuer": "https://token.actions.githubusercontent.com",
#     "subject": "repo:BukeLy@19304666/peerbox@1463640876:environment:production",
#     "audiences": ["api://AzureADTokenExchange"]}'
#   az role assignment create --assignee "$app" --role Contributor --scope \
#     "$(az containerapp show --resource-group peerbox --name peerbox --query id \
#     --output tsv)"
#   gh variable set AZURE_CLIENT_ID --env production --body "$app"
#   gh variable set AZURE_TENANT_ID --env production \
#     --body "$(az account show --query tenantId --output tsv)"
#   gh variable set AZURE_SUBSCRIPTION_ID --env production \
#     --body "$(az account show --query id --output tsv)"
set -eu

zone=peerbox.net
client_id=Iv23lizyV03bCCUqjTc9 # the peerbox-net GitHub App's; a client ID is public
tag=${1:?usage: deploy-azure.sh TAG, such as 0.3.0}

az containerapp update --resource-group peerbox --name peerbox \
  --image "ghcr.io/bukely/peerbox:$tag" \
  --set-env-vars \
    "BASE_URL=https://app.$zone" \
    "GITHUB_CLIENT_ID=$client_id" \
    DATABASE_URL=secretref:database-url \
    GITHUB_CLIENT_SECRET=secretref:github-client-secret
