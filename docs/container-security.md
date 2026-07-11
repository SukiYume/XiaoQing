# Container security boundary

The runtime image intentionally runs as `root`. XiaoQing's trusted Bot admins
retain unrestricted Shell, Codex, Jupyter, SSH, and related administration
features, so changing the container user would change the accepted product
contract rather than form a reliable sandbox.

The image uses a separate builder stage. GCC, package-manager indexes, and
build-time caches remain in that stage; the runtime stage receives only the
hashed Python virtual environment and the explicit runtime source allowlist.
Local secrets and state are excluded by `.dockerignore` and must be injected at
runtime.

Operational rules:

- Never mount `/var/run/docker.sock` or another container-engine control socket.
- Mount only host directories that trusted Bot admins are allowed to read and
  modify. A root process in the container may have the mount's host-level
  permissions; the container is not a security boundary for those paths.
- Keep OneBot ingress authenticated and expose high-privilege plugins only to
  the configured Bot admin IDs.
- Prefer read-only mounts for inputs that admin workflows do not need to edit.
- Treat network access and writable volumes as intentional admin capabilities,
  not as sandboxed execution.
