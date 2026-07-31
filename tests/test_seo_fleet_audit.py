import unittest

from tools.seo_fleet_audit import (
    FetchReceipt,
    SiteConfig,
    audit_site,
    is_safe_public_https_url,
    render_markdown,
)


class FakeFetcher:
    def __init__(self, receipts: dict[str, FetchReceipt]):
        self.receipts = receipts
        self.calls: list[str] = []

    def __call__(self, url: str) -> FetchReceipt:
        self.calls.append(url)
        return self.receipts[url]


class SeoFleetAuditTests(unittest.TestCase):
    def test_healthy_site_checks_root_robots_sitemap_and_every_loc(self) -> None:
        origin = "https://example.com"
        fetch = FakeFetcher(
            {
                f"{origin}/": FetchReceipt(
                    requested_url=f"{origin}/",
                    status=200,
                    final_url=f"{origin}/",
                    content_type="text/html; charset=utf-8",
                    body='<link rel="canonical" href="https://example.com/">',
                ),
                f"{origin}/robots.txt": FetchReceipt(
                    requested_url=f"{origin}/robots.txt",
                    status=200,
                    final_url=f"{origin}/robots.txt",
                    content_type="text/plain",
                    body=f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n",
                ),
                f"{origin}/sitemap.xml": FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=(
                        '<?xml version="1.0"?>'
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<url><loc>{origin}/</loc></url>"
                        f"<url><loc>{origin}/pricing</loc></url>"
                        "</urlset>"
                    ),
                ),
                f"{origin}/pricing": FetchReceipt(
                    requested_url=f"{origin}/pricing",
                    status=200,
                    final_url=f"{origin}/pricing",
                    content_type="text/html",
                    body="<html><title>Pricing</title></html>",
                ),
            }
        )

        audit = audit_site(
            SiteConfig(name="Example", origin=origin), fetch, max_workers=4
        )

        self.assertEqual([], audit.findings)
        self.assertEqual(4, audit.checked_urls)
        self.assertEqual(2, audit.sitemap_urls)

    def test_reports_exact_wrong_host_redirect_and_broken_url_receipts(self) -> None:
        origin = "https://example.com"
        foreign = "https://preview.example.net"
        fetch = FakeFetcher(
            {
                f"{origin}/": FetchReceipt(
                    requested_url=f"{origin}/",
                    status=200,
                    final_url=f"{origin}/",
                    content_type="text/html",
                    body=f'<link rel="canonical" href="{foreign}/">',
                ),
                f"{origin}/robots.txt": FetchReceipt(
                    requested_url=f"{origin}/robots.txt",
                    status=200,
                    final_url=f"{origin}/robots.txt",
                    content_type="text/plain",
                    body=f"Sitemap: {foreign}/sitemap.xml\n",
                ),
                f"{origin}/sitemap.xml": FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=(
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<url><loc>{origin}/gone</loc></url>"
                        f"<url><loc>{origin}/moved</loc></url>"
                        f"<url><loc>{foreign}/leaked</loc></url>"
                        "</urlset>"
                    ),
                ),
                f"{origin}/gone": FetchReceipt(
                    requested_url=f"{origin}/gone",
                    status=404,
                    final_url=f"{origin}/gone",
                    content_type="text/html",
                    body="not found",
                ),
                f"{origin}/moved": FetchReceipt(
                    requested_url=f"{origin}/moved",
                    status=200,
                    final_url=f"{origin}/pricing",
                    content_type="text/html",
                    body="moved",
                    redirects=(f"{origin}/pricing",),
                ),
            }
        )

        audit = audit_site(SiteConfig(name="Example", origin=origin), fetch)
        codes = {finding.code for finding in audit.findings}

        self.assertIn("ROOT_CANONICAL_MISMATCH", codes)
        self.assertIn("ROBOTS_SITEMAP_MISSING", codes)
        self.assertIn("SITEMAP_REF_HOST_MISMATCH", codes)
        self.assertIn("LOC_HTTP_STATUS", codes)
        self.assertIn("LOC_HOST_MISMATCH", codes)
        self.assertIn("LOC_REDIRECT", codes)

        report = render_markdown([audit])
        self.assertIn(f"`{origin}/gone`", report)
        self.assertIn("HTTP 200", report)
        self.assertIn("HTTP 404", report)
        self.assertIn(f"`{origin}/pricing`", report)
        self.assertNotIn(f"{foreign}/sitemap.xml", fetch.calls)
        self.assertNotIn(f"{foreign}/leaked", fetch.calls)

    def test_rejects_non_public_fetch_targets(self) -> None:
        def public_resolver(_host: str) -> list[str]:
            return ["93.184.216.34"]

        def private_resolver(_host: str) -> list[str]:
            return ["10.0.0.8"]

        self.assertTrue(
            is_safe_public_https_url("https://example.com/path", public_resolver)
        )
        for url, resolver in [
            ("http://example.com", public_resolver),
            ("https://user:password@example.com", public_resolver),
            ("https://example.com:8443", public_resolver),
            ("https://127.0.0.1", public_resolver),
            ("https://169.254.169.254/latest/meta-data", public_resolver),
            ("https://metadata.internal/path", private_resolver),
        ]:
            with self.subTest(url=url):
                self.assertFalse(is_safe_public_https_url(url, resolver))

    def test_site_origin_rejects_credentials_query_and_fragment(self) -> None:
        for origin in [
            "https://user@example.com",
            "https://example.com?preview=1",
            "https://example.com#preview",
        ]:
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    SiteConfig(name="Unsafe", origin=origin)

    def test_reports_a_non_html_root(self) -> None:
        origin = "https://example.com"
        asset_url = f"{origin}/status.json"
        fetch = FakeFetcher(
            {
                f"{origin}/": FetchReceipt(
                    requested_url=f"{origin}/",
                    status=200,
                    final_url=f"{origin}/",
                    content_type="application/json",
                    body="{}",
                ),
                f"{origin}/robots.txt": FetchReceipt(
                    requested_url=f"{origin}/robots.txt",
                    status=200,
                    final_url=f"{origin}/robots.txt",
                    content_type="text/plain",
                    body=f"Sitemap: {origin}/sitemap.xml\n",
                ),
                f"{origin}/sitemap.xml": FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=(
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<url><loc>{asset_url}</loc></url>"
                        "</urlset>"
                    ),
                ),
                asset_url: FetchReceipt(
                    requested_url=asset_url,
                    status=200,
                    final_url=asset_url,
                    content_type="application/json",
                    body="{}",
                ),
            }
        )

        audit = audit_site(SiteConfig(name="Example", origin=origin), fetch)

        self.assertIn("ROOT_CONTENT_TYPE", {finding.code for finding in audit.findings})

    def test_reports_malformed_or_empty_sitemap_xml(self) -> None:
        origin = "https://example.com"
        common = {
            f"{origin}/": FetchReceipt(
                requested_url=f"{origin}/",
                status=200,
                final_url=f"{origin}/",
                content_type="text/html",
                body='<link rel="canonical" href="https://example.com/">',
            ),
            f"{origin}/robots.txt": FetchReceipt(
                requested_url=f"{origin}/robots.txt",
                status=200,
                final_url=f"{origin}/robots.txt",
                content_type="text/plain",
                body=f"Sitemap: {origin}/sitemap.xml\n",
            ),
        }

        for body, expected_code in [
            ("<urlset>", "SITEMAP_XML_INVALID"),
            (
                "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'/>",
                "SITEMAP_EMPTY",
            ),
            (
                "<feed><loc>https://example.com/</loc></feed>",
                "SITEMAP_ROOT_INVALID",
            ),
        ]:
            with self.subTest(expected_code=expected_code):
                receipts = dict(common)
                receipts[f"{origin}/sitemap.xml"] = FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=body,
                )
                audit = audit_site(
                    SiteConfig(name="Example", origin=origin), FakeFetcher(receipts)
                )
                self.assertIn(
                    expected_code,
                    {finding.code for finding in audit.findings},
                )


if __name__ == "__main__":
    unittest.main()
