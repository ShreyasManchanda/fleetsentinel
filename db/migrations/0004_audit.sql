-- 0004_audit: append-only, hash-chained audit log. No FKs on purpose: audit rows must outlive users/tenants/drivers.
-- hash = sha256(prev_hash || canonical(row)). A BEFORE INSERT trigger computes the chain under an advisory lock, so
-- concurrent writers cannot fork it. UPDATE/DELETE/TRUNCATE are rejected by triggers and revoked from app roles.
-- Tamper demo: a superuser disables the immutability trigger, edits a row, and audit_verify() reports the first broken seq.

CREATE TABLE audit_log (
  seq           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  id            uuid NOT NULL DEFAULT gen_random_uuid(),
  ts            timestamptz NOT NULL,
  tenant_id     uuid,
  actor_id      text,
  actor_type    text,
  action        text NOT NULL,
  resource_type text,
  resource_ids  text[],
  purpose       text,
  decision      text,
  reason        text,
  latency_ms    int,
  attributes    jsonb NOT NULL DEFAULT '{}',
  prev_hash     bytea NOT NULL DEFAULT '\x00',
  hash          bytea NOT NULL DEFAULT '\x00'
);
CREATE INDEX audit_log_tenant_ts ON audit_log (tenant_id, ts DESC);

CREATE FUNCTION audit_canonical(r audit_log) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT concat_ws('|',
    r.id::text,
    to_char(r.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    coalesce(r.tenant_id::text, ''), coalesce(r.actor_id, ''), coalesce(r.actor_type, ''),
    r.action, coalesce(r.resource_type, ''), coalesce(array_to_string(r.resource_ids, ','), ''),
    coalesce(r.purpose, ''), coalesce(r.decision, ''), coalesce(r.reason, ''), coalesce(r.latency_ms::text, ''),
    r.attributes::text)
$$;

CREATE FUNCTION audit_log_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE last_hash bytea;
BEGIN
  PERFORM pg_advisory_xact_lock(727001);
  SELECT hash INTO last_hash FROM audit_log ORDER BY seq DESC LIMIT 1;
  NEW.prev_hash := coalesce(last_hash, '\x00'::bytea);
  NEW.hash := digest(NEW.prev_hash || convert_to(audit_canonical(NEW), 'UTF8'), 'sha256');
  RETURN NEW;
END $$;

CREATE TRIGGER audit_log_chain_ins BEFORE INSERT ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_chain();

CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit_log is append-only'; END $$;

CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();

-- Verify the chain over [from_seq, to_seq]. Returns ok=true when intact, else the first seq whose hash or link is wrong.
CREATE FUNCTION audit_verify(from_seq bigint DEFAULT 0, to_seq bigint DEFAULT 9223372036854775807)
RETURNS TABLE (ok boolean, checked bigint, first_bad_seq bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE r audit_log; prev bytea := NULL; n bigint := 0; expected bytea;
BEGIN
  FOR r IN SELECT * FROM audit_log WHERE seq BETWEEN from_seq AND to_seq ORDER BY seq LOOP
    n := n + 1;
    IF prev IS NOT NULL AND r.prev_hash <> prev THEN
      RETURN QUERY SELECT false, n, r.seq; RETURN;
    END IF;
    expected := digest(r.prev_hash || convert_to(audit_canonical(r), 'UTF8'), 'sha256');
    IF r.hash <> expected THEN
      RETURN QUERY SELECT false, n, r.seq; RETURN;
    END IF;
    prev := r.hash;
  END LOOP;
  RETURN QUERY SELECT true, n, NULL::bigint;
END $$;

CREATE TABLE audit_anchor (            -- daily anchor of the chain head; also written to a WORM (object-lock) bucket
  day        date PRIMARY KEY,
  last_seq   bigint NOT NULL,
  head_hash  bytea NOT NULL,
  object_uri text,
  anchored_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_tenant_read ON audit_log FOR SELECT USING (tenant_id = app_tenant());
