# Fluxer Cinema

A shared Jellyfin movie night in a Fluxer voice channel. Browse your library in a signed-in web player, build a shared queue, and watch the bot's video stream inside Fluxer.

This is a standalone service, not a Jellyfin plugin. One deployment serves one Fluxer instance, one community, and one active stream. Movies and TV episodes are supported. The queue, sign-in sessions, and playback position reset on restart.

**Status:** implemented and locally tested; live bot screen sharing, receiver synchronization, Intel acceleration, and the 30-minute 1080p acceptance run still require validation on your servers. The included compatibility probe is the first deployment gate. GitHub Actions builds, smoke-tests, and publishes the container.

## How it works

```text
Jellyfin ──private HTTP──> media worker ──screen video + audio──> Fluxer
                              ↑
Browser ──sign-in / controls──> Python backend
```

- Python 3.13/FastAPI handles OAuth, catalog access, the queue, and Fluxer's Gateway.
- A separate supervised Python process uses PyAV and LiveKit RTC to decode, pace, and publish media. Credentials are passed over stdin, never command arguments.
- React/TypeScript provides the responsive control UI. Video and audio play in Fluxer, not in the browser control page.
- Fluxer endpoints are discovered through `/.well-known/fluxer`; no hosted-instance domain is hardcoded. The bot uses normal bot credentials and never calls the bot-restricted stream preview/region API.
- Media stays on the private Jellyfin connection. By default, Jellyfin prepares an H.264 transcode at up to 1920×1080 and 30 fps, preserving lower source frame rates and aspect ratio. The bot decodes that stream and publishes H.264 video with stereo audio to Fluxer.

## Configure Fluxer

1. On your chosen Fluxer instance, create an application and save its application ID, client secret, and bot token.
2. Register the exact callback `https://cinema.example.com/auth/callback`, substituting your control UI origin.
3. Install the bot into the community. Grant **View Channel**, **Connect**, and **Stream** in the intended voice channels.
4. Copy `.env.example` to a private `.env`, then set the instance origin, application credentials, and community ID. Optionally restrict `FLUXER_CHANNEL_IDS` to a comma-separated list.
5. Forward HTTPS for `PUBLIC_ORIGIN` to the service's HTTP port 8080. Preserve the browser's `Origin` header. Disable proxy buffering for `/api/events` and allow long-lived responses.
6. Set `TRUSTED_PROXY_CIDRS` to the actual proxy peer addresses seen by the container. Forward or append `X-Forwarded-For` at that proxy. The default trusts no forwarded headers, so users behind a proxy share its IP limit until configured. Do not trust every address or an untrusted network. Keep the container's `--no-proxy-headers` Uvicorn flag; the application validates the proxy chain itself.

OAuth uses `identify`, authorization codes, state, and S256 PKCE. Browsers receive an eight-hour HTTP-only session cookie. No Fluxer or Jellyfin tokens are returned to the browser. Every control checks current community membership and voice presence on the server. Browsing requires community membership; controlling the active session requires being in the same voice channel as the bot.

## Configure Jellyfin

Create a dedicated **non-admin user** with only the movies/TV libraries you want shared. Enable media playback and the audio/video transcoding permissions needed for subtitles and format conversion. Disable other privileges that account does not need. Use a **user access token**, not a server-wide administrator API key.

The token helper prompts for the password without putting it in command history, and writes a new mode-0600 file:

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m cinema.jellyfin_token --url http://127.0.0.1:8096
```

Merge the two generated settings from `.env.jellyfin` into your private `.env`; also set `JELLYFIN_URL`. Base paths such as `http://jellyfin:8096/jellyfin` are supported. Do not commit credential files.

All viewers share this account's catalog, audio track, and burned-in subtitles. Access to the Fluxer voice channel determines who can watch the stream. This is not individual Jellyfin-account authorization, and playback history belongs to the bot account.

`JELLYFIN_FORCE_TRANSCODE` defaults to `true`, including when omitted. Jellyfin performs actual video encoding (stream copying is disabled), capped at 1080p/30 and `VIDEO_BITRATE`. This gives the bot a consistent input format, but adds server work and another lossy encoding step; startup and seeking can take longer. Hardware acceleration must be configured separately in Jellyfin. The bot still decodes and re-encodes for Fluxer, so this is not a guarantee that every source will play smoothly.

Set `JELLYFIN_FORCE_TRANSCODE=false` in the environment or Kubernetes ConfigMap and restart the bot to allow compatible titles to play directly. Subtitles and HDR still require transcoding. If Jellyfin cannot provide a required transcode, playback fails instead of silently falling back to direct decoding. Existing deployments adopting the default must permit video transcoding for the dedicated Jellyfin user.

## Docker Compose

Build locally:

```sh
cp .env.example .env
# Fill in .env before starting.
docker compose up -d --build
```

The example joins an **existing** Docker network named `jellyfin`. Set `JELLYFIN_NETWORK` to the network your Jellyfin container uses. Jellyfin's network alias should match the host in `JELLYFIN_URL`, normally `http://jellyfin:8096`. Both containers remain on the same host; no media directory needs to be shared with the bot.

The default image is `ghcr.io/jgerega107/jellyfin-fluxer-bot:main`. Prefer a full `sha-COMMIT` tag or digest for a repeatable deployment: set `CINEMA_IMAGE` in `.env`, then use `docker compose pull` and `docker compose up -d --no-build`. This repository and its initial package are private: authenticate your Docker client to `ghcr.io` with credentials authorized to read the package. Forks can set their own owner/image name.

For Jellyfin installed directly on the Docker host, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the service on Linux and use `JELLYFIN_URL=http://host.docker.internal:8096`. Jellyfin must listen on an address reachable from the bridge network; `127.0.0.1` inside this container refers to the bot itself. Docker Desktop already provides the host name. The example publishes the UI only on host loopback for an HTTPS reverse proxy.

Optional Intel device access:

```sh
export RENDER_GID=$(stat -c '%g' /dev/dri/renderD128)
docker compose -f compose.yaml -f deploy/compose.intel.yaml up -d --build
```

The image includes Intel VAAPI drivers. Device access permits the LiveKit SDK's encoder selection; it is **not proof** that a particular wheel/driver/device is accelerating H.264. Verify with the probe and GPU monitoring. Jellyfin's own QSV configuration only accelerates Jellyfin's transcode stage. Software encoding remains available and requires adequate CPU for 1080p.

The Linux worker requires a kernel with Landlock enabled (5.13 or newer) and a container runtime that permits Landlock/seccomp setup. It fails closed when confinement cannot be installed. The worker receives read access to runtime libraries, its own process information, optional GPU devices, and an explicitly selected local probe file. It can write only its private temporary directory. It cannot inspect backend memory, signal other processes, launch programs, or connect to Unix-domain services. Outbound IP networking is required for Jellyfin and Fluxer; the worker still possesses its scoped voice grant and dedicated Jellyfin media credentials. macOS development does not provide this Linux confinement.

## Kubernetes

`deploy/kubernetes` is a separate, single-replica Deployment using `Recreate` updates. It connects through Jellyfin's internal Service and can run on another node. It does not modify the Jellyfin workload or mount its media storage.

1. Set real origins, IDs, and the private Jellyfin Service URL in `config.yaml`.
2. Replace the moving `main` image in `deployment.yaml` with a published commit tag or digest.
3. Create the namespace and a real Secret named `cinema` using your secret-management workflow. `secret.example.yaml` documents the keys and is deliberately excluded from the kustomization. Also create a Docker registry Secret named `ghcr` for the private image; remove `imagePullSecrets` if using a public image.
4. Adjust the ingress class/hostname and supply `cinema-tls`. The example uses an nginx Ingress; other controllers need equivalent HTTPS, streaming, and timeout configuration.
5. Render and apply:

```sh
kubectl kustomize deploy/kubernetes
kubectl apply -k deploy/kubernetes
```

For Intel acceleration, use `deploy/kubernetes-intel`. Install the Intel device plugin first, adjust `gpu.intel.com/i915` if your driver advertises another resource, and set the correct render group in `supplementalGroups`. This overlay intentionally schedules only onto a node with a GPU allocation; use the base deployment for software operation.

Allow outbound access to Jellyfin, the configured Fluxer API/Gateway, and the media endpoints provided in voice grants (including WebRTC/TURN connectivity). The bot does not need a LiveKit administrator API key. Temporary storage is bounded to 512 MiB. CPU/memory defaults are starting points to measure on your host, not a certified 1080p minimum.

## Controls and recovery

- First play joins the requesting user's permitted voice channel. The existing session stays in that channel until stopped.
- All present community members can play, enqueue, reorder/remove queued items, pause/resume, seek, skip, change shared volume/audio/subtitles, and stop/disconnect.
- Queue capacity is 250 titles. Playback commands carry the current item key so stale controls cannot skip a new title.
- A pause keeps the publication alive for late viewers. Seek/track changes replace buffered media at the chosen position.
- After 60 seconds without human participants, the bot disconnects and clears its queue.
- A connection interruption pauses playback. Controls are disabled until a fresh Gateway membership snapshot is available; resumption is explicit.
- Worker errors retain the selected title for retry, release Jellyfin encodings, and display a sanitized error. A full service restart clears everything and signs users out.

## Live compatibility probe

Run the probe **instead of** the web service, using the same private configuration. Join the explicitly selected test voice channel as a viewer. The probe joins and publishes as the configured bot:

```sh
.venv/bin/python -m cinema.probe \
  --env-file .env \
  --channel YOUR_TEST_CHANNEL_ID \
  --item YOUR_JELLYFIN_MOVIE_ID \
  --seconds 1800 \
  --exercise-controls \
  --output validation/probe.json
```

Alternatively, use `--file /absolute/path/to/a/test-clip.mp4`. The probe supports an encrypted voice grant, repeats at EOF, pauses at 30 seconds, resumes at 35 seconds, and seeks to 10 seconds after one minute. Use a clip longer than 10 seconds for the exercise. Container users can override the command with `python -m cinema.probe` and mount the test clip and output directory explicitly.

Open the bot's stream in Fluxer and verify picture **and** sound. The JSON report contains publisher measurements only and leaves `viewer_verified` false. Receiver lip-sync, late subscription, network recovery, device acceleration, and actual quality require the checks in [docs/VALIDATION.md](docs/VALIDATION.md). A bot that joins voice without a watchable screen-share has not passed this gate.

## Development and tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check cinema tests
cd web
npm ci
npm run build
npx playwright install chromium
npm test
```

For local UI development, set `PUBLIC_ORIGIN=http://localhost:5173` and `ALLOW_INSECURE_LOCAL=true`, and register `http://localhost:5173/auth/callback` on the development Fluxer application. Run `.venv/bin/uvicorn cinema.app:create_app --factory --host 127.0.0.1 --port 8080 --no-access-log --no-proxy-headers` from the project root, then `npm run dev` in `web`. Open the UI at `localhost:5173`. Vite proxies `/api` and `/auth` to the backend. The insecure option is for an isolated development environment only.

Browser tests use clearly scoped mock catalog/events and exercise desktop/mobile layouts and controls; they do not establish live Fluxer compatibility. Python tests also generate real media to exercise decoding, stereo audio, timeline preservation, seeks, and pause timing.

## Release and operations

Sign-in starts use signed, short-lived opaque cookies without allocating a shared pending-login pool. PKCE verifiers are derived server-side; successful callback attempts are single-use. Authentication requests allow a burst of 10 per client IP, refilling at 20/minute, with an additional global budget. IPv6 clients share a /64 budget. Playback requests allow a burst of 10 per user, refilling at 60/minute; reads allow 80, refilling at 240/minute. Limits are checked before upstream authorization, and concurrent requests are bounded separately. HTTP 429 responses include `Retry-After`. These limits reduce application-level abuse; an internet-facing reverse proxy still needs ordinary connection and traffic controls.

Successful artwork responses are also cached privately in the browser for one hour, varying by session cookie; shared proxies and CDNs must not cache them. Failed requests remain uncached. Previously downloaded artwork can remain visible for that hour after library access changes, and replaced artwork refreshes when its browser cache expires. No disk cache or additional volume is needed. Subtitle changes rebuild the stream for burn-in and seek the full Jellyfin HLS timeline to the current position, preserving pause state.

Authenticated catalog and artwork responses are cached server-side for up to 10 seconds, with separate 4 MiB/32 MiB serialized-data budgets and entry caps. Membership is still checked on protected routes; playback resolution always rechecks Jellyfin directly. Library metadata permission changes can take up to that cache TTL to disappear. Request bodies are limited to 16 KiB and five seconds; compressed request bodies and Brotli upstream responses are rejected before decoding.

The production web server is Python and serves static React assets. It has no Node runtime, Next.js, or React Server Components. The builder is pinned to a Node release containing the CVE-2026-111497 (Node2Shell/Brotli) fix. Image checks verify absence of Node and optional Python Brotli modules, the Debian Brotli fix if that library is installed, and actual worker confinement. See [security notes](docs/SECURITY.md) for the advisory links and remaining boundaries.

Dependabot checks Python, npm, Docker, and GitHub Actions weekly. Repository security alerts and security-update PRs are enabled separately on GitHub. Updates require review and passing CI; they do not auto-merge or deploy to your servers.

The GitHub Actions workflow tests Python, builds the web UI, runs browser tests, renders deployment manifests, builds the Linux amd64 container, and smoke-tests its health endpoint and native media libraries without network access. Successful pushes to `main` publish to `ghcr.io/<repository-owner>/<repository-name>` with a full `sha-<commit>` tag and `main`; `vX.Y.Z` tags also publish `X.Y.Z`. Existing commit images are reused, and version tags point to those images instead of rebuilding or replacing them. Use the repository name `jellyfin-fluxer-bot` for the planned image name. The workflow uses `GITHUB_TOKEN` with package-write permission.

`/healthz` reports process health; `/readyz` requires both Jellyfin authentication and a synchronized Fluxer Gateway. Playback failures are visible in the player. Application logs omit credentials, source URLs, OAuth codes, and grant data; HTTP access logging is disabled by the container command. The browser receives publisher FPS and A/V drift for diagnostics. Queue persistence, multiple replicas, simultaneous rooms, chat commands, Live TV, and personal watch histories are outside v1.


## Instance branding and requesters

The player discovers the instance name, accent color, logo, favicon, and optional
sign-in backdrop from `app_public.branding`. It uses the same discovery fields
as the music player. Personal client themes and arbitrary instance CSS are not
applied. Branding refreshes when the Gateway reconnects; reload the page to see
updates. Missing or broken artwork falls back to text, with three delayed retries to recover from temporary failures. Artwork has a separate bounded request budget, admitting a full catalog page without occupying playback API slots.

Current and queued titles include the requester's community display name and
avatar, captured from verified membership when the title is requested. Community
avatars take precedence over global avatars; missing images show initials.

Set the bot's public profile bio in **User Settings → Applications → your cinema
application → Bot** to include the control UI URL, for example:
`Watch together: https://cinema.example.com`. Bot profile edits require the
application owner's session; the service's bot token cannot update its own bio.

Only View Channel, Connect, and Stream are needed for the screen-share tracks.
Speak controls microphone audio, which this bot does not publish.

The bot's custom status shows "Watching <title>" only while a title is actively
playing. Pausing, buffering/loading, stopping, or a playback error clears it.
Updates are deduplicated, checked every two seconds, and refreshed on reconnect.
The public player URL belongs in the owner-managed profile bio, not the status.
