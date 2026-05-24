# Project agent instructions

## Workflow

- Read relevant files before editing and keep diffs scoped to the request.
- Do not commit secrets, `.env` files, credentials, or generated local data.
- Run the relevant validation commands before finishing a file-changing task.
- At the end of every AI execution that changes files, inspect the actual diff,
  stage the relevant changes, and create a git commit.
- Derive the commit message from the actual changed content:
  - Use a concise subject, preferably conventional commit style.
  - Include a short body listing the changed functionality.
  - Include validation results, or state why validation was not run.
- Do not push unless the user explicitly asks for it.

