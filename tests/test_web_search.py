from __future__ import annotations

import json
import unittest

from gws_tui.web_search import SearchResult, WebSearchService, parse_brave_search_payload, parse_duckduckgo_html


class WebSearchParserTest(unittest.TestCase):
    def test_parse_duckduckgo_html_extracts_results(self) -> None:
        html = """
        <html>
          <body>
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Falpha">Alpha Result</a>
            <a class="result__snippet">Alpha snippet for testing.</a>
            <a class="result__a" href="https://example.com/beta">Beta Result</a>
            <div class="result__snippet">Beta snippet for testing.</div>
          </body>
        </html>
        """

        results = parse_duckduckgo_html(html)

        self.assertEqual(
            results,
            [
                SearchResult(title="Alpha Result", url="https://example.com/alpha", snippet="Alpha snippet for testing."),
                SearchResult(title="Beta Result", url="https://example.com/beta", snippet="Beta snippet for testing."),
            ],
        )

    def test_parse_brave_search_payload_extracts_results(self) -> None:
        payload = {
            "web": {
                "results": [
                    {
                        "title": "Alpha",
                        "url": "https://example.com/alpha",
                        "description": "Alpha description.",
                    },
                    {
                        "title": "Beta",
                        "url": "https://example.com/beta",
                        "description": "Beta description.",
                    },
                ]
            }
        }

        results = parse_brave_search_payload(payload)

        self.assertEqual(results[0].title, "Alpha")
        self.assertEqual(results[1].url, "https://example.com/beta")

    def test_brave_provider_requires_api_key(self) -> None:
        service = WebSearchService(provider="brave", brave_api_key="", timeout_seconds=1)

        with self.assertRaises(RuntimeError) as ctx:
            service.search("latest update")

        self.assertIn("Brave web search requires", str(ctx.exception))

    def test_search_rejects_blank_query(self) -> None:
        service = WebSearchService(provider="duckduckgo", timeout_seconds=1)

        with self.assertRaises(RuntimeError) as ctx:
            service.search("   ")

        self.assertIn("Web search query is required", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
