import os
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from urllib.parse import quote_plus

load_dotenv()

BRAVE_SEARCH_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

def _format_results(results: list[dict[str, str]]) -> str:
    if not results:
        return "検索結果が見つかりませんでした。"
    return "\n".join(
        [f"- {r['title']} ({r['url']})\n  {r['snippet']}" for r in results]
    )

def _search_duckduckgo(query: str) -> list[dict[str, str]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    response = requests.get(url, headers=headers, timeout=8)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    results: list[dict[str, str]] = []
    for result in soup.select(".result"):
        link = result.select_one(".result__a")
        snippet = result.select_one(".result__snippet")
        if not link:
            continue
        href = link.get("href", "").strip()
        if not href:
            continue
        results.append(
            {
                "title": link.get_text(strip=True) or "No Title",
                "url": href,
                "snippet": snippet.get_text(" ", strip=True) if snippet else "",
            }
        )
        if len(results) >= 3:
            break
    return results

def search_web(query: str):
    """Brave Search API を利用して Web 検索を実行する"""
    if not query or not str(query).strip():
        return "Error: query parameter is required."

    query = str(query).strip()

    brave_error = None
    if BRAVE_SEARCH_API_KEY:
        headers = {
            "X-Subscription-Token": BRAVE_SEARCH_API_KEY,
            "Accept": "application/json",
        }
        params = {
            "q": query,
            "count": 3, # 件数を絞って高速化
            "safesearch": "off",
        }

        try:
            response = requests.get(BRAVE_SEARCH_URL, headers=headers, params=params, timeout=5) # 5秒に短縮
            response.raise_for_status()
            data = response.json()

            results = []
            if data and "web" in data and "results" in data["web"]:
                for r in data["web"]["results"]:
                    results.append({
                        "title": r.get("title", "No Title"),
                        "url": r.get("url", ""),
                        "snippet": r.get("description", "")
                    })
            if results:
                return _format_results(results)
            brave_error = "Brave search returned no results."
        except Exception as e:
            brave_error = str(e)

    try:
        fallback_results = _search_duckduckgo(query)
        if fallback_results:
            return _format_results(fallback_results)
        if brave_error:
            return f"Error: 検索に失敗しました (Brave: {brave_error}, Fallback: no results)"
        return "検索結果が見つかりませんでした。"
    except Exception as e:
        if brave_error:
            return f"Error: 検索に失敗しました (Brave: {brave_error}, Fallback: {str(e)})"
        return f"Error: 検索に失敗しました ({str(e)})"

def _clean_text(text: str, limit: int = 5000) -> str:
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    cleaned = "\n".join(chunk for chunk in chunks if chunk)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "..."
    return cleaned

def fetch_content(url: str):
    """指定された URL から本文テキストを抽出する"""
    if not url or not str(url).strip():
        return "Error: url parameter is required."

    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        response = requests.get(url, headers=headers, timeout=8) # 8秒に短縮
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 不要な要素を削除
        for script_or_style in soup(["script", "style", "nav", "header", "footer", "aside"]):
            script_or_style.decompose()

        # 本文と思われる要素を優先的に取得
        main_content = soup.find('main') or soup.find('article') or soup.find(id='content') or soup.find(class_='content')
        target = main_content if main_content else soup.body
        
        if not target:
            return "エラー: 本文を取得できませんでした。"

        text = target.get_text(separator='\n')
        return _clean_text(text)
    except Exception as e:
        try:
            stripped = url.removeprefix("https://").removeprefix("http://")
            reader_url = f"https://r.jina.ai/http://{stripped}"
            fallback = requests.get(reader_url, timeout=10)
            fallback.raise_for_status()
            if fallback.text.strip():
                return _clean_text(fallback.text)
        except Exception as fallback_error:
            return f"Error: 内容の取得に失敗しました ({str(e)} / Fallback: {str(fallback_error)})"
        return f"Error: 内容の取得に失敗しました ({str(e)})"

if __name__ == "__main__":
    # Test
    # print(search_web("Apple M4"))
    # print(fetch_content("https://example.com"))
    pass
