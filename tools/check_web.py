import re, sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(root, "web", "index.html"), encoding="utf-8").read()
js = open(os.path.join(root, "web", "static", "app.js"), encoding="utf-8").read()
css = open(os.path.join(root, "web", "static", "style.css"), encoding="utf-8").read()

ids_html = set(re.findall(r'id="([^"]+)"', html))
ids_js = set(re.findall(r"\$\('([^']+)'\)", js))
missing = sorted(ids_js - ids_html)
print("JS 引用的 id 数量:", len(ids_js), "| HTML 中缺失:", missing or "无")

classes_html = set()
for m in re.findall(r'class="([^"]+)"', html):
    classes_html.update(m.split())
classes_js = set()
for m in re.findall(r'class="([^"]+)"', js):
    classes_js.update(x for x in m.split() if not x.startswith("${"))
missing_css = sorted(c for c in (classes_html | classes_js) if f".{c}" not in css)
print("HTML/JS 用到的 class:", len(classes_html | classes_js), "| CSS 中未定义:", missing_css or "无")

# 静态资源引用
for m in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
    p = os.path.join(root, "web", m.lstrip("/"))
    print(f"  资源 {m}: {'存在' if os.path.exists(p) else '缺失!'}")

# 检查 css 括号平衡
print("CSS 花括号平衡:", css.count("{") == css.count("}"), f"({css.count('{')}/{css.count('}')})")

# 检查 js 关键函数
for fn in ["startSearch", "poll", "collect", "renderResult", "renderRows", "resort", "showHealth"]:
    print(f"  JS 函数 {fn}:", "有" if re.search(rf"function {fn}\b", js) else "缺失!")
