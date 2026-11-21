# Fluxer Cinema

A shared Jellyfin movie night in a Fluxer voice channel. Browse your library in a signed-in web player, build a shared queue, and watch the bot's video stream inside Fluxer.

This is a standalone service, not a Jellyfin plugin. One deployment serves one Fluxer instance, one community, and one active stream. Movies and TV episodes are supported. The queue, sign-in sessions, and playback position reset on restart.

**Status:** implemented and locally tested; live bot screen sharing, receiver synchronization, Intel/NVIDIA acceleration, and the 30-minute 1080p acceptance run still require validation on your servers. The included compatibility probe is the first deployment gate. GitHub Actions builds, smoke-tests, and publishes the container.

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
- Media stays on the private Jellyfin connection. Compatible titles play directly by default. Jellyfin prepares media when subtitles, HDR conversion, or format compatibility require it; compatible video may be copied during remuxing/audio conversion. The bot decodes the result and publishes H.264 video with stereo audio to Fluxer, capped at 1920×1080 and 30 fps while preserving lower frame rates and aspect ratio.

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

`JELLYFIN_FORCE_TRANSCODE` defaults to `false`, including when omitted. Compatible titles use the original file and avoid a Jellyfin video encode. Remuxing/audio conversion can copy compatible H.264 video. Selected subtitles still require burn-in and HDR requires SDR conversion; both disable video stream copying. If Jellyfin cannot provide a required prepared stream, playback fails instead of opening an unsuitable source directly.

Set `JELLYFIN_FORCE_TRANSCODE=true` to always request H.264 up to 1080p/30 at `VIDEO_BITRATE`. This is useful when directly decoding high-resolution HEVC/AV1 overloads the bot. It adds server work and another lossy encode; configure Jellyfin hardware acceleration separately. **Existing `.env` files and ConfigMaps with `true` keep their behavior:** change the setting to `false` and recreate/restart the bot to adopt direct playback.

Direct playback here avoids the **Jellyfin** transcode. The bot still decodes frames with PyAV and encodes them for Fluxer with LiveKit; this is not encoded-packet passthrough. GPU encoding does not accelerate PyAV decoding or scaling.

## HTTP on a trusted LAN

Set `PUBLIC_ORIGIN=http://192.168.1.50:8081` and `ALLOW_INSECURE_LOCAL=true` to use a private LAN IP over HTTP. Register the exact `http://192.168.1.50:8081/auth/callback` redirect URI in Fluxer and open the player using that IP and port. Replace the example IP with your server's address.

The existing opt-in permits localhost/loopback, IPv4 private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), and IPv6 unique-local addresses (`fc00::/7`). HTTP LAN hostnames and public IPs remain disallowed. HTTPS remains the default; HTTP sign-in/session traffic is unencrypted, so use only on a trusted LAN.

For Compose, replace the loopback-only port mapping with `"192.168.1.50:8081:8080"` to expose the service on that host LAN address. Recreate the container after changing its environment.

## Docker Compose

Run the published image:

```sh
cp .env.example .env
# Fill in .env before starting.
docker compose pull
docker compose up -d
```

The example joins an **existing** Docker network named `jellyfin`. Set `JELLYFIN_NETWORK` to the network your Jellyfin container uses. Jellyfin's network alias should match the host in `JELLYFIN_URL`, normally `http://jellyfin:8096`. Both containers remain on the same host; no media directory needs to be shared with the bot.

The default image is `ghcr.io/jgerega107/cinemabot:main`. Prefer a full `sha-COMMIT` tag or digest for a repeatable deployment: set `CINEMA_IMAGE` in `.env`, then run `docker compose pull` and `docker compose up -d`. Authenticate to `ghcr.io` if using a private fork/package.

To build locally instead, run `docker build -t cinema:local .`, set `CINEMA_IMAGE=cinema:local` in `.env`, and run `docker compose up -d --pull never`.

The remaining Compose options support the worker's lifecycle and confinement:

- `read_only` keeps the image filesystem immutable; the bounded `/tmp` tmpfs provides writable scratch space for the worker, not Jellyfin's transcode storage. A writable directory is required even with Jellyfin transcoding. The 512 MiB value is a limit, not a reserved allocation.
- `init` handles signal forwarding and orphaned processes; the 30-second stop grace period allows application and worker cleanup.
- `cap_drop` and `no-new-privileges` retain container hardening.
- The shared Jellyfin network provides private service-name access, and the loopback port is for the host's HTTPS reverse proxy.

CPU and memory limits are optional host-specific tuning. The example leaves them unset; add `cpus` and `mem_limit` after measuring usage. The bot still decodes and encodes the Fluxer stream.

For Jellyfin installed directly on the Docker host, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the service on Linux and use `JELLYFIN_URL=http://host.docker.internal:8096`. Jellyfin must listen on an address reachable from the bridge network; `127.0.0.1` inside this container refers to the bot itself. Docker Desktop already provides the host name. The example publishes the UI only on host loopback for an HTTPS reverse proxy.

### GPU encoding

`VIDEO_ENCODER` selects the LiveKit H.264 encoder preference:

| Value | Preference |
| --- | --- |
| `auto` (default) | SDK selection, using available hardware with software fallback |
| `software` | Software reference run |
| `hardware` | Any available hardware encoder |
| `vaapi` | Intel VAAPI |
| `nvenc` | NVIDIA NVENC |

These are SDK preferences, not a strict hardware requirement. Unavailable backends may fall back, and driver/encoder initialization can still fail. Check the actual encoder using the probe below. H.264, a single video publication, and existing pause/seek behavior are retained.

For Intel, use a supported GPU with a working host `i915`/`xe` driver and H.264 encoding support. The image includes Debian's full-feature `intel-media-va-driver-non-free` (iHD) driver. Set the render-device group for the non-root container:

```sh
export RENDER_GID=$(stat -c '%g' /dev/dri/renderD128)
docker compose -f compose.yaml -f deploy/compose.intel.yaml up -d
```

For NVIDIA, install a compatible host NVIDIA driver and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), configure Docker with `nvidia-ctk runtime configure --runtime=docker`, and restart Docker as directed by the toolkit. The GPU must support NVENC H.264; CUDA availability alone is insufficient. Host-matched CUDA/NVENC libraries are injected by the toolkit, not installed as a second kernel driver in the image.

```sh
docker compose -f compose.yaml -f deploy/compose.nvidia.yaml up -d
```

The NVIDIA overlay reserves one GPU and enables `compute,video,utility` driver capabilities. To select a specific GPU, replace `count: 1` with `device_ids: ["YOUR-GPU-UUID"]` (do not use both). Use one vendor overlay at a time. Both retain non-root execution, the read-only filesystem, and worker confinement; neither requires a privileged container.

The probe includes `encoder_requested`, `encoder_implementation`, and `encoder_power_efficient` in progress samples when the SDK reports them. Missing fields mean **unknown**, not successful acceleration. Compare `VIDEO_ENCODER=software` against the vendor overlay on the same clip and monitor the host with `intel_gpu_top` or `nvidia-smi dmon -s u`; confirm video-encode activity as well as reduced bot CPU. Device visibility and requested preference alone are not validation. See [LiveKit hardware encoder support](https://docs.livekit.io/robotics/media/video/encoders/) and [release validation](docs/VALIDATION.md).

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

The base example uses no GPU allocation. Choose one optional Kustomize overlay for bot encoding:

```sh
kubectl kustomize deploy/kubernetes-intel
kubectl apply -k deploy/kubernetes-intel
# Or, on an NVIDIA cluster:
kubectl kustomize deploy/kubernetes-nvidia
kubectl apply -k deploy/kubernetes-nvidia
```

Intel requires the [Intel GPU device plugin](https://github.com/intel/intel-device-plugins-for-kubernetes/tree/main/cmd/gpu_plugin) and nodes advertising `gpu.intel.com/i915`. For `xe` nodes, change both resource keys to `gpu.intel.com/xe`. Edit `deploy/kubernetes-intel/gpu.yaml` to replace the sample supplemental group `109` with the render-device GID on eligible nodes; use consistent GIDs or constrain scheduling to matching nodes. The device plugin supplies device access; no hostPath or privileged pod is needed.

NVIDIA requires host drivers, Container Toolkit, and the [NVIDIA device plugin](https://github.com/NVIDIA/k8s-device-plugin) or GPU Operator, advertising `nvidia.com/gpu`. The example uses `runtimeClassName: nvidia`; configure that RuntimeClass or remove the field if NVIDIA is the default runtime. It requests one GPU and enables CUDA/NVENC driver-library injection. Select a GPU with NVENC H.264 support.

Both overlays inherit the base CPU/memory limits and hardening. GPU resources are allocated to the bot separately from Jellyfin. Configure Jellyfin hardware acceleration independently when it needs to transcode. Validate with the probe on the actual GPU node; rendering YAML does not establish working hardware encoding.

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

For local UI development, set `PUBLIC_ORIGIN=http://localhost:5173` and `ALLOW_INSECURE_LOCAL=true`, and register `http://localhost:5173/auth/callback` on the development Fluxer application. Run `.venv/bin/uvicorn cinema.app:create_app --factory --host 127.0.0.1 --port 8080 --no-access-log --no-proxy-headers` from the project root, then `npm run dev` in `web`. Open the UI at `localhost:5173`. Vite proxies `/api` and `/auth` to the backend. The insecure option is for localhost development or explicitly trusted LAN access only.

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
