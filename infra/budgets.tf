# An email when this month's AWS spend passes 80% of monthly_budget_usd.
#
# The account runs on credits, and by default a cost budget nets credits off, which would keep
# the measured spend near $0 until the credits run out. Leaving credits out makes the budget
# track what the account is burning through.
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
  }

  notification {
    notification_type          = "ACTUAL"
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alert_email]
  }
}
