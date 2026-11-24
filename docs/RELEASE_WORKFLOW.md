# Release workflow

## Verify in staging

1. Use an isolated database, storage and vector index. Set environment values using `deploy/app.env.example`; use `deploy/h5-workbench/rag.env.example` for Pageo settings. Enable H5 only after the Pageo service and its application credential are configured.
2. Install locked dependencies with `uv sync --locked`. Run `uv run python scripts/check-production-config.py --env-file <environment-file>`. On the Linux target use `--check-paths` to verify the configured paths.
3. Inspect account migration status with `uv run python scripts/migrate-accounts.py`. After verifying the staging database target and its backup, use the same command with `--apply`. The command uses the process environment and the repository’s local `.env`. Confirm the database target before running it.
4. Run the relevant tests and check the staging site. Exercise each research type through the project entry point. Verify knowledge search, version access, export and preview. Verify H5 with a staging Pageo instance and check that restricted artifacts stay restricted.

## Prepare and activate a release

Build from the reviewed Git commit with `uv run python scripts/build-release.py --ref <commit> --output-dir <directory>`. Record the archive checksum and keep the manifest with the release. Stage the extracted files under a versioned release directory and install the dependencies for that directory. Use `deploy/supermew.service` and `deploy/nginx-production.conf.example` as configuration references.

For an approved Linux release, retain the existing version and verify a recent recoverable backup before using `deploy/activate-release.py`. Pass the staged directory with `--candidate`, the current release directory with `--expected-current`, the application hostname with `--public-host`, and `--run` to activate. This changes the live service. See `--help` before running it. Check the release pointer, public `/version`, and an application smoke test after activation. Keep the previous release available for rollback.
