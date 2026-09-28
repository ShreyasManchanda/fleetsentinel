-- 0002_rls: Row-Level Security. The HARD tenant-isolation guarantee: it holds even for raw psql or buggy app code.
-- The API sets the tenant per transaction from the validated JWT:  SELECT set_config('app.tenant_id', '<uuid>', true);
-- Unset/empty => NULL => no rows (deny by default). Roles: fs_app (RLS applies), fs_worker (BYPASSRLS, trusted internal writers).

CREATE OR REPLACE FUNCTION app_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$;

-- tenant table: a tenant can only see itself
ALTER TABLE tenant ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant USING (id = app_tenant());

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'subscription','user_account','fleet','vehicle','subject_key','driver','driver_assignment','depot','alert_rule',
    'trip','alert','risk_prediction','agent_action','work_order','dsr_request','consent_record','vehicle_signature'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant())', t);
  END LOOP;
END $$;

-- knowledge_chunk: global rows (tenant_id NULL) readable by everyone; tenant rows only by their tenant
ALTER TABLE knowledge_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_chunk FORCE ROW LEVEL SECURITY;
CREATE POLICY knowledge_read  ON knowledge_chunk FOR SELECT USING (tenant_id IS NULL OR tenant_id = app_tenant());
CREATE POLICY knowledge_write ON knowledge_chunk FOR ALL    USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
