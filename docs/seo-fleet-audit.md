# Public-site fleet SEO audit

`tools/seo_fleet_audit.py` is the executable evidence layer for Quant Alchemy's public search-discovery surfaces. The production sensor is scheduled through Hermes every Monday at 13:15 UTC, and the crawler can also be run manually.

For every site in `config/public-sites.json`, the crawler verifies:

- the GitHub repository homepage matches the configured canonical production origin;
- the root resolves to canonical HTTP 200 HTML;
- the root canonical link, when declared, matches the configured production origin;
- `robots.txt` is direct, plain text, and references the canonical sitemap;
- every robots sitemap reference stays on the canonical host and serves parseable XML;
- every sitemap and nested sitemap-index `<loc>` is fetched;
- every page `<loc>` stays on the canonical host, returns direct HTTP 200, and—when HTML declares a canonical—uses the same URL.
- configured `expected_text_paths` such as `llms.txt` and `ai.txt` return direct HTTP 200 plain text;
- configured `required_canonical_paths` return direct HTTP 200 HTML and declare exactly one matching canonical link.

The run produces Markdown and JSON receipts with the exact requested URL, expected result, observed status/final URL, and defect code. The scheduled Hermes job delivers the Markdown receipt to the task thread; JSON is retained locally for machine processing. An authenticated `gh` CLI is required because some mapped repositories are private. A GitHub lookup failure exits with operational status `2`; homepage drift remains the normal defect status `1`.

## Local verification

```bash
python3 -m unittest tests/test_seo_fleet_audit.py -v
python3 tools/seo_fleet_audit.py \
  --config config/public-sites.json \
  --markdown-out /tmp/weekly-seo-fleet-audit.md \
  --json-out /tmp/weekly-seo-fleet-audit.json
```

Add a public production site by adding its canonical HTTPS origin and source repository to `config/public-sites.json`. Use root-relative `expected_text_paths` and `required_canonical_paths` only for artifacts and routes that are part of that site's explicit public contract. Authenticated/internal-only products should not be added.
