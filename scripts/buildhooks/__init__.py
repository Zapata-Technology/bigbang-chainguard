"""Hook modules for scripts/bb-build.py: the Python a build in builds/<name>/ needs.

builds/<name>/recipe.yaml names its hook (`hook: <module>`); several builds can share one
(the MinIO operator and its sidecar use minio.py). A hook module has

  generate(r, a) -> spec              required: what builds/<name>/build.yaml holds. `r` is
                                      the recipe (with its `name`), `a` bb-build.py's options
                                      (a.org, a.ref, a.version). bb-build.py writes the file.
  build_args(spec, variant, r) -> {}  optional: docker --build-arg values; without it the
                                      recipe's `build_args` templates are used
  smoke_test(image, variant, spec, r) optional: raises SystemExit on failure; without it the
                                      recipe's `smoke` script and checks are run

Constants (image names, Iron Bank projects, modules to raise, notes) live in the recipe, not
here, so a hook holds only what can't be said declaratively. Each module's docstring says
what its build does and which recipe keys it reads.
"""
