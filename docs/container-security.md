# Container security boundary

The runtime image intentionally runs as `root`. XiaoQing's trusted Bot admins
retain unrestricted Shell, Codex, Jupyter, SSH, and related administration
features, so changing the container user would change the accepted product
contract rather than form a reliable sandbox.

## Release-only build context

Never run `docker build` against the repository workspace. Build one wheel into
a clean distribution directory, then generate a new, dedicated context:

```bash
build_root="$(mktemp -d)"
trap 'rm -rf -- "$build_root"' EXIT
dist="$build_root/dist"
context="$build_root/context"
mkdir "$dist"
python -m pip wheel . --no-deps --wheel-dir "$dist"
python scripts/build_docker_context.py --dist-dir "$dist" --context-dir "$context"
docker build -t xiaoqing "$context"
```

`build_docker_context.py` requires exactly one ordinary `xiaoqing-*.whl`
candidate in the selected distribution directory. It checks the filename and
the wheel's `METADATA` Name/Version against the current `pyproject.toml`, safely
unpacks the archive into a separate inspection directory, and rejects absolute
or parent-traversing names, duplicate members, links, special files, encrypted
members, and file-count or uncompressed-byte budget violations. The unpacked
wheel must pass the CR-201 secret scanner.

The fresh context contains exactly these six ordinary files:

- `.dockerignore`
- `Dockerfile`
- `artifacts/xiaoqing.whl`
- `requirements/python-3.13-runtime.lock`
- `config/config.json.example`
- `config/secrets.json.example`

The completed context is scanned again before the helper reports success. A
failure removes the partially created context. The destination must not already
exist, so stale files, local plugin directories, ignored files, databases, and
workspace credentials cannot be inherited accidentally. `.dockerignore` also
denies everything and re-allows only the six exact paths; it is a final guard,
not the mechanism used to select release files.

The builder installs dependencies only from the Python 3.13 runtime lock with
`--require-hashes`, then installs the validated fixed-name wheel with
`--no-deps`. The runtime stage receives that installed wheel, the locked virtual
environment, and the two public examples. It never receives `core/`, `plugins/`,
the workspace, or the original `dist/` directory.

The repository maintains separate hashed locks for Python 3.10 through 3.13.
Each `*-runtime.lock` contains every enabled plugin feature (including ML,
Jupyter, astronomy, Web, and SSH support) but excludes test, lint, audit, and
formatting tools. Each `*-ci.lock` is the corresponding runtime environment plus
the `dev` extra. `requirements.txt` deliberately selects the current Python
3.13 runtime profile; CI selects the version-matched CI profile.

Delete the temporary context after the image has been built. Do not reuse it for
a later release; regenerate it from that release's wheel.

## Mandatory image verification

Every release context must pass the real Docker verification gate before the
image or any of its build cache can be uploaded. CI runs the gate in its own
`docker-release-smoke` job after the full Python test matrix succeeds:

```bash
python scripts/verify_docker_release.py \
  --context-dir "$context" \
  --image-tag "xiaoqing-ci:${GITHUB_SHA}" \
  --require-docker
```

`--require-docker` is fail-closed: a missing or unusable Docker/BuildKit daemon
is a release failure, never a skipped check. The verifier performs a real
BuildKit build, checks the command-line help in a container with
`--network none`, starts the Bot without container networking or a published
port, and polls its authenticated `/health` endpoint from inside the container.
It then exports the image and scans every historical layer, including deleted
files, for secret findings and the release canary. Inspecting only the final
root filesystem or `docker history` is insufficient because a secret deleted
by a later layer is still recoverable from the image archive.

Do not add remote cache export, `docker push`, image artifact upload, or another
upload step before this command succeeds. Local BuildKit cache may contain any
byte sent to a build layer, so CI treats the verified image as the first point
at which release output may leave the runner.

## Trusted-admin operating rules

- Never mount `/var/run/docker.sock` or another container-engine control socket.
- Mount only host directories that trusted Bot admins are allowed to read and
  modify. A root process in the container may have the mount's host-level
  permissions; the container is not a security boundary for those paths.
- Keep OneBot ingress authenticated and expose high-privilege plugins only to
  the configured Bot admin IDs.
- Prefer read-only mounts for inputs that admin workflows do not need to edit.
- Inject real `config.json` and `secrets.json` only at runtime through an
  explicitly authorized volume or secret mount.
- Treat network access and writable volumes as intentional admin capabilities,
  not as sandboxed execution.
