# Releasing

How the maintainer publishes a version. Release tags cannot be moved or deleted, so a mistake after tagging means a new patch version.

1. **Release pull request.** Raise the version in `pyproject.toml`, `uv.lock` (`uv lock`), the `secretops version` output in `src/secretops/cli.py`, README (status line and the bundle URL and names in Get started), `docs/INSTALL.md` (bundle URL, names and expected output) and the checkout example in `scripts/install.sh`. Merge it after CI passes.
2. **Tag.** On the merge commit, after CI on `main` passed:
   ```sh
   git tag -a vX.Y.Z -m "secretops vX.Y.Z" MERGE_COMMIT
   git push origin vX.Y.Z
   ```
3. **Release workflow.** The tag starts `.github/workflows/release.yml`. It checks that the tag matches the version, runs the tests, builds the wheel, the sdist and the installer bundle, attests them, and creates a **draft** release with install commands and SHA-256 sums.
   If the run fails because of GitHub rather than the code (check githubstatus.com), re-run the job for the same tag; a tag never needs to move.
4. **Publish the draft.** Replace the comment at the top of the notes with a short summary of the changes, then publish. Publishing starts `.github/workflows/publish.yml`.
5. **Approve PyPI.** `publish.yml` waits for approval in the `pypi` environment, checks the wheel and sdist digests against the release, and uploads them with trusted publishing.
6. **Check.** Run:
   ```sh
   sh scripts/check-release.sh X.Y.Z
   ```
   It waits until PyPI's index lists the version (often a few minutes after the upload), then checks the release files against `SHA256SUMS` and their attestations, that the bundle pins the release wheel, that PyPI has the same files with provenance from `publish.yml`, that a clean venv installs the version, and that the bundle installs a broker in a clean Ubuntu container, refuses a recipe with a wrong field and uninstalls. It changes nothing on GitHub or PyPI.

The installer bundle takes the program from PyPI, so it works only after step 5 and once PyPI's index lists the version; until then it stops with nothing changed.
