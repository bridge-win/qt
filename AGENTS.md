# Project agent instructions

## Workflow

- Read relevant files before editing and keep diffs scoped to the request.
- Do not commit secrets, `.env` files, credentials, or generated local data.
- Run the relevant validation commands before finishing a file-changing task.
- On this Mac, run all local container commands with explicit
  `docker --context orbstack`. Use `deploy/orbstack.sh` for this project's
  services. Do not change the global context or operate on other projects.
- At the end of every AI execution that changes files, inspect the actual diff,
  stage the relevant changes, create a git commit, and push the current branch
  to its configured remote.
- Derive the commit message from the actual changed content:
  - Use a concise subject, preferably conventional commit style.
  - Include a short body listing the changed functionality.
  - Include validation results, or state why validation was not run.
- If the push fails, report the exact failure and leave the local commit intact.
