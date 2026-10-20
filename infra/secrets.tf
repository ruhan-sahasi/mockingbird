# The server's secrets, as SSM SecureString parameters under /mockingbird/. ECS reads them when
# the task starts and hands them to the container as environment variables (ecs.tf).
#
# Terraform creates each one with the placeholder "unset" and then never touches the value
# again (ignore_changes), so the real values are set outside Terraform, one at a time:
#
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /mockingbird/ANTHROPIC_API_KEY --value "$ANTHROPIC_API_KEY"
#
# The real values never appear in any .tf file. Terraform's refresh does read every parameter
# back, decrypted, into terraform.tfstate, though, so the state file holds them once they are
# set. That is one reason the state stays local and gitignored.

locals {
  secret_names = toset([
    "ANTHROPIC_API_KEY",
    "VAPI_API_KEY",
    "DATABASE_URL",
    "BRAIN_SECRET",
    "WEBHOOK_SECRET",
    "OWNER_KEY",
    "VISITOR_SALT",
  ])
}

resource "aws_ssm_parameter" "secret" {
  for_each = local.secret_names

  name  = "/${local.name}/${each.key}"
  type  = "SecureString" # encrypted with the AWS-managed aws/ssm key
  value = "unset"

  lifecycle {
    ignore_changes = [value]
  }
}
