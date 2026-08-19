-- Failed sign-in attempts, counted in the database because serverless
-- instances share no memory — an in-process counter resets on every cold
-- start and throttles nothing.
CREATE TABLE IF NOT EXISTS auth_failures (
    id         bigserial PRIMARY KEY,
    email      citext      NOT NULL DEFAULT '',
    ip         inet,
    ts         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auth_failures_email_idx ON auth_failures (email, ts DESC);
CREATE INDEX IF NOT EXISTS auth_failures_ip_idx    ON auth_failures (ip, ts DESC);
