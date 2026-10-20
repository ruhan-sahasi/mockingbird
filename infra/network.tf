# A VPC with two public subnets and no NAT gateway. The Fargate task gets a public IP so it can
# reach Vapi, Anthropic, Supabase and ECR directly. A NAT gateway would be a fixed monthly cost,
# and the security groups already keep everything but the load balancer off the task.

data "aws_availability_zones" "available" {
  state = "available"

  # Fargate does not run ARM64 tasks in this us-east-1 zone (AWS's "task definitions for 64-bit
  # ARM workloads" page). In any other region the filter matches nothing.
  exclude_zone_ids = ["use1-az3"]
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = local.name }
}

# An ALB needs subnets in at least two availability zones.
resource "aws_subnet" "public" {
  count = 2

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index) # 10.20.0.0/24, 10.20.1.0/24
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
