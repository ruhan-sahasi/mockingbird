variable "region" {
  description = "AWS region for everything except the global services (CloudFront, Budgets)."
  type        = string
  default     = "us-east-1"
}

variable "alert_email" {
  description = "Where the budget alert is sent. Set it in terraform.tfvars, which is gitignored."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly AWS spend that triggers the alert at 80%. This is credit burn, not cash."
  type        = number
  default     = 50
}
