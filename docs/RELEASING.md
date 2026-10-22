# Releasing

How the maintainer publishes a version. Release tags cannot be moved or deleted, so a mistake after tagging means a new patch version.

1. **Release pull request.** Raise the version in `pyproject.toml`, `uv.lock` (`uv lock`), the `secretops version` output in `src/secretops/cli.py`, README (status line and install commands), `docs/INSTALL.md` and the examples in `scripts/install.sh`. Merge it after CI passes.
2. **Tag.** On the merge commit, after CI on `main` passed:
   ```sh
   git tag -a vX.Y.Z -m "secretops vX.Y.Z" MERGE_COMMIT
   git push origin vX.Y.Z
   ```
3. **Release workflow.** The tag starts `.github/workflows/release.yml`. It checks that the tag matches the version, runs the tests, builds the wheel, the sdist and the installer bundle, attests them, and creates a **draft** release with install commands and SHA-256 sums.
4. **Publish the draft.** Replace the comment at the top of the notes with a short summary of the changes, then publish. Publishing starts `.github/workflows/publish.yml`.
5. **Approve PyPI.** `publish.yml` waits for approval in the `pypi` environment, checks the wheel and sdist digests against the release, and uploads them with trusted publishing.
6. **Check.** The PyPI hashes equal the release; `gh attestation verify` passes for the bundle; a clean venv with `pip install secretops==X.Y.Z` prints the version; the bundle installs or upgrades a test broker.

The installer bundle takes the program from PyPI, so it works only after step 5.
