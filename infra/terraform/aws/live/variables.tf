variable "region" {
  description = "AWS region for the demo box"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "c6i.4xlarge for load/chaos evidence, then m6i.xlarge for the judging window"
  type        = string
  default     = "c6i.4xlarge"
}

variable "ami" {
  description = "Ubuntu 24.04 amd64 AMI in var.region (re-verify at apply time)"
  type        = string
  default     = "ami-0e86e20dae9224db8"
}

variable "key_name" {
  description = "Existing EC2 key pair for operator SSH"
  type        = string
}

variable "ssh_cidr" {
  description = "CIDR allowed to SSH (operator IP, never 0.0.0.0/0)"
  type        = string
}

variable "alert_email" {
  description = "Budget alarm recipient"
  type        = string
}
