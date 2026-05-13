# Homebrew tap setup

This directory contains the formula for distributing Korpha via
Homebrew. **The formula doesn't live in this repo** — it lives in a
separate `homebrew-tap` repo per Homebrew's tap convention.

## One-time tap setup

1. Create a public repo named **exactly** `homebrew-tap` under
   `github.com/Korpha/`. Brew discovers tap repos by the
   `homebrew-` prefix.

2. Copy the formula in:

   ```bash
   git clone git@github.com:Korpha/homebrew-tap.git
   cd homebrew-tap
   mkdir -p Formula
   cp ../korpha_agent/distribution/homebrew/korpha.rb Formula/
   git add Formula/korpha.rb
   git commit -m "Initial Korpha formula"
   git push
   ```

3. After the first `v0.1.0` GitHub release, replace
   `REPLACE_WITH_RELEASE_TARBALL_SHA256` in `Formula/korpha.rb`
   with:

   ```bash
   curl -sL https://github.com/korpha/korpha/archive/refs/tags/v0.1.0.tar.gz | sha256sum
   ```

## Users install with

```bash
# Option 1 — tap explicitly, then install
brew tap Korpha/tap
brew install korpha

# Option 2 — one shot
brew install Korpha/tap/korpha

# Or install from main branch (for latest unreleased changes)
brew install --HEAD Korpha/tap/korpha
```

## Updating the formula on each release

Two paths:

**Manual** — bump version + sha256 in `Formula/korpha.rb`, commit,
push.

**Automated** — add a GitHub Actions workflow in `homebrew-tap` that
listens for `release.published` events on the main `korpha` repo
(via repository_dispatch) and runs `brew bump-formula-pr`. Recipe
template at https://github.com/macauley/action-homebrew-bump-formula

## Testing locally before pushing

```bash
brew install --build-from-source ./Formula/korpha.rb
brew test korpha
brew uninstall korpha
```
