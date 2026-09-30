output "demo_url" {
  description = "Public HTTPS URL judges can click (Traefik + Let's Encrypt on the VM)"
  value       = "https://app.${replace(aws_eip.demo.public_ip, ".", "-")}.sslip.io"
}

output "instance_id" {
  value = aws_instance.demo.id
}

output "lake_bucket" {
  value = aws_s3_bucket.lake.bucket
}

output "anchors_bucket" {
  value = aws_s3_bucket.anchors.bucket
}
