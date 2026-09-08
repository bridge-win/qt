# QT edge integration handoff

`worker.ts` is the canonical Worker code for the QT research workbench.
The deployment lead must replace the insecure root worker with this source (or
re-export it), then merge `wrangler.fragment.toml` into the one canonical root
Wrangler configuration. Do not deploy both files as independent Workers.

Required deployment bindings:

- Static assets binding `ASSETS` from `web/dist`.
- Private R2 binding `REPORTS`; all externally visible artifact keys map only
  to `reports/<validated-key>`.
- Non-secret environment values: `ACCESS_JWT_ISSUER`,
  `ACCESS_JWT_AUDIENCE`, `ORIGIN_URL`, optional `MAX_API_BODY_BYTES`.
- Secrets: `ORIGIN_CLIENT_ID`, `ORIGIN_CLIENT_SECRET` through `wrangler secret
  put`; neither is a browser variable, Git value, log field, or plugin mount.

Cloudflare Access remains the account-level entry gate for the deployed
workers.dev site. Inside the Worker, signed `Cf-Access-Jwt-Assertion`
verification is enforced for `/api/*` and `/artifacts/*` before proxying the
API origin or reading private R2 artifacts. Static SPA assets are still served
through the Worker with hardened response headers, but they do not perform a
second in-Worker JWT verification because Access already gates the site before
the request reaches Worker code. API 404 responses pass through as API
responses and can never become SPA HTML.
