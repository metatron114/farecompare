import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

for name in ["farecompare", "fare-compare"]:
    url = f"https://api.github.com/repos/metatron114/{name}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
            print(f"  [{name}] 已存在 -> {d['html_url']}  private={d['private']}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  [{name}] 不存在，可以创建")
        else:
            print(f"  [{name}] HTTP {e.code}")
    except Exception as e:
        print(f"  [{name}] {type(e).__name__}: {e}")
