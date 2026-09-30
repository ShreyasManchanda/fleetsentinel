# Live stack (BUILD + APPLIED for the demo, destroyed after judging)

Single EC2 VM + k3s + the exact Helm chart used on kind. Provisions: VPC, one VM
(500 GB encrypted gp3), S3 lake bucket (SSE-KMS), object-locked anchor bucket (WORM,
COMPLIANCE 1 y), budget alarm at $30.

```bash
cd infra/terraform/aws/live
terraform init
terraform apply -var key_name=... -var ssh_cidr=<your-ip>/32 -var alert_email=...
# read the URL:
terraform output demo_url
# after judging:
terraform destroy -var key_name=... -var ssh_cidr=<your-ip>/32 -var alert_email=...
```

Cost: c6i.4xlarge (~$0.68/h) for load/chaos, then resize to m6i.xlarge (~$0.19/h) for
the judging window — **$15–25 total**. RDS is deliberately NOT used: it does not
support TimescaleDB (spec §17.3); Postgres+Timescale runs on the VM via compose.
