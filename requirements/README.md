# Dependency profiles

Each environment has one small, human-edited `.in` file and one generated, hash-verified `.lock` file:

| Profile | Used for |
| --- | --- |
| `runtime` | The application installed for normal use |
| `dev` | Runtime plus tests, coverage, browser testing, and auditing |
| `build` | Runtime plus desktop packaging |
| `tools` | Isolated lock generation and advisory auditing |

This separation keeps development and packaging tools out of the shipped runtime. Edit only the `.in` files; regenerate and audit locks with `scripts/update_dependencies.ps1` on Windows or `scripts/update_dependencies.sh` on macOS/Linux. The complete review process is documented in [`docs/DEPENDENCIES.md`](../docs/DEPENDENCIES.md).
