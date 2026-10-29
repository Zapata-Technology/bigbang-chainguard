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

## Current coverage (Big Bang 3.35.1)

- About 100 of Big Bang's images convert. The rest stay on Iron Bank:
  - vendor products (Twistlock, Anchore Enterprise, Fortify, SonarQube)
  - the GitLab CNG set, which must move as one release
  - a few with no Chainguard image yet
- Big Bang's own images (`big-bang/base`, `grafana-plugins`, `devops-tester`) are rebuilt
  from Iron Bank's own recipes, FROM Chainguard images.
- Verified by rendering every package, in standard and FIPS mode.
- **Not yet verified on a full cluster deployment.** That is the next milestone, and help
  is welcome.

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
