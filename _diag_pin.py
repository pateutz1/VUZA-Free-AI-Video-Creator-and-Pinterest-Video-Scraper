import asyncio, json
from urllib.parse import quote
from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

QUERY = "neon metropolis futuristic"
SOURCE_URL = f"https://www.pinterest.com/search/pins/?q={quote(QUERY)}"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        # CSRF cookie is set by the page load itself
        cookies = await ctx.cookies("https://www.pinterest.com")
        csrftoken = next((c["value"] for c in cookies if c["name"] == "csrftoken"), None)
        print("CSRFTOKEN:", (csrftoken or "NONE")[:12], "...")

        api_url = "https://www.pinterest.com/resource/BaseSearchResource/get/"
        payload = {
            "source_url": f"/search/pins/?q={quote(QUERY)}",
            "data": json.dumps({
                "options": {"query": QUERY, "scope": "pins", "page_size": 25},
                "context": {},
            }),
        }
        status, body = await page.evaluate(
            """async ([apiUrl, fetchPayload, token]) => {
                const r = await fetch(apiUrl, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'accept': 'application/json, text/javascript, */*, q=0.01',
                        'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'x-requested-with': 'XMLHttpRequest',
                        'x-csrftoken': token,
                    },
                    body: new URLSearchParams(fetchPayload).toString(),
                });
                return [r.status, await r.text()];
            }""",
            [api_url, payload, csrftoken],
        )
        print("STATUS:", status)
        try:
            data = json.loads(body)
        except Exception as exc:
            print("PARSE_FAIL:", exc, body[:300])
            return
        results = (
            data.get("resource_response", {}).get("data", {}).get("results", [])
        )
        print("RESULTS:", len(results))
        for item in results[:5]:
            images = item.get("images") or {}
            best = None
            if images:
                first = next(iter(images.values()))
                best = first.get("url")
            print("PIN:", item.get("id"), "|", str(best)[:110])
        await browser.close()

asyncio.run(main())
