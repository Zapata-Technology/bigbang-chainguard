# Security report

| Mode | Images | NeuVector C/H | Open C/H | grype C/H | Trivy C/H | KEV | Signed | SBOM |
|---|---|---|---|---|---|---|---|---|
| standard | 40 | 1/46 | 0/10 | 0/10 | 0/0 | 0 | 40/40 | 40/40 |
| standard (optional, not deployed) | 84 | 20/216 | 72/367 | 68/342 | 28/23 | 0 | 84/84 | 84/84 |
| fips | 40 | 0/45 | 0/10 | 0/10 | 0/0 | 0 | 40/40 | 40/40 |
| fips (optional, not deployed) | 84 | 20/228 | 72/367 | 68/342 | 28/23 | 0 | 84/84 | 84/84 |

Scanned 2026-11-25 (rke2.yml run 45570618056). Open C/H: after Chainguard's advisories. The full report:
[security-report.pdf](security-report.pdf). Rebuilt weekly by rke2.yml; [testing.md](testing.md#neuvectors-scan-of-every-deployed-image) explains it.
