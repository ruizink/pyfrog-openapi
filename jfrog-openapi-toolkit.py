#!/usr/bin/env python3
"""
JFrog OpenAPI toolkit — scrape, normalize, and merge JFrog Platform API specs.

Subcommands:
  scrape     Crawl JFrog docs and download raw OpenAPI specs.
  normalize  Absorb server URL paths into API path keys.
  merge      Merge normalized specs into a single JSON file (requires openapi-merge-cli).
  all        Run scrape → normalize → merge in sequence.

Python requirements:
  pip install pyyaml

Runtime requirement for 'merge' / 'all':
  Node.js / npx with openapi-merge-cli (fetched on demand via npx --yes by default).
"""

import argparse
import concurrent.futures
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

try:
    import yaml  # type: ignore[import-untyped]
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
USER_AGENT = "Mozilla/5.0 (compatible; jfrog-openapi-toolkit/1.0)"

# ── scrape configuration ───────────────────────────────────────────────────────

SCRAPE_URL_TEMPLATE = (
    "https://docs.jfrog.com/{api_name}/api-next/v2/branches/1.0"
    "/reference/{entry}?dereference=true&reduce=false"
)
SCRAPE_SIDEBAR_TEMPLATE = (
    "https://docs.jfrog.com/{api_name}/api-next/v2/branches/1.0"
    "/sidebar?page_type=reference"
)
SCRAPE_PAGES = [
    "artifactory",
    "security",
    "governance",
    "integrations",
    "projects",
    "administration",
]
SCRAPE_MAX_WORKERS = 20
SCRAPE_DEFAULT_OUTPUT_DIR = os.path.join(PROJECT_DIR, "jfrog-apis")
# Basenames to always remove after crawling (e.g. duplicates or unwanted specs).
SCRAPE_EXCLUDE_FILES: list[str] = []

# ── normalize configuration ────────────────────────────────────────────────────

NORMALIZE_DEFAULT_INPUT_DIR = SCRAPE_DEFAULT_OUTPUT_DIR
NORMALIZE_DEFAULT_OUTPUT_DIR = os.path.join(PROJECT_DIR, ".normalized_apis")

# Matches template URLs like {protocol}://{host}:{port}/path as well as standard ones.
_URL_RE = re.compile(r"^([^:]*://[^/]*)(/[^?#]*)?")

# ── merge configuration ────────────────────────────────────────────────────────

MERGE_DEFAULT_INPUT_DIR = NORMALIZE_DEFAULT_OUTPUT_DIR
MERGE_DEFAULT_OUTPUT = os.path.join(PROJECT_DIR, "jfrog-merged-api", "spec.json")
MERGE_TMP_DIR = os.path.join(PROJECT_DIR, ".merge-tmp")

MERGE_API_TITLE = "JFrog Platform API (Unofficial)"
MERGE_API_VERSION = "1.0.0"
MERGE_API_DESCRIPTION = (
    "**Unofficial** combined REST API spec for the JFrog Platform. "
    "This is not an official JFrog specification and may be incomplete or inaccurate."
)
MERGE_API_CONTACT_NAME = "ruizink"
MERGE_API_CONTACT_URL = "https://github.com/ruizink/jfrog-openapi-toolkit"

# Default and description for server URL template variables.
# Keys are variable names (e.g. "server_url"); values must have at least "default".
# Any variable not listed here falls back to {"default": "", "description": <name>}.
MERGE_SERVER_URL = "{server_url}"
MERGE_SERVER_VARIABLES: dict[str, dict] = {
    "server_url": {
        "default": "https://myserver.jfrog.io",
        "description": "Base URL of the JFrog Platform instance (e.g. https://myserver.jfrog.io)",
    },
}

# Each entry: {"file": "<basename>", "excludePaths": [<optional list>]}
# Leave empty to auto-discover all valid files in the input directory.
MERGE_INPUTS: list[dict] = [
    {"file": "access-tokens-api.yaml"},
    {"file": "artifactory-security_openapi.yaml"},
    {"file": "artifactory-system-api.yaml"},
    {"file": "artifacts-and-storage_openapi.yaml"},
    {"file": "federated-repository_openapi.yaml"},
    {"file": "federation-api.yaml"},
    {"file": "global-roles-api.yaml"},
    {"file": "groups-api.yaml"},
    {"file": "import-export_openapi.yaml"},
    {"file": "jfrog-entitlements-server-api.yaml"},
    {"file": "loggers_openapi.yaml"},
    {"file": "mission-control-api.yaml"},
    {"file": "permissions-api.yaml"},
    {"file": "plugins_openapi.yaml"},
    {"file": "projects-api.yaml"},
    {"file": "repository-replication_openapi.yaml", "excludePaths": ["/artifactory/api/replications/{action}"]},
    {"file": "respositories-openapi.yaml"},
    {"file": "retention-policy_openapi.yaml"},
    {"file": "searches-openapi.yaml"},
    {"file": "system-config-api.yaml"},
    {"file": "trashcan-openapi.yaml"},
    {"file": "users-api.yaml"},
    {"file": "xray-openapi-combined.06cc264.yaml"},
]

# ── scrape helpers ─────────────────────────────────────────────────────────────


def _fetch_json(url: str) -> "dict | list":
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    logger.debug("GET %s", url)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        data = resp.read().decode()
    logger.debug("Response %d bytes from %s", len(data), url)
    return json.loads(data)


def _fetch_json_dict(url: str) -> dict:
    result = _fetch_json(url)
    if not isinstance(result, dict):
        raise ValueError(f"Expected JSON object but got array from {url}")
    return result


def _fetch_json_list(url: str) -> list:
    result = _fetch_json(url)
    if not isinstance(result, list):
        raise ValueError(f"Expected JSON array but got object from {url}")
    return result


def _collect_slugs(nodes: list) -> list[str]:
    """Recursively collect endpoint slugs from sidebar JSON."""
    slugs: list[str] = []
    for node in nodes:
        if node.get("type") == "endpoint":
            slugs.append(node["slug"])
        slugs.extend(_collect_slugs(node.get("pages", [])))
    return slugs


def _fetch_page_slugs(page: str) -> list[str]:
    url = SCRAPE_SIDEBAR_TEMPLATE.format(api_name=page)
    logger.info("Fetching sidebar: %s", url)
    try:
        data = _fetch_json_list(url)
    except Exception as exc:
        logger.warning("Could not fetch sidebar for %s: %s", page, exc)
        return []
    slugs = _collect_slugs(data)
    logger.debug("Found %d endpoint slug(s) in sidebar for %s", len(slugs), page)
    return slugs


def _build_api_urls(pages: list[str]) -> list[str]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(pages)) as executor:
        page_slugs: dict[str, list[str]] = dict(
            zip(pages, executor.map(_fetch_page_slugs, pages))
        )
    seen: set[str] = set()
    urls: list[str] = []
    for page in pages:
        count = 0
        for slug in page_slugs[page]:
            api_url = SCRAPE_URL_TEMPLATE.format(api_name=page, entry=slug)
            if api_url not in seen:
                seen.add(api_url)
                urls.append(api_url)
                count += 1
            else:
                logger.debug("Duplicate URL skipped: %s", api_url)
        if count:
            logger.info("  %s -> %d entries", page, count)
        else:
            logger.warning("  No sidebar entries found for %s", page)
    return urls


def _write_schema(file_path: str, schema: dict) -> None:
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".yaml", ".yml"):
        if HAS_YAML:
            content = yaml.dump(schema, allow_unicode=True, sort_keys=False)
        else:
            logger.warning("PyYAML not installed — writing JSON to %s", file_path)
            content = json.dumps(schema, indent=2)
    else:
        content = json.dumps(schema, indent=2)
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    logger.debug("Written: %s (%d bytes)", file_path, len(content))


def _fetch_and_write(url: str, output_dir: str, written: set[str], lock: threading.Lock) -> None:
    logger.debug("Fetching %s", url)
    try:
        data = _fetch_json_dict(url)
    except Exception as exc:
        logger.error("Failed to fetch %s: %s", url, exc)
        return

    api_block = data.get("data", {}).get("api", {})
    logger.debug("Response keys for %s: %s", url, list(api_block.keys()))
    schema = api_block.get("schema")
    if not schema:
        logger.warning("No schema in response for %s", url)
        return

    raw = api_block.get("url") or api_block.get("uri")
    if not raw:
        logger.warning("No file path in response for %s", url)
        return
    file_path = os.path.join(output_dir, os.path.basename(raw))
    logger.debug("Resolved output path: %s", file_path)

    with lock:
        if file_path in written:
            logger.warning("Skipping — already written this run: %s (source: %s)", file_path, url)
            return
        written.add(file_path)

    try:
        _write_schema(file_path, schema)
    except OSError as exc:
        logger.error("Failed to write %s: %s", file_path, exc)
        with lock:
            written.discard(file_path)


# ── normalize helpers ──────────────────────────────────────────────────────────


def _pick_server_url(servers: list[dict]) -> str | None:
    if not servers:
        return None
    for s in servers:
        if "{jfrog_url}" in s.get("url", ""):
            return s["url"]
    return servers[0].get("url")


def _extract_base_path(url: str) -> tuple[str, str]:
    m = _URL_RE.match(url)
    if m:
        return m.group(1), (m.group(2) or "").rstrip("/")
    return url, ""


def _normalize_spec(spec: dict) -> tuple[dict, str]:
    spec = copy.deepcopy(spec)
    servers: list[dict] = spec.get("servers", [])
    chosen_url = _pick_server_url(servers)

    if not chosen_url:
        return spec, "no servers — skipped"

    base, base_path = _extract_base_path(chosen_url)
    if not base_path:
        return spec, f"server has no path ({chosen_url}) — paths unchanged"

    old_paths: dict = spec.get("paths", {})
    spec["paths"] = {base_path + k: v for k, v in old_paths.items()}

    for s in servers:
        s_base, s_path = _extract_base_path(s.get("url", ""))
        if s_path == base_path:
            s["url"] = s_base or "/"

    return spec, f"{chosen_url}  →  server={base}  prefix={base_path!r}  ({len(old_paths)} paths rewritten)"


# ── merge helpers ──────────────────────────────────────────────────────────────


def _discover_apis(apis_dir: str) -> list[dict]:
    return [
        {"file": name}
        for name in sorted(os.listdir(apis_dir))
        if os.path.splitext(name)[1].lower() in (".yaml", ".yml", ".json")
    ]


def _resolve_input_file(name: str, apis_dir: str) -> str:
    if os.path.isabs(name) and os.path.exists(name):
        return name
    if os.path.exists(name):
        return os.path.abspath(name)
    candidate = os.path.join(apis_dir, os.path.basename(name))
    if os.path.exists(candidate):
        return os.path.abspath(candidate)
    raise FileNotFoundError(f"Cannot find API file: {name!r}")


def _load_spec(path: str) -> dict:
    _, ext = os.path.splitext(path)
    with open(path, encoding="utf-8") as fh:
        if ext.lower() in (".yaml", ".yml"):
            if not HAS_YAML:
                raise RuntimeError("PyYAML is required: pip install pyyaml")
            return yaml.safe_load(fh)
        return json.load(fh)


def _apply_path_exclusions(spec: dict, exclude_paths: list[str], source: str) -> dict:
    if not exclude_paths:
        return spec
    paths = spec.get("paths", {})
    removed = []
    for p in exclude_paths:
        if p in paths:
            del paths[p]
            removed.append(p)
        else:
            logger.warning("excludePath %r not found in %s", p, source)
    if removed:
        logger.info("Excluded %d path(s) from %s: %s", len(removed), source, removed)
    return spec


def _prepare_merge_input(entry: dict, apis_dir: str, tmp_dir: str) -> str:
    file_path = _resolve_input_file(entry["file"], apis_dir)
    exclude_paths: list[str] = entry.get("excludePaths", [])
    if not exclude_paths:
        return file_path
    spec = _load_spec(file_path)
    spec = _apply_path_exclusions(spec, exclude_paths, entry["file"])
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json",
        prefix=f"pre-{os.path.splitext(os.path.basename(file_path))[0]}-",
        dir=tmp_dir,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(spec, fh)
    logger.info("Pre-processed %s -> %s", entry["file"], tmp_path)
    return tmp_path


def _remove_examples(obj):
    if isinstance(obj, dict):
        return {k: _remove_examples(v) for k, v in obj.items() if k not in ("example", "examples")}
    if isinstance(obj, list):
        return [_remove_examples(item) for item in obj]
    return obj


def _const_to_enum(obj):
    if isinstance(obj, dict):
        result = {k: _const_to_enum(v) for k, v in obj.items()}
        if "const" in result:
            result["enum"] = [result.pop("const")]
        return result
    if isinstance(obj, list):
        return [_const_to_enum(item) for item in obj]
    return obj


def _fix_schema_types(obj):
    if isinstance(obj, list):
        return [_fix_schema_types(item) for item in obj]
    if not isinstance(obj, dict):
        return obj
    result = {k: _fix_schema_types(v) for k, v in obj.items()}
    if "type" in result and isinstance(result["type"], list):
        types = result["type"]
        has_null = "null" in types
        non_null = [t for t in types if t != "null"]
        if not non_null:
            del result["type"]
        else:
            result["type"] = non_null[0]
            if has_null:
                result["nullable"] = True
    elif "type" in result and isinstance(result["type"], str) and result["type"] == "null":
        del result["type"]
    return result


def _normalize_servers(spec: dict) -> dict:
    """Replace every server entry's URL with MERGE_SERVER_URL."""
    servers = spec.get("servers", [])
    if not servers:
        return spec
    seen_paths: set[str] = set()
    normalized: list[dict] = []
    for server in servers:
        path = ""
        m = _URL_RE.match(server.get("url", ""))
        if m:
            path = (m.group(2) or "").rstrip("/")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        normalized.append({"url": MERGE_SERVER_URL + path})
    spec["servers"] = normalized
    return spec


def _fix_server_variables(spec: dict) -> dict:
    _var_re = re.compile(r"\{([^}]+)\}")
    for server in spec.get("servers", []):
        url = server.get("url", "")
        variables = server.setdefault("variables", {})
        for name in _var_re.findall(url):
            if name not in variables:
                variables[name] = MERGE_SERVER_VARIABLES.get(
                    name, {"default": name, "description": name}
                )
    return spec


def _run_openapi_merge(config_path: str, use_npx: bool) -> int:
    cmd = (["npx", "--yes", "openapi-merge-cli"] if use_npx else ["openapi-merge-cli"]) + [
        "--config", config_path
    ]
    logger.info("Running: %s", " ".join(cmd))
    return subprocess.run(cmd).returncode  # noqa: S603


# ── subcommand handlers ────────────────────────────────────────────────────────


def cmd_scrape(args: argparse.Namespace) -> None:
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)
    urls = _build_api_urls(SCRAPE_PAGES)
    written: set[str] = set()
    lock = threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_fetch_and_write, url, output_dir, written, lock): url
            for url in urls
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                logger.error("Unexpected error for %s: %s", futures[future], exc)

    for fname in os.listdir(output_dir):
        fpath = os.path.join(output_dir, fname)
        if os.path.isfile(fpath) and fpath not in written:
            logger.info("Removing stale file: %s", fpath)
            os.remove(fpath)

    for fname in SCRAPE_EXCLUDE_FILES:
        fpath = os.path.join(output_dir, fname)
        if os.path.isfile(fpath):
            logger.info("Removing excluded file: %s", fpath)
            os.remove(fpath)

    logger.info("Scrape complete. %d file(s) written to %s", len(written), output_dir)


def cmd_normalize(args: argparse.Namespace) -> None:
    if not HAS_YAML:
        logger.error("PyYAML is required for normalize: pip install pyyaml")
        sys.exit(1)

    input_dir, output_dir = args.input_dir, args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    entries = sorted(
        name for name in os.listdir(input_dir)
        if os.path.splitext(name)[1].lower() in (".yaml", ".yml", ".json")
    )
    if not entries:
        logger.error("No spec files found in %s", input_dir)
        sys.exit(1)

    for name in entries:
        src = os.path.join(input_dir, name)
        dst = os.path.join(output_dir, name)
        with open(src, encoding="utf-8") as fh:
            try:
                spec = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                logger.error("YAML parse error in %s: %s", src, exc)
                continue
        if not isinstance(spec, dict):
            logger.warning("Skipping %s — not a mapping at top level", src)
            continue
        normalized, note = _normalize_spec(spec)
        logger.info("%s: %s", name, note)
        with open(dst, "w", encoding="utf-8") as fh:
            yaml.dump(normalized, fh, allow_unicode=True, sort_keys=False)

    logger.info("Normalize complete. %d file(s) written to %s", len(entries), output_dir)


def cmd_merge(args: argparse.Namespace) -> None:
    apis_dir = args.input_dir
    output_path = (
        args.output if os.path.isabs(args.output)
        else os.path.join(PROJECT_DIR, args.output)
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    inputs = MERGE_INPUTS if MERGE_INPUTS else _discover_apis(apis_dir)
    if not inputs:
        logger.error("No API files found in %s and MERGE_INPUTS is empty.", apis_dir)
        sys.exit(1)
    if not MERGE_INPUTS:
        logger.info("MERGE_INPUTS is empty — auto-discovered %d file(s).", len(inputs))

    os.makedirs(MERGE_TMP_DIR, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="run-", dir=MERGE_TMP_DIR)

    try:
        input_file_paths: list[str] = []
        for entry in inputs:
            try:
                path = _prepare_merge_input(entry, apis_dir, tmp_dir)
            except FileNotFoundError as exc:
                logger.error("%s", exc)
                sys.exit(1)
            input_file_paths.append(path)

        logger.info("Merging %d file(s) -> %s", len(input_file_paths), output_path)

        config_data = {
            "inputs": [{"inputFile": os.path.relpath(p, tmp_dir)} for p in input_file_paths],
            "output": os.path.relpath(output_path, tmp_dir),
        }

        if args.keep_config:
            config_path = os.path.join(PROJECT_DIR, "openapi-merge.json")
        else:
            fd, config_path = tempfile.mkstemp(
                suffix=".json", prefix="openapi-merge-cfg-", dir=tmp_dir
            )
            os.close(fd)

        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(config_data, fh, indent=2)
        logger.info("Config written to %s", config_path)

        rc = _run_openapi_merge(config_path, use_npx=not args.no_npx)
        if rc != 0:
            logger.error("openapi-merge-cli exited with code %d", rc)
            sys.exit(rc)

        with open(output_path, encoding="utf-8") as fh:
            merged = json.load(fh)

        merged["info"] = {
            "title": MERGE_API_TITLE,
            "description": MERGE_API_DESCRIPTION,
            "version": MERGE_API_VERSION,
            "contact": {"name": MERGE_API_CONTACT_NAME, "url": MERGE_API_CONTACT_URL},
        }
        merged = _remove_examples(merged)
        merged = _const_to_enum(merged)
        merged = _fix_schema_types(merged)
        merged = _normalize_servers(merged)
        merged = _fix_server_variables(merged)

        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)

        logger.info("Merge complete. Output: %s", output_path)
        if args.keep_config:
            logger.info("Config retained at %s", config_path)

    finally:
        if not args.keep_config:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def cmd_all(args: argparse.Namespace) -> None:
    cmd_scrape(argparse.Namespace(
        output_dir=args.scraped_dir,
        workers=args.workers,
    ))
    cmd_normalize(argparse.Namespace(
        input_dir=args.scraped_dir,
        output_dir=args.normalized_dir,
    ))
    cmd_merge(argparse.Namespace(
        input_dir=args.normalized_dir,
        output=args.output,
        keep_config=args.keep_config,
        no_npx=args.no_npx,
    ))


# ── argument parser ────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jfrog_openapi",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        metavar="LEVEL",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scrape
    p = sub.add_parser("scrape", help="Crawl JFrog docs and download raw OpenAPI specs.")
    p.add_argument(
        "--output-dir", default=SCRAPE_DEFAULT_OUTPUT_DIR, metavar="DIR",
        help=f"Directory to write downloaded specs (default: {SCRAPE_DEFAULT_OUTPUT_DIR}).",
    )
    p.add_argument(
        "--workers", type=int, default=SCRAPE_MAX_WORKERS, metavar="N",
        help=f"Parallel download workers (default: {SCRAPE_MAX_WORKERS}).",
    )

    # normalize
    p = sub.add_parser("normalize", help="Absorb server URL paths into API path keys.")
    p.add_argument(
        "--input-dir", default=NORMALIZE_DEFAULT_INPUT_DIR, metavar="DIR",
        help=f"Directory of raw specs (default: {NORMALIZE_DEFAULT_INPUT_DIR}).",
    )
    p.add_argument(
        "--output-dir", default=NORMALIZE_DEFAULT_OUTPUT_DIR, metavar="DIR",
        help=f"Directory for normalized specs (default: {NORMALIZE_DEFAULT_OUTPUT_DIR}).",
    )

    # merge
    p = sub.add_parser("merge", help="Merge normalized specs into a single JSON file.")
    p.add_argument(
        "--input-dir", default=MERGE_DEFAULT_INPUT_DIR, metavar="DIR",
        help=f"Directory of normalized specs (default: {MERGE_DEFAULT_INPUT_DIR}).",
    )
    p.add_argument(
        "--output", default=MERGE_DEFAULT_OUTPUT, metavar="FILE",
        help=f"Output file path (default: {MERGE_DEFAULT_OUTPUT}).",
    )
    p.add_argument("--keep-config", action="store_true", help="Keep the generated openapi-merge config file.")
    p.add_argument("--no-npx", action="store_true", help="Use a locally installed openapi-merge-cli instead of npx.")

    # all
    p = sub.add_parser("all", help="Run scrape → normalize → merge in sequence.")
    p.add_argument(
        "--scraped-dir", default=SCRAPE_DEFAULT_OUTPUT_DIR, metavar="DIR",
        help=f"Intermediate directory for downloaded specs (default: {SCRAPE_DEFAULT_OUTPUT_DIR}).",
    )
    p.add_argument(
        "--normalized-dir", default=NORMALIZE_DEFAULT_OUTPUT_DIR, metavar="DIR",
        help=f"Intermediate directory for normalized specs (default: {NORMALIZE_DEFAULT_OUTPUT_DIR}).",
    )
    p.add_argument(
        "--output", default=MERGE_DEFAULT_OUTPUT, metavar="FILE",
        help=f"Final merged output file (default: {MERGE_DEFAULT_OUTPUT}).",
    )
    p.add_argument(
        "--workers", type=int, default=SCRAPE_MAX_WORKERS, metavar="N",
        help=f"Parallel download workers for scrape (default: {SCRAPE_MAX_WORKERS}).",
    )
    p.add_argument("--keep-config", action="store_true", help="Keep the generated openapi-merge config file.")
    p.add_argument("--no-npx", action="store_true", help="Use a locally installed openapi-merge-cli instead of npx.")

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s: %(message)s",
        force=True,
    )
    logger.debug("Log level set to %s", args.log_level)
    {"scrape": cmd_scrape, "normalize": cmd_normalize, "merge": cmd_merge, "all": cmd_all}[
        args.command
    ](args)


if __name__ == "__main__":
    main()
