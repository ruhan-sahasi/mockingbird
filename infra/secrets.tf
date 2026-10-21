# The server's secrets, as SSM SecureString parameters under /mockingbird/. ECS reads them when
# the task starts and hands them to the container as environment variables (ecs.tf).
#
# Terraform creates each one with the placeholder "unset" and never writes it again, so the real
# values are set outside Terraform, one at a time:
#
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /mockingbird/ANTHROPIC_API_KEY --value "$ANTHROPIC_API_KEY"
#
# The placeholder goes in through value_wo, a write-only argument: Terraform sends it to AWS on
# create and never stores it. The provider then records the parameter as write-only
# (has_value_wo) and, on every refresh, leaves `value` empty instead of reading the decrypted
# secret back, so the real values never enter terraform.tfstate. A plain `value = "unset"` with
# ignore_changes would only hide the diff: every refresh after put-parameter would copy the
# decrypted value into the state file.
#
# value_wo_version stays at 1. Raising it would make the next apply overwrite the real value
# with "unset".
#
# The container refuses to start while any of these is still "unset" (scripts/entrypoint.sh).

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

  name             = "/${local.name}/${each.key}"
  type             = "SecureString" # encrypted with the AWS-managed aws/ssm key
  value_wo         = "unset"
  value_wo_version = 1
}
