# a0-plugins
This repository is the community-maintained index of plugins surfaced in Agent Zero.

Submit a PR here to make your plugin visible to other Agent Zero users.

## What goes in this repo

Each plugin submission is a single folder (unique plugin name) containing:

- **`plugin.yaml`**
- **Optional thumbnail image** (`.png`, `.jpeg`/`.jpg`, or `.webp`)
  - **Square aspect ratio**
  - **Max size: 20 KB**

This repository is an index only: `plugin.yaml` points to the plugin's own repository.

## Submitting a plugin (Pull Request)

Every PR is first automatically validated by CI. If it passes, it will then be reviewed by a human maintainer before merging.

If your PR keeps failing checks and has no activity for 7+ days, it may be automatically closed.

### Rules

- **One plugin per PR**
  - Your PR must add exactly **one** new top-level subfolder for your plugin.
- **Unique folder name**
  - Use a unique, stable folder name (recommended: short, lowercase, `kebab-case`).
- **Reserved names**
  - Folders starting with `_` are reserved for project/internal use (examples, templates, etc.) and are **not visible in Agent Zero**. Do not submit community plugins with a leading underscore.
- **Required metadata**
  - All required fields in `plugin.yaml` must be present and non-empty.
- **Optional metadata**
  - The only optional field is **`tags`**.

### Automated validation (CI)

PRs are automatically checked for:

- **Structure**
  - Exactly one plugin folder per PR under `plugins/<your-plugin-name>/`
  - No extra files (only `plugin.yaml` and an optional thumbnail)
- **`plugin.yaml` rules**
  - Only allowed fields: `title`, `description`, `github`, `tags`
  - Required fields: `title`, `description`, `github`
  - `title` max length: 50 characters
  - `description` max length: 500 characters
  - `github` must be a GitHub repository URL that exists and contains `plugin.yaml` at the repository root
  - `tags` (if present) must be a list of strings, up to 5
- **Thumbnail rules (optional)**
  - Must be named `thumbnail.<ext>`
  - Must be square and <= 20 KB
  - Allowed formats: `.png`, `.jpg`/`.jpeg`, `.webp`

### Folder structure

```text
plugins/<your-plugin-name>/
  plugin.yaml
  thumbnail.png|thumbnail.jpg|thumbnail.jpeg|thumbnail.webp   (optional)
```

### `plugin.yaml` format

See `plugins/example1/plugin.yaml` for the reference format.

Required fields:

- **`title`**: Human-readable plugin name
- **`description`**: One-sentence description
- **`github`**: URL of the plugin repository

Optional fields:

- **`tags`**: List of tags (recommended list: [`TAGS.md`](./TAGS.md), up to 5 tags)

Example:

```yaml
title: Example Plugin
description: Example plugin template to demonstrate the plugin system
github: https://github.com/agentzero/a0-plugin-example
tags:
  - example
  - template
```

## Recommended tags

Use tags from [`TAGS.md`](./TAGS.md) where possible (recommended: up to 5 tags):

- **[`TAGS.md`](./TAGS.md)**: Recommended tag list for this index

## Safety / abuse policy

By contributing to this repository, you agree that your submission must not contain malicious content.

If we detect malicious behavior (including but not limited to malware, credential theft, obfuscation intended to hide harmful behavior, or supply-chain attacks), the submission will be removed and **we will report it** to the relevant platforms and/or authorities. **Legal action may be taken if needed.**
