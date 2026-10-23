#!/usr/bin/env python3
"""Export a configured Figma icon collection. Requires Python 3.12+, no packages."""

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
STATE = "icons/index.json"
EXPORT_SCHEMA = 1


class SyncError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()


def catalog_bytes(icons):
    lines = ["# Icon catalog", "", "Generated from Figma. Select an icon name to open its SVG.", "",
             "| Preview | Icon | Collection | Artwork | Figma |", "| --- | --- | --- | --- | --- |"]
    for item in icons:
        name = html.escape(item["name"]).replace("\n", " ")
        label = re.sub(r"([\\\[\]*_`|])", r"\\\1", name)
        page = html.escape(item["page"]).replace("|", "&#124;").replace("\n", " ")
        relative = "./" + item["path"].removeprefix("icons/")
        artwork = "Includes raster image" if item.get("contains_raster") else "Vector"
        lines.append(f'| <img src="{relative}" width="40" alt="{name.replace(chr(124), "&#124;")}"> | [{label}]({relative}) | {page} | {artwork} | [Source]({item["figma_url"]}) |')
    return ("\n".join(lines) + "\n").encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Figma:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url, authenticated=False):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise SyncError("Expected an HTTPS export URL.")
        headers = {"User-Agent": "MSFT-Icons-sync/1"}
        if authenticated:
            headers["X-Figma-Token"] = self.token
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers=headers)
                # No redirects: never forward the Figma token to an asset host.
                with self.opener.open(request, timeout=90) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                    try:
                        delay = max(0, int(error.headers.get("Retry-After", 2 ** attempt)))
                    except ValueError:
                        delay = 2 ** attempt
                    if delay > 60:
                        raise SyncError(f"Rate limited; retry after {delay} seconds.") from None
                    time.sleep(delay)
                    continue
                raise SyncError(f"{'Figma API' if authenticated else 'SVG download'} returned HTTP {error.code}.") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise SyncError("Network request failed after retries.") from None

    def api(self, path, **params):
        query = urllib.parse.urlencode(params)
        payload = json.loads(self.request(f"https://api.figma.com/v1/{path}?{query}", True))
        if payload.get("err") or payload.get("error"):
            raise SyncError("Figma returned an API error.")
        return payload

    def download(self, url):
        return self.request(url)


def slug(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")[:80] or "icon"


def node_suffix(node_id):
    # Figma IDs are normally digits separated by colons; hex also handles instance IDs.
    if re.fullmatch(r"\d+:\d+", node_id):
        return node_id.replace(":", "-")
    return node_id.encode().hex()


def inventory(document):
    result = []

    def visit(node, page, visible=True):
        visible = visible and node.get("visible", True) and node.get("opacity", 1) != 0
        if node["type"] == "CANVAS":
            page = node
        result.append((node, page, visible))
        for child in node.get("children", []):
            visit(child, page, visible)

    visit(document, None)
    return result


def select_icons(document, config):
    rows = inventory(document)
    lookup = {node["id"]: (node, page, visible) for node, page, visible in rows}
    selected = {}

    def visit(node, page, visible):
        visible = visible and node.get("visible", True) and node.get("opacity", 1) != 0
        if not visible:
            return
        if node["type"] == "CANVAS":
            page = node
        matches = node["type"] == "COMPONENT" if config["selection"] == "components" else any(
            setting.get("format") == "SVG" for setting in node.get("exportSettings", [])
        )
        if matches and node["type"] not in {"DOCUMENT", "CANVAS", "SECTION", "COMPONENT_SET"}:
            if not page:
                raise SyncError("An icon must belong to a page.")
            suffix = node_suffix(node["id"])
            page_path = f"{slug(page['name'])}--{node_suffix(page['id'])}"
            selected[node["id"]] = {
                "id": node["id"], "name": node["name"], "page": page["name"],
                "path": f"icons/{page_path}/{slug(node['name'])}--{suffix}.svg",
            }
            return  # A complete icon is one export, not one export per nested vector.
        for child in node.get("children", []):
            visit(child, page, visible)

    for node_id in config["root_node_ids"]:
        if node_id not in lookup:
            raise SyncError(f"Configured root {node_id} no longer exists; no files changed.")
        visit(*lookup[node_id])
    if not selected:
        raise SyncError("No icons matched. Check the roots and selection mode; no files changed.")
    return sorted(selected.values(), key=lambda item: item["path"])


def managed_path(root, value):
    if not isinstance(value, str) or not re.fullmatch(r"icons/[a-z0-9-]+/[a-z0-9-]+\.svg", value):
        raise SyncError("Invalid managed SVG path in index.")
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()) or any(
        parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent
    ):
        raise SyncError("Managed SVG paths must not use symbolic links.")
    return path


def validate_svg(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise SyncError("Unexpected XML declarations in SVG export.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise SyncError("Figma export is not valid XML.") from None
    if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
        raise SyncError("Figma export is not an SVG document.")
    return any(node.tag.rsplit("}", 1)[-1] == "image" for node in root.iter())


def load_config(root, require_roots=True):
    config = json.loads((root / "figma-sync.json").read_text())
    config["file_key"] = os.environ.get("FIGMA_FILE_KEY") or config.get("file_key", "")
    if not re.fullmatch(r"[A-Za-z0-9]+", config["file_key"]):
        raise SyncError("Set file_key in figma-sync.json or the FIGMA_FILE_KEY environment variable.")
    if config.get("selection") not in {"components", "svg-export-settings"}:
        raise SyncError("selection must be components or svg-export-settings.")
    roots = config.get("root_node_ids")
    if not isinstance(roots, list) or any(not isinstance(item, str) or not item for item in roots):
        raise SyncError("root_node_ids must be a list of Figma node IDs.")
    if require_roots and not roots:
        raise SyncError("Choose the icon pages or container IDs in root_node_ids first.")
    if type(config.get("batch_size")) is not int or not 1 <= config["batch_size"] <= 50:
        raise SyncError("batch_size must be an integer between 1 and 50.")
    return config


def sync(root, client, config, dry_run=False, force=False):
    root = root.resolve()
    state_path = root / STATE
    catalog_path = root / "icons/README.md"
    if (root / "icons").is_symlink() or state_path.is_symlink() or catalog_path.is_symlink():
        raise SyncError("The icons directory and index must not be symbolic links.")
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    old_icons = previous.get("icons", [])
    for item in old_icons:
        managed_path(root, item["path"])
    config_hash = digest(json_bytes({"config": config, "export_schema": EXPORT_SCHEMA}))
    key = config["file_key"]
    # A cheap metadata request avoids full document/render requests when unchanged.
    if previous and not force and not dry_run:
        metadata = client.api(f"files/{key}/meta")["file"]
        if (metadata.get("version") and metadata["version"] == previous.get("figma_version")
                and config_hash == previous.get("config_hash") and old_icons
                and catalog_path.is_file() and catalog_path.read_bytes() == catalog_bytes(old_icons)
                and all(managed_path(root, item["path"]).is_file()
                        and digest(managed_path(root, item["path"]).read_bytes()) == item["sha256"]
                        for item in old_icons)):
            print("Figma version and local SVGs are unchanged.")
            return False

    source = client.api(f"files/{key}")
    version = source.get("version")
    if not version or source.get("document", {}).get("type") != "DOCUMENT":
        raise SyncError("Figma did not return a complete, versioned document.")
    icons = select_icons(source["document"], config)
    if dry_run:
        print(json_bytes({"version": version, "count": len(icons), "icons": icons}).decode(), end="")
        return False

    old_paths = {item["path"] for item in old_icons}
    for item in icons:
        destination = managed_path(root, item["path"])
        if destination.exists() and item["path"] not in old_paths:
            raise SyncError(f"Refusing to overwrite an unmanaged file: {item['path']}")

    # Download and validate ALL renders before touching any existing assets.
    with tempfile.TemporaryDirectory(prefix=".figma-sync-", dir=root) as temp:
        staging = Path(temp)
        for start in range(0, len(icons), config["batch_size"]):
            batch = icons[start:start + config["batch_size"]]
            rendered = client.api(
                f"images/{key}", ids=",".join(item["id"] for item in batch), format="svg",
                version=version, svg_outline_text="true", svg_include_id="false",
                svg_simplify_stroke="false", use_absolute_bounds="true", contents_only="true",
            ).get("images", {})
            for item in batch:
                url = rendered.get(item["id"])
                if not isinstance(url, str) or not url:
                    raise SyncError(f"Figma could not render {item['id']}; no files changed.")
                data = client.download(url)
                item["contains_raster"] = validate_svg(data)
                target = staging / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                item["sha256"] = digest(data)
                item["figma_url"] = f"https://www.figma.com/design/{key}?node-id={urllib.parse.quote(item['id'])}"
            print(f"Validated {min(start + len(batch), len(icons))}/{len(icons)} SVGs.")

        state = {"schema_version": EXPORT_SCHEMA, "file_key": key, "figma_version": version,
                 "config_hash": config_hash, "icons": icons}
        for item in icons:
            destination = managed_path(root, item["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            (staging / item["path"]).replace(destination)
        for stale in old_paths - {item["path"] for item in icons}:
            managed_path(root, stale).unlink(missing_ok=True)
        staged_state = staging / "index.json"
        staged_state.write_bytes(json_bytes(state))
        staged_catalog = staging / "README.md"
        staged_catalog.write_bytes(catalog_bytes(icons))
        staged_catalog.replace(catalog_path)
        staged_state.replace(state_path)
    print(f"Synced {len(icons)} icons from Figma version {version}.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true", help="List pages and potential icon counts")
    parser.add_argument("--dry-run", action="store_true", help="List selected icons without exporting or writing")
    parser.add_argument("--force", action="store_true", help="Re-export even when the version is unchanged")
    args = parser.parse_args()
    try:
        config = load_config(ROOT, require_roots=not args.inventory)
        token = os.environ.get("FIGMA_TOKEN")
        if not token:
            raise SyncError("Set FIGMA_TOKEN to a Figma personal access token.")
        client = Figma(token)
        if args.inventory:
            source = client.api(f"files/{config['file_key']}")
            for page in source["document"]["children"]:
                rows = inventory(page)
                print(json.dumps({"id": page["id"], "name": page["name"],
                                  "visible_components": sum(n["type"] == "COMPONENT" and v for n, _, v in rows),
                                  "visible_svg_export_nodes": sum(v and any(s.get("format") == "SVG" for s in n.get("exportSettings", [])) for n, _, v in rows)}))
        else:
            sync(ROOT, client, config, args.dry_run, args.force)
    except (SyncError, OSError, ValueError, KeyError, TypeError):
        error = sys.exc_info()[1]
        # Do not dump remote payloads, signed URLs or credentials in CI logs.
        print(f"Sync failed: {error if isinstance(error, SyncError) else type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
