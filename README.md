# a0-plugins
This repository is the community-maintained index of plugins surfaced in Agent Zero.

Submit a PR here to make your plugin visible to other Agent Zero users.

## What goes in this repo

Each plugin submission is a single folder (unique plugin name) containing:

- **`index.yaml`**
- **Optional thumbnail image** (`.png`, `.jpeg`/`.jpg`, or `.webp`)
  - **Square aspect ratio**
  - **Max size: 20 KB**
- **Optional screenshots** in `index.yaml` under `screenshots`
  - Up to **5 screenshot URLs**
  - Must be **full URLs**
  - Allowed formats: `.png`, `.jpg`/`.jpeg`, `.webp`
  - URL must exist
  - **Max size: 2 MB per screenshot**

This repository is an index only: `index.yaml` points to the plugin's own repository.

## Submitting a plugin (Pull Request)

Every PR is first automatically validated by CI. If it passes, it will then be reviewed by a human maintainer before merging.

If your PR keeps failing checks and has no activity for 7+ days, it may be automatically closed.

### Rules

- **One plugin per PR**
  - Your PR must add exactly **one** new top-level subfolder for your plugin.
- **Unique folder name**
  - Use a unique, stable folder name with lowercase letters, numbers, and underscores only (regex: `^[a-z0-9_]+$`).
- **Reserved names**
  - Folders starting with `_` are reserved for project/internal use (examples, templates, etc.) and are **not visible in Agent Zero**. Do not submit community plugins with a leading underscore.
- **Required metadata**
  - All required fields in `index.yaml` must be present and non-empty.
- **Optional metadata**
  - The optional fields are **`tags`** and **`screenshots`**.

### Automated validation (CI)

PRs are automatically checked for:

- **Structure**
  - Exactly one plugin folder per PR under `plugins/<your-plugin-name>/`
  - No extra files (only `index.yaml` and an optional thumbnail)
- **`index.yaml` rules**
  - Only allowed fields: `title`, `description`, `github`, `tags`, `screenshots`
  - Required fields: `title`, `description`, `github`
  - `index.yaml` max total length: 2000 characters
  - `title` max length: 50 characters
  - `description` max length: 500 characters
  - `github` must be a GitHub repository URL that exists and contains `plugin.yaml` at the repository root
  - The plugin folder name in this index (for example `plugins/my_plugin/`) must exactly match the `name` field in the remote repository's root `plugin.yaml`
  - `tags` (if present) must be a list of strings, up to 5
  - `screenshots` (if present) must be a list of full image URLs, up to 5
- **Thumbnail rules (optional)**
  - Must be named `thumbnail.<ext>`
  - Must be square and <= 20 KB
  - Allowed formats: `.png`, `.jpg`/`.jpeg`, `.webp`
- **Screenshot rules (optional)**
  - Must be provided only via `index.yaml` field `screenshots`
  - Up to 5 URLs total
  - Must be full URLs
  - Allowed formats: `.png`, `.jpg`/`.jpeg`, `.webp`
  - Each URL must exist
  - Max size per file: 2 MB

### Folder structure

```text
plugins/<your_plugin_name>/
  index.yaml
  thumbnail.png|thumbnail.jpg|thumbnail.jpeg|thumbnail.webp   (optional)
```

The folder name under `plugins/` is authoritative in this index and must exactly match the `name` in your remote repository's root `plugin.yaml`.

### `index.yaml` format

See `plugins/_example1/index.yaml` for the reference format.

Required fields:

- **`title`**: Human-readable plugin name
- **`description`**: One-sentence description
- **`github`**: URL of the plugin repository (its root `plugin.yaml` must include a `name` field that exactly matches your folder name in this index: `plugins/<your_plugin_name>/`)

Optional fields:

- **`tags`**: List of tags (recommended list: [`TAGS.md`](./TAGS.md), up to 5 tags)
- **`screenshots`**: List of up to 5 full image URLs (`.png`, `.jpg`, `.jpeg`, `.webp`), each reachable and <= 2 MB

Screenshot URL tips:

- You can host screenshots in your plugin repository and reference them directly with raw URLs.
- Example raw GitHub URL format:
  - `https://raw.githubusercontent.com/<owner>/<repo>/<branch>/path/to/screenshot.png`
- You can also use any other stable public image URL, as long as it is reachable, uses an allowed extension, and stays within the size limit.

Example:

```yaml
title: Example Plugin
description: Example plugin template to demonstrate the plugin system
github: https://github.com/agentzero/a0-plugin-example
tags:
  - example
  - template
screenshots:
  - https://raw.githubusercontent.com/agentzero/a0-plugin-example/main/docs/main.png
  - https://raw.githubusercontent.com/agentzero/a0-plugin-example/main/docs/settings.webp
```

## Recommended tags

Use tags from [`TAGS.md`](./TAGS.md) where possible (recommended: up to 5 tags):

- **[`TAGS.md`](./TAGS.md)**: Recommended tag list for this index

## Safety / abuse policy

By contributing to this repository, you agree that your submission must not contain malicious content.

If we detect malicious behavior (including but not limited to malware, credential theft, obfuscation intended to hide harmful behavior, or supply-chain attacks), the submission will be removed and **we will report it** to the relevant platforms and/or authorities. **Legal action may be taken if needed.**
