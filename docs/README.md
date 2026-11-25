# The docs, by audience

- **Site operator:** installs, upgrades and runs Big Bang on a cluster with `bb-install.py`.
- **Org admin:** owns the Chainguard org and the copy of this repository a site installs from
  (the plan, the images, the build registry, CI).
- **Maintainer:** changes this repository: the mapping, the scripts, the tests.

| Doc | For | What's in it |
|---|---|---|
| [../README.md](../README.md) | site operator, org admin | What this is, installing in three commands, the security case, known gaps |
| [quickstart.md](quickstart.md) | site operator | Install from a pinned, signed release; check it; upgrade |
| [install.md](install.md) | site operator | Every installer step and option: logins, TLS, signatures, upgrades and backups, disconnected sites, GitOps, doing it by hand, package notes |
| [troubleshooting.md](troubleshooting.md) | site operator | By symptom: ImagePullBackOff, signature refusals, repo1 timeouts, inotify, expired tokens, FIPS Keycloak, GitLab locks |
| [upgrades/](upgrades/) | site operator, org admin | Per Big Bang release: the package charts and Iron Bank images that change (generated) |
| [images.md](images.md) | site operator, org admin | Every image each package deploys and what replaces it, standard and FIPS (generated) |
| [version-differences.md](version-differences.md) | site operator, org admin | Each image's version on Iron Bank and on Chainguard (generated) |
| [required-images.md](required-images.md) | org admin | The Chainguard repositories your plan must cover (generated) |
| [your-org.md](your-org.md) | org admin | Running it on your own Chainguard org: plan, settings, CI for your copy |
| [big-bang-on-chainguard-briefing.pdf](big-bang-on-chainguard-briefing.pdf) | org admin | The case for Big Bang on Chainguard, in slides |
| [security-report.pdf](security-report.pdf) ([counts](security-report.md)) | org admin, site operator | The weekly security report: every image's CVEs after Chainguard's advisories, KEV, EPSS, signatures and SBOMs (generated) |
| [for-big-bang.md](for-big-bang.md) | org admin, Big Bang team | Coverage, and how this could fit in the Big Bang ecosystem |
| [../SECURITY.md](../SECURITY.md) | org admin, site operator | Reporting vulnerabilities; the controls in place; credentials in the cluster |
| [architecture.md](architecture.md) | maintainer, org admin | Diagrams of the pieces and how a change flows to a cluster |
| [how-it-works.md](how-it-works.md) | maintainer | The conversion, version rules, the images built here, releases, the escape hatches |
| [testing.md](testing.md) | maintainer | Test layers, the results table (generated), what the runtime tests found |
| [test-results.json](test-results.json) | maintainer | Each kind test's description and newest result (the table's source) |
| [adding-a-package.md](adding-a-package.md) | maintainer | Adding or fixing an image mapping |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | maintainer | How to send a change |
| [../AGENTS.md](../AGENTS.md) | maintainer (and AI agents) | Source vs generated, the rules, the commands, the loop |
| [agents/](agents/) | maintainer (and AI agents) | One playbook per job, and the traps already hit |
