terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "FleetSentinel", ManagedBy = "terraform", Ephemeral = "true" } }
}

# --- VPC (single AZ: this is a demo box, not prod) ---
resource "aws_vpc" "demo" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
}

resource "aws_subnet" "demo" {
  vpc_id                  = aws_vpc.demo.id
  cidr_block              = "10.42.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = "${var.region}a"
}

resource "aws_internet_gateway" "demo" { vpc_id = aws_vpc.demo.id }

resource "aws_route_table" "demo" {
  vpc_id = aws_vpc.demo.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.demo.id
  }
}

resource "aws_route_table_association" "demo" {
  subnet_id      = aws_subnet.demo.id
  route_table_id = aws_route_table.demo.id
}

resource "aws_security_group" "demo" {
  name   = "fleetsentinel-demo"
  vpc_id = aws_vpc.demo.id

  ingress {
    description = "HTTPS (demo UI + API)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "SSH (operator only)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- Single VM: k3s + the Helm chart + datastores via compose ---
resource "aws_instance" "demo" {
  ami                    = var.ami
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.demo.id
  vpc_security_group_ids = [aws_security_group.demo.id]
  key_name               = var.key_name
  root_block_device {
    volume_size = 500
    volume_type = "gp3"
    encrypted   = true
  }
  user_data = templatefile("${path.module}/cloud-init.sh", {
    hostslip = replace(aws_eip.demo.public_ip, ".", "-")
    lake     = aws_s3_bucket.lake.bucket
    anchors  = aws_s3_bucket.anchors.bucket
    kms_key  = aws_kms_key.lake.arn
  })

  tags = { Name = "fleetsentinel-demo" }
}

resource "aws_eip" "demo" { domain = "vpc" }

# --- Lake bucket (SSE-KMS) + object-locked audit-anchor bucket (WORM) ---
resource "aws_kms_key" "lake" {
  description             = "FleetSentinel lake SSE-KMS"
  deletion_window_in_days = 7
}

resource "aws_s3_bucket" "lake" { bucket_prefix = "fleetsentinel-lake-" }

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.lake.arn
    }
  }
}

resource "aws_s3_bucket" "anchors" {
  bucket_prefix       = "fleetsentinel-anchors-"
  object_lock_enabled = true
}

resource "aws_s3_bucket_object_lock_configuration" "anchors" {
  bucket = aws_s3_bucket.anchors.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 365
    }
  }
}

# --- Budget alarm: kill-switch against a forgotten VM ---
resource "aws_budgets_budget" "demo" {
  name         = "fleetsentinel-demo"
  budget_type  = "COST"
  limit_amount = "30"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}
