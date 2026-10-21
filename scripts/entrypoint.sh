#!/bin/sh
# The container's entrypoint: check the secrets, bring the database schema up to date, then run
# CMD (uvicorn).
set -eu

# Terraform creates every SSM parameter with the placeholder "unset" (infra/secrets.tf), and
# that string is public. If one put-parameter was missed, refuse to start rather than serve with
# a guessable secret: OWNER_KEY=unset would let anyone skip the demo limits, and BRAIN_SECRET or
# WEBHOOK_SECRET=unset would make the Vapi paths guessable. Only names are printed, never values.
fail() {
  echo "entrypoint: $1" >&2
  exit 1
}

for name in ANTHROPIC_API_KEY VAPI_API_KEY DATABASE_URL BRAIN_SECRET WEBHOOK_SECRET OWNER_KEY VISITOR_SALT; do
  value=$(printenv "$name" || true)
  if [ "$value" = "unset" ]; then
    fail "$name is still Terraform's placeholder \"unset\". Set it with aws ssm put-parameter --overwrite, then redeploy."
  fi
  # DATABASE_URL may be absent (the server then keeps interviews in memory). The rest are
  # required, and Settings would reject them anyway, but only after the migration had run.
  if [ -z "$value" ] && [ "$name" != DATABASE_URL ]; then
    fail "$name is not set."
  fi
done

# create_app does not migrate, so this is where Supabase gets its tables. Without DATABASE_URL
# there is nothing to migrate. If a migration fails, the container exits instead of serving on a
# half-applied schema.
if [ -n "${DATABASE_URL:-}" ]; then
  uv run python -m mockingbird.migrate
fi

# exec, so uvicorn (through uv) gets ECS's stop signal directly and can shut down cleanly.
exec "$@"
