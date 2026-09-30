{{- /* Common labels + names for every service. */ -}}
{{- define "fs.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: Helm
{{- end -}}

{{- define "fs.env" -}}
- name: KAFKA_BOOTSTRAP
  value: {{ .Values.datastores.kafka | quote }}
- name: POSTGRES_DSN
  valueFrom: { secretKeyRef: { name: fs-datastore, key: postgres-dsn } }
- name: REDIS_URL
  value: {{ .Values.datastores.redis | quote }}
- name: MONGO_URL
  value: {{ .Values.datastores.mongo | quote }}
- name: S3_ENDPOINT
  value: {{ .Values.datastores.s3Endpoint | quote }}
- name: OIDC_ISSUER
  value: {{ .Values.datastores.keycloak | quote }}
{{- end -}}
