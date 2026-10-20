# scripts/deploy.sh reads these with `terraform output -raw`.

output "cloudfront_url" {
  description = "The public HTTPS address of the site and API."
  value       = "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "ecr_repo_url" {
  description = "Where deploy.sh pushes the server image."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.app.name
}

output "web_bucket" {
  description = "Where deploy.sh syncs web/dist."
  value       = aws_s3_bucket.web.id
}

output "distribution_id" {
  description = "The CloudFront distribution deploy.sh invalidates."
  value       = aws_cloudfront_distribution.main.id
}
