# Deploy Queue Context to Render

This deployment is one password-protected research workspace. Everyone with its password shares the selected profile, Riot key pool, import job and saved data. It does not provide separate user accounts or public access to import controls.

## GitHub

The repository is [swishmeister/QContext](https://github.com/swishmeister/QContext). Publish the source files at the repository root, with `Dockerfile`, `render.yaml` and `package.json` directly visible. Do not upload a ZIP file as the application, `node_modules`, `.env` files, the local `data` folder, or a database backup. `.gitignore` and `.dockerignore` exclude these files from normal workflows.

## Render setup

1. In Render, choose **New → Blueprint** and connect the QContext repository, branch `main`. Render reads `render.yaml`.
2. Review the proposed **paid web service** and **1 GB persistent disk** before deploying. The blueprint requests a 1 CPU / 2 GB service for the Node frontend and Python collector together. It uses one instance.
3. Enter a unique password of at least 20 characters for `QUEUE_CONTEXT_PASSWORD`. This is the website login password, separate from your Riot key. The username defaults to `owner`.
4. Deploy. The Dockerfile installs dependencies, checks TypeScript, builds the frontend, then starts the production gateway and collector. Render terminates HTTPS. `/healthz` becomes ready when both services respond.
5. Open the Render URL and sign in with the browser's login prompt. Open the settings cog and enter your Riot key(s). Search your Riot ID or refresh the profile to import.

If creating a service manually, choose **Web Service**, runtime **Docker**, Dockerfile `./Dockerfile`, health check `/healthz`, and a paid instance. Add a disk mounted at `/var/data` and set `QUEUE_CONTEXT_DATA_DIR=/var/data`, `QUEUE_CONTEXT_USERNAME=owner` and `QUEUE_CONTEXT_PASSWORD`. Keep the Docker start command unchanged.

An existing Render **Static Site** cannot run the Python collector. Create the web service for this build; do not delete an existing site until the replacement has been verified.

Render supplies `PORT` and `RENDER_EXTERNAL_URL`. For a custom domain, set `QUEUE_CONTEXT_PUBLIC_URL` to its exact HTTPS origin, such as `https://queue.example.com`, then use that address. The configured origin is the only accepted dashboard host. Do not include a path or trailing query.

## Data and restarts

- The database lives at `/var/data/queue-lab.sqlite3`. Its historical name is retained for compatibility.
- Your local computer's database is not uploaded automatically. The hosted app starts with an empty cache.
- A persistent disk preserves the hosted match cache, profile history and manual duo labels across restarts and deploys. Only files under the mount path persist.
- Riot keys stay in server memory, are not returned by the API, and disappear when the service restarts. Re-enter them and resume the saved import after a restart. A development key may also expire independently.
- Use one service instance. This SQLite store and in-memory scheduler are not designed for multiple replicas.
- The built-in JSON export is an observations export, not a complete database backup. Back up the database using SQLite's backup mechanism if a full recovery copy is needed.

The free service tier does not provide the persistent disk required by this configuration. Review current costs in the Render deployment screen before confirming.

## Validation

```sh
pnpm test
pnpm typecheck
pnpm build
pnpm test:production
node scripts/audit-publication.mjs
```

The production test starts the built frontend, gateway and collector with a temporary database. It checks protected access, production assets, CSRF protection, synthetic key handling and data persistence after restart. It never imports from Riot or accesses your local research database.

For local development, continue using `pnpm dev`. `pnpm start` now starts the hosted production build and requires hosting settings. A local production trial can use `QUEUE_CONTEXT_PUBLIC_URL=http://127.0.0.1:10000` plus a password and separate collector/web ports if the development app is already running.

## References

- [Render Docker deployments](https://render.com/docs/docker)
- [Render Blueprint configuration](https://render.com/docs/blueprint-spec)
- [Render persistent disks](https://render.com/docs/disks)
- [Render default environment variables](https://render.com/docs/environment-variables)
