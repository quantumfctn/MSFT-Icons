import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from scripts.sync_icons import Figma, SyncError, digest, json_bytes, managed_path, select_icons, sync, validate_svg


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0h24v24z"/></svg>'
CONFIG = {"file_key": "test", "root_node_ids": ["0:1"], "selection": "components", "batch_size": 2}


def component(node_id, name="Microsoft 365"):
    return {"id": node_id, "type": "COMPONENT", "name": name, "children": []}


def document(*nodes):
    return {"type": "DOCUMENT", "id": "0:0", "name": "Icons", "children": [
        {"type": "CANVAS", "id": "0:1", "name": "Icons", "children": list(nodes)}]}


class FakeFigma:
    def __init__(self, doc=None, version="v1"):
        self.doc = doc or document(component("1:1"), component("1:2"))
        self.version = version
        self.calls = []
        self.failed_id = None
        self.bad_svg_id = None

    def api(self, path, **params):
        self.calls.append((path, params))
        if path.endswith("/meta"):
            return {"file": {"version": self.version}}
        if path.startswith("images/"):
            return {"images": {i: None if i == self.failed_id else f"https://assets.test/{i}"
                               for i in params["ids"].split(",")}}
        return {"document": self.doc, "version": self.version}

    def download(self, url):
        return b"<html>failure</html>" if url.endswith(str(self.bad_svg_id)) else SVG


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        output = patch("sys.stdout", new=io.StringIO())
        output.start()
        self.addCleanup(output.stop)

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_component_selection_skips_nested_vectors_and_hidden_ancestors(self):
        parent = component("1:1")
        parent["children"] = [component("2:1")]
        hidden = {"id": "3:1", "name": "Drafts", "type": "FRAME", "visible": False,
                  "children": [component("3:2")]}
        self.assertEqual([i["id"] for i in select_icons(document(parent, hidden), CONFIG)], ["1:1"])

    def test_duplicate_names_and_unsafe_names_cannot_overwrite_or_escape(self):
        icons = select_icons(document(component("1:1"), component("1:2"), component("1:3", "../../FOO\\bar")), CONFIG)
        self.assertEqual(len({i["path"] for i in icons}), 3)
        for item in icons:
            self.assertTrue(managed_path(self.root, item["path"]).is_relative_to(self.root))

    def test_variants_export_individually(self):
        variants = {"id": "2:1", "name": "Outlook", "type": "COMPONENT_SET", "children": [
            component("2:2", "Theme=Light"), component("2:3", "Theme=Dark")]}
        self.assertEqual(len(select_icons(document(variants), CONFIG)), 2)

    def test_svg_settings_support_frames_without_exporting_children_twice(self):
        node = {"id": "4:1", "name": "Sentinel", "type": "FRAME", "exportSettings": [{"format": "SVG"}],
                "children": [{"id": "4:2", "name": "path", "type": "VECTOR", "exportSettings": [{"format": "SVG"}]}]}
        config = {**CONFIG, "selection": "svg-export-settings"}
        self.assertEqual([i["id"] for i in select_icons(document(node), config)], ["4:1"])

    def test_exports_are_version_pinned_batched_and_indexed(self):
        client = FakeFigma(document(*(component(f"1:{i}") for i in range(5))))
        sync(self.root, client, CONFIG)
        renders = [p for path, p in client.calls if path.startswith("images/")]
        self.assertEqual(len(renders), 3)
        self.assertTrue(all(p["version"] == "v1" for p in renders))
        state = json.loads((self.root / "icons/index.json").read_text())
        self.assertEqual(len(state["icons"]), 5)
        self.assertEqual(state["icons"][0]["sha256"], digest(SVG))

    def test_unchanged_version_only_requests_metadata_and_repairs_missing_assets(self):
        client = FakeFigma()
        sync(self.root, client, CONFIG)
        before = self.files()
        client.calls.clear()
        self.assertFalse(sync(self.root, client, CONFIG))
        self.assertEqual(client.calls, [("files/test/meta", {})])
        self.assertEqual(self.files(), before)
        next(self.root.rglob("*.svg")).unlink()
        sync(self.root, client, CONFIG)
        self.assertEqual(self.files(), before)

    def test_config_change_forces_export_even_with_unchanged_version(self):
        client = FakeFigma()
        sync(self.root, client, CONFIG)
        client.calls.clear()
        sync(self.root, client, {**CONFIG, "batch_size": 1})
        self.assertEqual(sum(p.startswith("images/") for p, _ in client.calls), 2)

    def test_failed_render_or_invalid_svg_leaves_existing_collection_untouched(self):
        sync(self.root, FakeFigma(), CONFIG)
        before = self.files()
        for failure in ("failed_id", "bad_svg_id"):
            client = FakeFigma(document(component("1:1", "Renamed"), component("1:3")), "v2")
            setattr(client, failure, "1:3")
            with self.assertRaises(SyncError):
                sync(self.root, client, CONFIG)
            self.assertEqual(self.files(), before)

    def test_rename_and_deletion_remove_only_previously_managed_files(self):
        sync(self.root, FakeFigma(), CONFIG)
        manual = self.root / "icons/manual.svg"
        manual.write_bytes(SVG)
        sync(self.root, FakeFigma(document(component("1:1", "Renamed")), "v2"), CONFIG)
        self.assertTrue(manual.exists())
        self.assertEqual(len(list(self.root.rglob("*.svg"))), 2)
        self.assertTrue(any("renamed" in p.name for p in self.root.rglob("*.svg")))

    def test_missing_root_and_empty_results_cannot_clear_collection(self):
        sync(self.root, FakeFigma(), CONFIG)
        before = self.files()
        for doc in (document(), {"id": "0:0", "type": "DOCUMENT", "children": []}):
            with self.assertRaises(SyncError):
                sync(self.root, FakeFigma(doc, "v2"), CONFIG)
            self.assertEqual(self.files(), before)

    def test_dry_run_never_downloads_or_writes(self):
        client = FakeFigma()
        sync(self.root, client, CONFIG, dry_run=True)
        self.assertEqual(self.files(), {})
        self.assertEqual(client.calls, [("files/test", {})])

    def test_corrupt_index_path_and_symlink_are_rejected(self):
        (self.root / "icons").mkdir()
        (self.root / "icons/index.json").write_bytes(json_bytes({"icons": [{"path": "../README.md"}]}))
        with self.assertRaises(SyncError):
            sync(self.root, FakeFigma(), CONFIG)
        (self.root / "icons/index.json").unlink()
        (self.root / "icons/icons--0-1").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SyncError):
            sync(self.root, FakeFigma(), CONFIG)

    def test_token_is_sent_only_to_api_and_rate_limit_is_respected(self):
        client = Figma("test-secret")
        success = unittest.mock.MagicMock()
        success.__enter__.return_value.read.return_value = b'{}'
        error = urllib.error.HTTPError("https://api.figma.com", 429, "rate limited", {"Retry-After": "4"}, None)
        with patch.object(client.opener, "open", side_effect=[error, success, success]) as opener, patch("time.sleep") as sleep:
            client.api("files/test")
            client.download("https://assets.test/icon.svg")
        sleep.assert_called_once_with(4)
        self.assertEqual(opener.call_args_list[0].args[0].get_header("X-figma-token"), "test-secret")
        self.assertIsNone(opener.call_args_list[-1].args[0].get_header("X-figma-token"))

    def test_long_rate_limit_fails_without_a_long_sleep(self):
        client = Figma("test-secret")
        error = urllib.error.HTTPError("https://api.figma.com", 429, "rate limited", {"Retry-After": "86400"}, None)
        with patch.object(client.opener, "open", side_effect=error), patch("time.sleep") as sleep:
            with self.assertRaisesRegex(SyncError, "86400"):
                client.api("files/test")
        sleep.assert_not_called()

    def test_raster_imagery_is_identified_without_discarding_the_svg(self):
        self.assertFalse(validate_svg(SVG))
        self.assertTrue(validate_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,AAAA"/></svg>'))


if __name__ == "__main__":
    unittest.main()
