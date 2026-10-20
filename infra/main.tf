# Mockingbird's AWS stack: CloudFront in front of an S3 site and an ALB, the ALB in front of
# one Fargate task. State is local in phase 1 (infra/terraform.tfstate, gitignored).
#
#   cp terraform.tfvars.example terraform.tfvars   # then fill in alert_email
#   terraform init && terraform plan

terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.69"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = local.name
    }
  }
}

locals {
  name = "mockingbird"
}
