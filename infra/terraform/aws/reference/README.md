# Reference stack ([DESIGNED], plan-validated only — never applied)

The production shape the live demo deliberately is not: EKS, MSK (Kafka API),
ElastiCache (Redis), S3. Postgres+Timescale runs **in-cluster via CloudNativePG**
(or Timescale Cloud) and MongoDB in-cluster or on Atlas.

**Not RDS: AWS RDS does not support the TimescaleDB extension**, so an RDS-based
reference could never run our schema (spec §17.3). This file exists to state that
decision and to `terraform plan`-validate the surrounding managed services.

```bash
cd infra/terraform/aws/reference
terraform init -backend=false
terraform validate
```
