terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

provider "aws" {
  region = var.region
}

# Managed event log + cache. Postgres is NOT here on purpose (see README).
resource "aws_msk_cluster" "events" {
  cluster_name           = "fleetsentinel-events"
  kafka_version          = "3.7.x"
  number_of_broker_nodes = 3

  broker_node_group_info {
    instance_type   = "kafka.m5.large"
    client_subnets  = var.subnet_ids
    security_groups = [var.kafka_sg_id]
  }
}

resource "aws_elasticache_replication_group" "state" {
  replication_group_id = "fleetsentinel-state"
  description          = "Latest vehicle state, windows, checkpoints, rate limits"
  engine               = "redis"
  node_type            = "cache.r6g.large"
  num_cache_clusters   = 2
}

variable "subnet_ids" {
  type = list(string)
}

variable "kafka_sg_id" {
  type = string
}
