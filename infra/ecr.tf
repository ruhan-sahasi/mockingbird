# The image repository. scripts/deploy.sh pushes :latest and forces a new ECS deployment, so the
# tag has to be mutable.
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"

  # Without this, terraform destroy fails once images have been pushed.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Each push moves :latest and leaves the previous image untagged. Keep a few of those for a
# manual rollback and expire the rest, so storage does not grow with every deploy.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 5 newest untagged images"
      selection = {
        tagStatus   = "untagged"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}
