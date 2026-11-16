# Container publishing

All three bots use the same release sequence: tests, one amd64 image build,
runtime checks and Trivy scan, Cosign provenance/SBOM attestations, keyless Cosign
signature and verification, then tag promotion. Pull requests build and scan
locally without registry login, publishing, attestation, or signing.

Images are published to ghcr.io/jgerega107/cinemabot. The image name follows
the repository name. sha-<full commit> is immutable; reruns reuse and recheck
that digest. latest and main follow the default branch.
Musicbot and utilitybot retain fluxer as a compatibility alias.
Version tags are derived from v* Git tags.
Release commits must belong to the default branch. Failed candidates may remain
under build-<run>-<attempt>, but are never promoted. Deploy only a verified
digest or a promoted tag, never a build tag.

BuildKit emits an SBOM and maximum build provenance in the signed image index.
Cosign also signs the original BuildKit provenance and SPDX SBOM as attestations
in GHCR. Reruns reuse those original predicates rather than claiming to rebuild.
This works with user-owned private repositories. Cosign uses
GitHub OIDC and Sigstore; no signing key or new repository secret is needed.
Actions are pinned to reviewed commits and maintained by Dependabot. The Cosign
CLI version is explicitly pinned and should be updated alongside the installer
after checking compatibility.

The scanner reports HIGH/CRITICAL findings and blocks CRITICAL findings and
CVE-2026-111497, preserving the existing musicbot/utilitybot policy.
Runtime checks use no network, a read-only root filesystem, and a non-root user.
Signing confirms origin and integrity; it does not prove an image has
no vulnerabilities or enforce admission in Kubernetes.

Verify a default-branch image (replace DIGEST with the published sha256 value):

    cosign verify ghcr.io/jgerega107/cinemabot@sha256:DIGEST --certificate-identity https://github.com/jgerega107/cinemabot/.github/workflows/ci.yml@refs/heads/main --certificate-oidc-issuer https://token.actions.githubusercontent.com

For a version-tag run, use @refs/tags/vVERSION in the certificate identity.
Repository or workflow renames change that identity. GitHub artifact attestations
are not used: that service does not support user-owned private repositories.
Verify the signed provenance/SBOM with cosign verify-attestation, using the same
certificate identity and issuer and --type slsaprovenance or --type spdxjson.

References:
- https://docs.docker.com/learn/content/build-a-trusted-delivery-pipeline/
- https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations
- https://docs.sigstore.dev/cosign/signing/signing_with_containers/

Cinemabot uses a digest-pinned Debian Trixie Python base and applies available OS
updates during builds. The previous Bookworm SQLite package failed the common
CRITICAL vulnerability gate (CVE-2025-7458).
