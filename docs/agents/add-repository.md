# Add a repository to the org

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Add a repository to the org
Add what the conversion needs, but check the plan first: per-image plans count images
against a quota per tier, and a `CATALOG` plan has no limit in its active tiers
(`python scripts/bb-org-repos.py --quota` shows each tier's room,
what still needs adding and which counted images Big Bang doesn't use). For each name,
standard and `-fips`:

```bash
chainctl images catalog --name <name> -o json          # its id and catalog_tier
chainctl images repos create <name> --parent <org> --tier <catalog_tier>
chainctl images repos update <name> --parent <org> --source <id>   # start syncing tags
```

Tags appear after the sync; `bb-convert.py` converts the image on its next run.
`scripts/bb-org-repos.py --apply [names]` does both steps for each name (default: what
`bb-convert.py --missing` lists). If the sync fails with `PermissionDenied ... does not
have an active entitlement`, the plan doesn't cover the image. Don't retry; report it for
a person (`chainctl images entitlements summary` shows the per-tier quotas). Only
Chainguard can add it.

