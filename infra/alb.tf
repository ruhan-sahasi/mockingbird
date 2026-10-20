# The load balancer between CloudFront and the task. It listens on plain HTTP: viewers get HTTPS
# from CloudFront's *.cloudfront.net certificate, and only CloudFront can reach this listener.

# AWS keeps this list of CloudFront's origin-facing IP ranges up to date.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "HTTP from CloudFront only"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront origin-facing ranges"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
}

# The ALB only ever talks to the task, so its outbound traffic is the task port and nothing else.
resource "aws_vpc_security_group_egress_rule" "alb_to_task" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To the app container"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.task.id
}

resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  # The default 60 s would drop an interview's WebSocket during a long pause. The relay also
  # pings the browser every 20 s, and the browser sends mic frames the whole time.
  # Keep uvicorn's --timeout-keep-alive (310, in the Dockerfile) above this value, or uvicorn
  # closes idle connections the ALB still reuses, and those requests fail with a 502.
  idle_timeout = 300

  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "app" {
  name        = local.name
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip" # Fargate tasks register by IP
  vpc_id      = aws_vpc.main.id

  # Plain /healthz does not touch the database, so a Supabase hiccup does not get the task
  # killed. The keep-alive job checks the database with ?db=1. Two passes 15 s apart mark a new
  # task healthy (the provider's default is three, 30 s apart), which shortens the gap during a
  # deploy.
  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
