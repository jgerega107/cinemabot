# Release validation

Live validation is pending by user choice. No server credentials, test channel, or local Docker daemon were available during implementation; container build and smoke verification run in GitHub Actions. Do not describe a release as verified for production screen sharing until the following evidence is collected.

## Automated checks

- Python authorization, OAuth PKCE/state/replay, session expiry, membership removal, custom endpoint discovery, voice grants, Jellyfin account restrictions and negotiation, queue concurrency, stale event rejection, worker failure/retry, disconnect handling, and real media decode/seek/pause tests.
- Browser tests at desktop and mobile sizes for sign-in, browsing/search, title details, starting playback, shared controls, subtitles, and disabled controls outside voice.
- TypeScript build, Python lint, Compose rendering, Kubernetes base/Intel rendering, and availability of pinned Linux amd64 wheels.
- CI additionally runs the actual container build and health smoke test. These do not replace a live compatibility test.

## Live matrix

Record Fluxer version, discovered endpoints (without credentials), Jellyfin version, image digest, CPU/GPU, driver version, and viewer client/version for each run.

| Scenario | Required observation | Status |
| --- | --- | --- |
| Hosted Fluxer, normal bot account | Watchable screen-share and stereo audio | Pending |
| Self-hosted Fluxer, separate API/Gateway/web domains | Discovery, OAuth, voice join and stream all work | Pending |
| Encrypted voice grant | Viewers decrypt both audio and video | Pending |
| 1080p/30, 30-minute reference clip | Stable output; receiver A/V offset within 100 ms | Pending |
| Lower-rate, VFR, wide/portrait video | Natural speed; aspect ratio preserved | Pending |
| H.264 and HEVC sources | Correct picture and audio, including seeking | Pending |
| Alternate audio; text and image subtitles | Selected track heard; captions burned in for everyone | Pending |
| HDR input | SDR output without washed-out highlights/colors | Pending |
| Pause/seek/track change | No old buffered audio; correct shared position | Pending |
| Viewer joins during playback and pause | Picture initializes; audio subscription behaves correctly | Pending |
| Gateway/media outage, bot kick/move | Playback pauses/stops; controls require fresh membership; explicit resume | Pending |
| Empty voice channel | Disconnects and clears after 60 seconds | Pending |
| Intel Arc A380 + software reference host | Record utilization and achieved quality separately | Pending |

Use a test clip with a visible frame counter and synchronized flashes/clicks. Measure **receiver** offset from a recording or capture with a known timebase; publisher timestamps alone are insufficient. Record received resolution, frame rate, WebRTC dropped frames, network statistics, CPU, memory, and (when enabled) GPU utilization over the run. The publisher probe's `av_drift_ms` is a diagnostic of queued source frames, not a receiver measurement.

Run `python -m cinema.probe --help` for the sender. Store its sanitized JSON report alongside a human report of the receiver observations. Never include grants, tokens, signed URLs, or raw logs containing credentials.

If the normal bot cannot publish video/audio or advertise a watchable screen-share, stop the release gate and record the exact Fluxer version and sanitized failure. Do not switch to a user account token to bypass a bot restriction. Optional stream thumbnails/region APIs are not part of this implementation.

## Packaging checks before publication

1. Build and run the Linux amd64 image on a Docker host. Confirm non-root operation, read-only root filesystem, bounded `/tmp`, clean shutdown, `/healthz`, and `/readyz` transitions.
2. Run the Compose example on the Jellyfin host; verify it uses the private Docker/host connection.
3. Deploy the Kubernetes example as a separate Deployment; verify private Service access and SSE through the HTTPS ingress.
4. Test the Intel overlay on a host with a compatible render group, driver, and device allocation. Confirm actual encoder activity; device presence is insufficient.
5. Only then record a verified image digest and the compatibility matrix in the release notes.
