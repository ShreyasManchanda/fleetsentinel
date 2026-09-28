-- 0007_grants: least-privilege roles. Roles themselves are created by `fs migrate` (passwords from env, never in git).
--   fs_app    : API + agent-facing tool endpoints. RLS APPLIES. No DDL, no DELETE, no audit writes.
--   fs_worker : trusted internal writers (processor, normalizer, sinks, batch, simulator seed). BYPASSRLS.

GRANT USAGE ON SCHEMA public TO fs_app, fs_worker;

-- fs_app: read everything RLS/global allows; raw hot-tier tables are only reachable through the security-barrier views.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fs_app;
REVOKE SELECT ON telemetry_1m, fleet_metrics_1h, mv_fleet_cost_daily FROM fs_app;
GRANT SELECT ON v_telemetry_1m, v_fleet_metrics_1h, v_fleet_cost_daily TO fs_app;

GRANT INSERT, UPDATE ON agent_action, work_order, dsr_request, consent_record, user_account, schema_mapping TO fs_app;
GRANT UPDATE ON driver, subject_key TO fs_app;                     -- DSR erasure nulls PII columns / destroys the wrapped DEK
GRANT INSERT ON subject_key TO fs_app;
GRANT UPDATE (status, acknowledged_by, resolved_at) ON alert TO fs_app;
GRANT INSERT ON risk_prediction TO fs_app;

-- fs_worker: full access to data tables; audit is append-only for everyone.
GRANT ALL ON ALL TABLES IN SCHEMA public TO fs_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fs_worker;

REVOKE ALL ON audit_log FROM fs_app, fs_worker;
GRANT SELECT ON audit_log TO fs_app;                                -- RLS limits it to the caller's tenant
GRANT SELECT, INSERT ON audit_log TO fs_worker;
GRANT USAGE, SELECT ON SEQUENCE audit_log_seq_seq TO fs_worker;
GRANT SELECT, INSERT, UPDATE ON audit_anchor TO fs_worker;
GRANT SELECT ON audit_anchor TO fs_app;

GRANT EXECUTE ON FUNCTION audit_verify(bigint, bigint) TO fs_app, fs_worker;
