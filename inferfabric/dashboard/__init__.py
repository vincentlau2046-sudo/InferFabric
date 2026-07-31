"""InferFabric Dashboard — fragment-based assembly

G-3a: 拆分 dashboard.py 单文件 → fragments + js 目录
- 启动时一次性加载所有 fragments 和 JS 到内存缓存
- get_html() 返回完整 HTML，与原 dashboard.py 字节级等价
- fragment 加载失败 graceful degradation
"""

import logging
from pathlib import Path

log = logging.getLogger("inferfabric.dashboard")

_DASHBOARD_DIR = Path(__file__).parent
_cached_html: str | None = None


def get_html() -> str:
    """组装 fragments → 返回完整 HTML。启动时缓存到内存。"""
    global _cached_html
    if _cached_html is not None:
        return _cached_html
    try:
        base = (_DASHBOARD_DIR / "base.html").read_text(encoding="utf-8")
        # 替换 fragment 占位符
        for frag_name in ("inference", "monitor", "deploy", "cloud"):
            frag_path = _DASHBOARD_DIR / "fragments" / f"{frag_name}.html"
            try:
                frag = frag_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                log.warning("Dashboard fragment missing: %s", frag_path)
                frag = f'<div class="tab-content" id="tab-{frag_name}">'
                frag += f'<p style="padding:2em;color:#9ca3af;">⚠️ Fragment {frag_name} 加载失败，查看 iff 日志</p>'
                frag += '</div>'
            base = base.replace(f"<!-- FRAGMENT:{frag_name} -->", frag)
        # 替换 CSS 占位符
        css_path = _DASHBOARD_DIR / "fragments" / "style.css"
        try:
            css = css_path.read_text(encoding="utf-8")
            base = base.replace("<!-- CSS:main -->", css)
        except FileNotFoundError:
            log.warning("Dashboard CSS missing: %s", css_path)
        # 替换 JS 占位符
        for js_name in ("app", "monitor"):
            js_path = _DASHBOARD_DIR / "js" / f"{js_name}.js"
            try:
                js = js_path.read_text(encoding="utf-8")
                base = base.replace(f"<!-- JS:{js_name} -->", js)
            except FileNotFoundError:
                log.warning("Dashboard JS missing: %s", js_path)
        _cached_html = base
        return base
    except Exception as e:
        log.error("Dashboard fragment load failed: %s", e)
        return _fallback_html()


def invalidate_cache():
    """使缓存失效（热重载用）"""
    global _cached_html
    _cached_html = None


def _fallback_html() -> str:
    """最小可用 HTML — fragment 加载全部失败时的降级页面"""
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>InferFabric</title>
<style>body{font-family:monospace;padding:2em;background:#fafaf9;color:#1f2937;}</style>
</head><body>
<h1>⚠️ InferFabric Dashboard</h1>
<p>Dashboard 资源加载失败，查看 iff 日志获取详情。</p>
<p>API 端点仍可正常使用：<code>/v1/models</code> <code>/api/metrics</code></p>
</body></html>"""
