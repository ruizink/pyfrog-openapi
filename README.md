# jfrog-openapi-toolkit

**Unofficial** toolkit for generating a combined OpenAPI spec for the JFrog Platform REST APIs.

> This is not an official JFrog product. The generated spec is assembled from JFrog's public documentation and may be incomplete or inaccurate.

---

## What it does

JFrog publishes dozens of individual OpenAPI specs (one per service) through their developer docs. This toolkit:

1. **Scrapes** the raw specs from the JFrog documentation site.
2. **Normalizes** each spec by absorbing the server URL path prefix into the individual API paths (so all services share a single `https://{jfrog_url}` base URL).
3. **Merges** the normalized specs into a single `jfrog-merged-api.json` file suitable for code generation, API explorers, or tooling like Postman.

---

## Requirements

### Python

- Python 3.10+
- [PyYAML](https://pypi.org/project/PyYAML/) (the only non-stdlib dependency)

```sh
pip install -r requirements.txt
```

### Node.js (for `merge` / `all` steps only)

The merge step uses [`openapi-merge-cli`](https://www.npmjs.com/package/openapi-merge-cli). It is invoked automatically via `npx --yes` so no manual install is required, as long as Node.js and npx are available.

```sh
node --version   # 16+ recommended
npx --version
```

Pass `--no-npx` to use a locally installed `openapi-merge-cli` binary instead.

---

## Usage

All functionality is exposed through a single script with subcommands.

### Run everything in one go

```sh
python jfrog_openapi.py all
```

This runs scrape → normalize → merge in sequence and writes the result to `jfrog-merged-api.json`.

### Run steps individually

```sh
# 1. Download raw specs from JFrog docs into jfrog-apis/
python jfrog_openapi.py scrape

# 2. Normalize server URL paths into .normalized_apis/
python jfrog_openapi.py normalize

# 3. Merge into a single jfrog-merged-api.json
python jfrog_openapi.py merge
```

### Options

#### `scrape`

| Option | Default | Description |
|---|---|---|
| `--output-dir DIR` | `jfrog-apis/` | Directory to write downloaded specs. |
| `--workers N` | `20` | Number of parallel download workers. |

#### `normalize`

| Option | Default | Description |
|---|---|---|
| `--input-dir DIR` | `jfrog-apis/` | Directory of raw specs to normalize. |
| `--output-dir DIR` | `.normalized_apis/` | Directory for normalized output. |

#### `merge`

| Option | Default | Description |
|---|---|---|
| `--input-dir DIR` | `.normalized_apis/` | Directory of normalized specs to merge. |
| `--output FILE` | `jfrog-merged-api.json` | Path for the merged output file. |
| `--keep-config` | off | Retain the generated `openapi-merge.json` config after merging. |
| `--no-npx` | off | Use a locally installed `openapi-merge-cli` instead of `npx`. |

#### `all`

Accepts all of the above options plus:

| Option | Default | Description |
|---|---|---|
| `--scraped-dir DIR` | `jfrog-apis/` | Intermediate directory for downloaded specs. |
| `--normalized-dir DIR` | `.normalized_apis/` | Intermediate directory for normalized specs. |

---

## Customising which APIs to include

Edit the `MERGE_INPUTS` list in `jfrog_openapi.py` to control which specs are merged and whether any paths should be excluded:

```python
MERGE_INPUTS: list[dict] = [
    {"file": "users-api.yaml"},
    {"file": "groups-api.yaml"},
    {"file": "repository-replication_openapi.yaml", "excludePaths": [
        "/artifactory/api/replications/{action}",
    ]},
]
```

Leave `MERGE_INPUTS` empty (`[]`) to auto-discover and merge all files in the input directory.

---
