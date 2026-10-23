# Microsoft App Icons

Microsoft application icons curated by Brandon Pittman, exported as individual SVG files from the [Figma collection](https://www.figma.com/design/Dc9hrEER6fgmrXVmOIImJT/Microsoft-App-Icons?node-id=0-1).

**[Browse and download icons](icons/README.md)** · **[Machine-readable index](icons/index.json)**

Figma is the source of truth. The repository includes the current icon components and a separate archive of deprecated icons. The SVGs preserve Figma's colors, gradients, masks, and icon bounds. Text is outlined so viewers do not need the original fonts.

Each icon has a readable filename plus its Figma node ID. This keeps identically named icons from overwriting each other. The index includes the original name, collection, Figma source link, and SVG checksum. To download everything, use GitHub's **Code → Download ZIP**.

## Daily updates

The included GitHub Actions workflow checks Figma daily at **09:17 UTC** (05:17 Toronto during daylight saving time; 04:17 during standard time). It also runs when the exporter or its configuration changes on the default branch, and provides **Actions → Sync icons from Figma → Run workflow** for an on-demand update. GitHub may delay scheduled runs.

The exporter reads file metadata first and skips rendering when the version and local outputs match. For changed files, it exports all selected icons from one pinned Figma version, validates every SVG, and commits the resulting collection. Renamed or deleted icons are removed from their old paths only after a complete successful export. A failed download, missing configured page, or empty selection stops the run and preserves the published collection.

## Enable the workflow

1. In **Figma → Settings → Security → Personal access tokens**, create a token with `file_content:read` and `file_metadata:read`. The token's account must have access to this file. Store the token directly in GitHub; do not commit it.
2. In [repository Actions secrets](https://github.com/quantumfctn/MSFT-Icons/settings/secrets/actions), add a secret named **`FIGMA_TOKEN`**.
3. Run **Sync icons from Figma** manually and check that the job succeeds. Scheduled runs use the default branch. The workflow needs permission to write repository contents; branch rules must allow its bot to push.

`figma-sync.json` already points at the source file and the Icons / Archive pages. Optional repository variable `FIGMA_FILE_KEY` overrides its file key. No separate GitHub personal access token is needed: the workflow uses GitHub's built-in `GITHUB_TOKEN`.

The Figma connection used in chat is separate from the credential used by GitHub Actions. Replace the `FIGMA_TOKEN` secret when its token expires. Figma's API limits depend on the seat and the plan containing the file. GitHub can disable scheduled workflows in public repositories after 60 days without repository activity; if the collection is unchanged for a long period, check that the schedule is still enabled.

## Curating the collection

By default, every visible **component** under the configured pages is exported, including individual variants in component sets. Nested artwork inside an icon is part of that SVG, not a separate icon. Add new icon components to either configured page and the next sync discovers them automatically.

The initial selection excludes the introductory page, presentation backgrounds, loose vector experiments, and non-component frames. Some named loose artwork in the source file (including Sentinel, Intune, and Project Opal) therefore needs a publication decision before inclusion. Hiding a component or moving it outside the selected roots removes it from the next successful export.

Configuration:

| Field | Meaning |
| --- | --- |
| `file_key` | The design file key from the Figma URL |
| `root_node_ids` | Pages or containers to search; currently `0:1` (Icons) and `686:155` (Archive) |
| `selection` | `components`, or `svg-export-settings` to select nodes explicitly marked for SVG export |
| `batch_size` | Number of icons per render request, from 1 to 50 |

Exporting raster artwork as SVG does not turn it into vector paths. The catalog marks SVGs that contain embedded images, and the index includes a `contains_raster` flag. In the initial export, Excel, Word, Stream, Outlook, OneDrive, Viva Pulse, and archived Skype include raster imagery; the remaining 117 exports contain no SVG image elements.

## Local development

Python 3.12 or newer is sufficient; there are no third-party dependencies. Provide `FIGMA_TOKEN` through your environment or secret manager, then run:

```sh
python3 scripts/sync_icons.py --inventory  # Inspect available pages and counts
python3 scripts/sync_icons.py --dry-run    # Preview the exact selected icons
python3 scripts/sync_icons.py              # Export the collection
python3 scripts/sync_icons.py --force      # Rebuild regardless of file version
python3 -m unittest discover -s tests -v
```

Run only one local export at a time. GitHub Actions serializes automated runs. Downloads are staged before installation; an interruption during installation can leave a partial local working tree, which a rerun repairs. The workflow commits only after the script finishes successfully, so such an interruption does not publish a partial collection.

This is a community-maintained collection, not an official Microsoft repository. Icon artwork and product trademarks belong to their respective owners.

## References

- [Figma SVG export API](https://developers.figma.com/docs/rest-api/file-endpoints/)
- [Figma token setup](https://developers.figma.com/docs/rest-api/personal-access-tokens/)
- [Figma API rate limits](https://developers.figma.com/docs/rest-api/rate-limits/)
- [GitHub scheduled workflow behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
