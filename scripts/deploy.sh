#!/usr/bin/env bash
# Ship the current checkout to the stack in infra/: the server image to ECR and ECS, and the web
# app to S3 behind CloudFront.
#
#   scripts/deploy.sh
#
# Needs `terraform apply` done in infra/, Docker with buildx, npm, and the AWS CLI signed in to
# the same account. It prints names and URLs only. The secrets live in SSM and never pass
# through here, and the registry password goes over a pipe, never onto the command line.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

output() {
  terraform -chdir="$root/infra" output -raw "$1"
}

repo_url="$(output ecr_repo_url)"
cluster="$(output ecs_cluster)"
service="$(output ecs_service)"
bucket="$(output web_bucket)"
distribution_id="$(output distribution_id)"
site_url="$(output cloudfront_url)"

# repo_url is <account>.dkr.ecr.<region>.amazonaws.com/mockingbird.
registry="${repo_url%%/*}"
region="$(cut -d. -f4 <<<"$registry")"

echo "==> Building and pushing the server image"
aws ecr get-login-password --region "$region" |
  docker login --username AWS --password-stdin "$registry"
docker buildx build --platform linux/arm64 --tag "$repo_url:latest" --push "$root"

echo "==> Restarting the ECS service on the new image"
aws ecs update-service --region "$region" --cluster "$cluster" --service "$service" \
  --force-new-deployment >/dev/null

echo "==> Building the web app"
if [[ ! -d "$root/web/node_modules" ]]; then
  npm --prefix "$root/web" ci
fi
npm --prefix "$root/web" run build

echo "==> Publishing the web app"
dist="$root/web/dist"
# The hashed files first, so the new page never names one that is not there yet. A file's name
# changes whenever its content does, so a browser may keep it for a year without asking again.
aws s3 sync "$dist/assets" "s3://$bucket/assets" --region "$region" \
  --cache-control "public, max-age=31536000, immutable"
# The page names the hashed files, so a browser must check it on every visit. Without a
# Cache-Control header it would guess a freshness from the file's age and keep an old page,
# which names files that the last step below deletes.
aws s3 cp "$dist/index.html" "s3://$bucket/index.html" --region "$region" \
  --cache-control "no-cache"
invalidation="$(aws cloudfront create-invalidation --distribution-id "$distribution_id" \
  --paths "/" "/index.html" --query Invalidation.Id --output text)"
echo "==> Waiting for CloudFront to drop the old page"
aws cloudfront wait invalidation-completed --distribution-id "$distribution_id" \
  --id "$invalidation"
# Only now does nothing serve a page that names the old hashed files, so they can go.
aws s3 sync "$dist" "s3://$bucket" --delete --region "$region"

echo "==> Done. ECS is replacing the task now, so the API is down for a minute or two."
echo "    Check it with: curl -fsS '$site_url/healthz?db=1'"
