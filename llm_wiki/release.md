# Release process

Runbook for preparing and pushing a new release. Follow it in order and
never skip the confirmation step.

## Versioning

Tags are `vX.Y.Z` with an optional `b` beta suffix.

| Change                                    | Bump           | Tag example |
|-------------------------------------------|----------------|-------------|
| Breaking (config format, peer config regen required, client app upgrade) | major  `X.0.0` | `v5.0.0` |
| New functionality, backward compatible    | feature `X.Y.0` | `v4.10.0`  |
| Bug fixes only                            | fix     `X.Y.Z` | `v5.0.1`   |
| Preview / testing build                   | append `b`      | `v5.0.0b`  |

Tags ending in `b` are beta releases and are published on GitHub as
**pre-releases**. Beta images are pushed to GHCR **without** the `latest`
tag — `latest` always points at the newest stable release.

### Stable releases roll up beta changes

The `## [X.Y.Z]` section of a **stable** release must contain **every change
since the previous stable release** — everything shipped in the intermediate
beta builds included. Users upgrade stable-to-stable and must see the full
picture (especially breaking changes) without reading beta sections. Beta
sections remain in `CHANGELOG.md` as history, but the stable section must be
complete and self-contained: never write only the delta since the previous
beta.

## Sync before committing

Before every commit, make sure local work sits on top of the current
`origin` state — this avoids divergence between the local repo and origin:

```bash
git stash && git pull --rebase && git stash pop
git add -A
git commit -m "..."
```

If `git stash pop` reports conflicts, resolve them, then continue.

## Pre-flight checklist

Before tagging, verify:

1. `CHANGELOG.md` has a section for the new version at the top, keepachangelog
   format (`## [X.Y.Z] - YYYY-MM-DD`), with today's date and accurate content.
   The CI release job takes the release description from this section.
   For a **stable** release this section must roll up all changes since the
   last stable release (see "Stable releases roll up beta changes" above);
   a delta-only section since the previous beta is not acceptable.
2. `README.md` is updated for every user-visible change.
3. Breaking changes require a major bump and explicit upgrade notes in
   `README.md`.
4. `python3 -m py_compile` passes on every touched Python file.
5. The working tree is committed; the tag will point at the current `HEAD`.

## Confirmation (mandatory, never skip)

**Never push a tag without explicit user approval.** First output ALL release
information:

```
Version:        5.0.0b
Tag:            v5.0.0b (annotated, on commit <sha>)
Pre-release:    yes (b suffix)
GHCR tags:      v5.0.0b (beta — no 'latest' tag)
Release notes:  the CHANGELOG section below - it becomes the GitHub release body:

  ## [5.0.0b] - 2026-09-04
  ... (section content)

CI will:
  1. build and push ghcr.io/illmouse/wgeasy-tg-bot:v5.0.0b (no :latest for beta)
  2. create GitHub release v5.0.0b from the changelog section (pre-release)
```

Ask the user to confirm. Only after an explicit approval perform the tag push.
If the user requests changes, update `CHANGELOG.md` and repeat.

## Pushing the tag

```bash
git fetch origin
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

Push the specific tag only. Never use `--force` and never blanket-push with
`--tags`.

## What CI does on tag push

`.github/workflows/build.yaml`:

1. **build** — buildah builds the image and pushes
   `ghcr.io/illmouse/wgeasy-tg-bot:{tag,latest}`. Exception: beta tags
   (`vX.Y.Zb`) are pushed with **only** their version tag — they never
   receive `latest`.
2. **release** (runs after build, only for `vX.Y.Z` / `vX.Y.Zb` tags) —
   extracts the `## [X.Y.Z]` section from `CHANGELOG.md` and creates the GitHub
   release with it as the description. `b`-suffixed tags are marked
   pre-release. If a release for the tag already exists, CI leaves it
   untouched.

## Never replace an existing successful tag

- Before re-creating a tag, check the state of the existing one:

   ```bash
   curl -s https://github.com/illmouse/wgeasy-tg-bot/releases.atom | grep -o 'tag/[^<]*'   # does a release already exist?
   curl -s https://api.github.com/repos/illmouse/wgeasy-tg-bot/actions/runs | grep -E '"head_branch"|"conclusion"'   # did the tag build pass?
   ```

- If the tag's build succeeded (release exists) — **do not delete or re-push
  the tag.** Cut a NEW version instead (bump the patch, or the next
  feature/fix version).
- Deleting and re-pushing a tag is allowed **only** when the previous CI build
  for that tag failed.
- The CI release job enforces the same rule: it skips creation if a release
  already exists and never overwrites one.

## Failure handling

- Build fails after the tag push: fix on a branch until the build is green,
  then either re-create the tag (allowed — previous build failed) or cut a new
  version.
- Release job fails with "No '## [X.Y.Z]' section found in CHANGELOG.md": add
  the missing section and re-run the failed job from the Actions UI.