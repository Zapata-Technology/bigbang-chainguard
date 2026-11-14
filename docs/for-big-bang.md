# For the Big Bang team

This repo lets a Big Bang user run Big Bang with Chainguard images from their own
Chainguard org, with FIPS as an option, without forking Big Bang. Zapata Technology
built it and offers it to the Big Bang community under Apache-2.0.

## What it is

- **A generator, not a fork.** It renders Big Bang and every package, finds each Iron Bank
  image and the value path that sets it, and maps it to the Chainguard equivalent. It then
  writes plain values overlays for the upstream chart and checks them by rendering again.
  Nothing in Big Bang changes.
- **Per org.** Each user points it at their own Chainguard org. Tags come from that org,
  so the overlays carry versions the org actually has.
- **Kept current.** A nightly job tracks new Big Bang releases, new Chainguard patches, and
  changes to Iron Bank's recipes for Big Bang's own images, and opens a pull request.
- **Honest about gaps.** `docs/images.md` lists every image that stays on Iron Bank, with
  the reason, and every image that runs a different version than Big Bang tested.

## Current coverage (Big Bang 4.1.0)

- Every image Big Bang 4.1.0 deploys is converted or listed with a reason
  (`docs/images.md`, `docs/version-differences.md`). The version table counts each Iron
  Bank image and tag. The image map counts image names, so an image pinned at two tags
  is one name there and two rows in the version table. What stays on Iron Bank includes
  vendor products (Twistlock, Anchore Enterprise, Fortify), parts of the GitLab CNG set,
  Red Hat UBI bases its scripts expect, and the MinIO server (Zapata's org file).
- Images Chainguard doesn't publish are built here FROM Chainguard images. The list is
  in [how-it-works.md](how-it-works.md). The installer image is in [install.md](install.md).
- Rendering checks every package in standard and FIPS mode on every pull request. Kind
  runs the tests the change reaches (`compat/*.yaml`, each with a functional check). A
  digest-only change runs the smoke set:
  <!-- smoke-tests -->
  `kyverno-enforce`, `metrics-server`, `object-storage`, `keycloak`, `kiali`, `velero`
  <!-- /smoke-tests -->
  (Kyverno, metrics-server, Tempo, Keycloak, Kiali, Velero). GitLab and GitLab Runner run weekly. `results-gate` fails while this Big
  Bang version has no recorded standard-mode matrix. The full recorded matrix is still
  Big Bang 4.0.0 (2026-11-08). On 4.1.0, the Keycloak and Mattermost release-upgrade and
  migration tests, and `bb-install.py upgrade --to`, passed on 2026-11-14
  ([testing.md](testing.md)). Upgrading from the newest 3.35.1 release to Big Bang 4 is
  one of the kind tests. A weekly run puts the FIPS overlay on a kernel in FIPS mode.
  The weekly and manual RKE2 runs also install the default packages with `--fips`.
- **Not yet run on a production-sized, multi-node cluster.** That is the next milestone,
  and help is welcome.

## How it could live in the Big Bang ecosystem

These are options, roughly from least to most involvement for the Big Bang team:

1. **Link to it.** Big Bang docs point Chainguard users to this repo. It stays here and
   community maintained.
2. **Community project.** Host the repo under Big Bang's community area on repo1, with
   Zapata and other users as maintainers. Its CI needs only a Chainguard pull identity and
   repo1 access.
3. **A Big Bang option.** Big Bang publishes the generated overlays per release, the way
   it publishes the chart. Each user would still need their own Chainguard org for pulls,
   so it would stay a generator plus a documented process.

Whichever option you choose, two things would help the project:

- A stable way to find images. Discovery today renders every package; an image list
  published per Big Bang release would do the job.
- Keeping image settings as values, so new images aren't hardcoded in templates.

## Contact

Open an issue in this repository, or contact Zapata Technology.
