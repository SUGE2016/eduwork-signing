# EduWork signing service

[中文](README.md)

Owner-controlled macOS signing and notarization for EduWork. Upstream maintainers do not receive Apple credentials or signing-repository write access. Each release is processed without manual approval.

## Operation

The scheduled workflow checks published releases approximately every ten minutes, preferring an arm64 DMG over ZIP. It signs nested code, submits the package to Apple, resumes pending notarization, and validates on a separate runner without signing credentials. Only validated packages become public releases here. GitHub schedules can be delayed.

Only `ECNU/EduWork` and the owner's test fork `SUGE2016/EduWork` are allowed. Default discovery starts at this repository's creation date; historical versions require a manual `source_tag` or an explicit `SIGN_FROM` variable. The accepted bundle ID is `org.eduwork.eduwork.electron`. DMG inputs retain their Finder layout; ZIP inputs get a basic drag-to-install DMG.

This service consumes **published release assets**, not arbitrary build artifacts. Upstream must separately connect tag builds and signed-delivery collection. Windows packages are outside this Apple signing flow.

## Configuration

Create the `signing` environment, restrict deployment to `main`, and configure these environment secrets:

- `MACOS_CERTIFICATE_P12_BASE64`: base64-encoded Developer ID Application certificate **and private key**, exported as PKCS#12.
- `MACOS_CERTIFICATE_PASSWORD`: PKCS#12 password.
- `APPLE_ID`: developer Apple account.
- `APPLE_APP_SPECIFIC_PASSWORD`: notarization app-specific password.

Repository variables: `APPLE_TEAM_ID` (required), `SOURCE_REPO` (defaults to `ECNU/EduWork`), `SIGNING_ENABLED=true` (enable scheduling after a successful trial), and optional `SIGN_FROM` (ISO UTC time).

Alternatively run `python3 scripts/configure_secrets.py --p12 /absolute/path/certificate.p12` locally. Passwords are entered interactively and passed to `gh` through stdin, not command-line arguments or configuration files.

First dispatch with `inspect_only=true` and a published source tag. Configure credentials, test a real signing run, then enable scheduling. No secrets belong in code, logs, or artifacts. Hosting a private key in GitHub Secrets still entrusts it to GitHub. Never expose the signing job to PRs, arbitrary branches, or upstream-controlled scripts.

## Recovery and validation

Each source release maps to draft `signed-<source-release-id>`. State records hashes, source commit, asset ID and the Apple submission ID. Pending submissions resume on later runs. A unique submission name supports recovery when upload succeeds before saving the ID. An incomplete upload or ambiguous state requires owner inspection, not blind resubmission.

Rejected notarization retains logs and blocks new signing until the owner resolves it. Incomplete drafts or hash mismatches fail closed. A crash after uploading a stapled file but before saving its updated hash requires manual reconciliation. Disable `SIGNING_ENABLED` to pause scheduling.

Validation covers staple verification, Gatekeeper assessment of the Internet-marked DMG and its app, and main-UI startup using a local copy on an isolated runner. Interactive first-open confirmation and full model/Office/media workflows are not automated. Provenance uses GitHub release identity, tag commit, asset ID and SHA-256; build attestations, reproducible builds and a source security audit are not claimed.

## Upstream integration

Upstream first publishes a prerelease candidate with a macOS ZIP/DMG. This service publishes the signed result under `signed-<source-release-id>`. Upstream polls for that result using `scripts/fetch_signed.py` and `scripts/service.py` pinned to a reviewed commit:

```sh
python3 scripts/fetch_signed.py --upstream ECNU/EduWork --release-id "$SOURCE_RELEASE_ID" --output signed
```

The collector verifies source repository/release/tag/commit/asset and the validated output hash. Upstream uses its own token to upload the DMG and receipt to its release, then promotes it after all platforms are ready. Do not repack the signed DMG or overwrite original Sparkle ZIP/appcast signatures. Without this upstream integration, signed packages remain downloadable from this repository.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
