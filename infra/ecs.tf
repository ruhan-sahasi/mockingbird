# One Fargate task running the server image, behind the ALB.

resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 14
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "App port from the ALB only"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  description                  = "App port from the ALB"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.alb.id
}

# The task calls out to Vapi, Anthropic, Supabase, ECR, SSM and CloudWatch Logs, all over the
# internet (there are no VPC endpoints). Nothing can connect in except the ALB.
resource "aws_vpc_security_group_egress_rule" "task_out" {
  security_group_id = aws_security_group.task.id
  description       = "Outbound to the internet"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = "app"
    image     = "${aws_ecr_repository.app.repository_url}:latest"
    essential = true

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    # Not a secret: this is where Vapi and browsers reach the server.
    environment = [
      { name = "PUBLIC_BASE_URL", value = "https://${aws_cloudfront_distribution.main.domain_name}" },
    ]

    # ECS reads each parameter at task start. The task definition holds only the ARNs.
    secrets = [
      for name, p in aws_ssm_parameter.secret : { name = name, valueFrom = p.arn }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "app"
      }
    }
  }])
}

resource "aws_ecs_service" "app" {
  name            = local.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  launch_type     = "FARGATE"

  # Exactly one task, always: live interviews are held in that task's memory (the session
  # registry), so a second task would receive requests for calls it knows nothing about.
  # A deploy therefore stops the old task before starting the new one (0% healthy minimum, 100%
  # maximum), which means a short outage on each deploy instead of two tasks overlapping.
  desired_count                      = 1
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  # Room for the entrypoint to run migrations before uvicorn answers /healthz.
  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # no NAT gateway, so this is how the task reaches the internet
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8000
  }

  # The target group must be attached to a listener before a service can register into it.
  depends_on = [aws_lb_listener.http]
}
