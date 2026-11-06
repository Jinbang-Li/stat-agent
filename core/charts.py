# -*- coding: utf-8 -*-
"""
图表渲染：**手写 SVG，不依赖绘图库**。

为什么不用 matplotlib：当前环境是 matplotlib 2.2.3（2018 年），配中文字体要改
全局 rcParams、还得在服务器上安装字体，一旦字体缺失，图里全是方块；而且它输出
位图，缩放后发虚。SVG 由浏览器渲染，中文字体直接用客户端系统的，既清晰又不用
在服务器上配置任何东西，体积还小。

代价是坐标轴、刻度、图例都得自己算——但图也就五种，值这个代价。
"""

import math

W = 660
H = 360
PAD_L = 64
PAD_R = 24
PAD_T = 44
PAD_B = 52

FONT = ("system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', "
        "'PingFang SC', sans-serif")

COLORS = ["#2563eb", "#f59e0b", "#10b981", "#ef4444",
          "#8b5cf6", "#0891b2", "#64748b"]
GRAY = "#64748b"
GRID = "#e2e8f0"
INK = "#0f172a"


def render(spec, width=W, height=H):
    """把图表规格渲染成 SVG 字符串。任何异常都退化成"暂无图表"，不影响主流程。"""
    try:
        kind = spec.get("type")
        fn = {
            "histogram": _histogram,
            "bar": _bar,
            "line": _line,
            "scatter": _scatter,
            "scatter_groups": _scatter_groups,
        }.get(kind)
        if fn is None:
            return _empty(width, height, "暂不支持该图表类型")
        return fn(spec, width, height)
    except Exception as exc:                              # noqa: BLE001
        return _empty(width, height, "图表渲染失败：%s" % exc)


# ------------------------------------------------------------------ 各图表
def _histogram(spec, w, h):
    vals = [v for v in (spec.get("values") or []) if v is not None]
    if len(vals) < 2:
        return _empty(w, h, "数据不足以绘制分布图")
    lo, hi = min(vals), max(vals)
    if lo == hi:
        return _empty(w, h, "取值恒定，无法绘制分布")
    bins = _bin_counts(vals, 12)
    counts = [c for _, c in bins]
    ymax = max(counts) or 1
    ticks = _ticks(0, ymax, 4)
    y0 = ticks[0] if ticks else 0
    y1 = ticks[-1] if ticks else ymax
    parts = [_head(w, h, spec.get("title", "")), _axes_frame(w, h)]
    for t in ticks:
        y = _sy(t, y0, y1, h)
        parts.append(_grid_y(y, w))
        parts.append(_text(PAD_L - 8, y + 4, _num(t), size=11, anchor="end", fill=GRAY))
    plot_w = w - PAD_L - PAD_R
    bw = plot_w / float(len(bins))
    for i, (center, c) in enumerate(bins):
        bh = (c / float(y1 - y0)) * (h - PAD_T - PAD_B) if y1 > y0 else 0
        x = PAD_L + i * bw + bw * 0.12
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" '
                     'fill-opacity="0.75" rx="2"/>'
                     % (x, h - PAD_B - bh, bw * 0.76, bh, COLORS[0]))
    # x 轴只标首中尾，避免挤成一团
    for i in (0, len(bins) // 2, len(bins) - 1):
        center = bins[i][0]
        x = PAD_L + (i + 0.5) * bw
        parts.append(_text(x, h - PAD_B + 18, _num(center, 3), size=11, anchor="middle", fill=GRAY))
    parts.append(_text(w / 2.0, h - 8, spec.get("x_label", ""), size=11, anchor="middle", fill=GRAY))
    parts.append(_text(16, h / 2.0, "频数", size=11, anchor="middle", fill=GRAY,
                       transform="rotate(-90 16 %.1f)" % (h / 2.0)))
    parts.append("</svg>")
    return "\n".join(parts)


def _bar(spec, w, h):
    cats = [str(c) for c in (spec.get("categories") or [])]
    vals = spec.get("values") or []
    errors = spec.get("errors") or []
    if not cats or not vals:
        return _empty(w, h, "无数据")
    clean = [v if v is not None else 0 for v in vals]
    vmin, vmax = min(0, min(clean)), max(0, max(clean))
    ticks = _ticks(vmin, vmax, 4)
    y0, y1 = ticks[0], ticks[-1]
    parts = [_head(w, h, spec.get("title", "")), _axes_frame(w, h)]
    for t in ticks:
        y = _sy(t, y0, y1, h)
        parts.append(_grid_y(y, w))
        parts.append(_text(PAD_L - 8, y + 4, _num(t), size=11, anchor="end", fill=GRAY))
    plot_w = w - PAD_L - PAD_R
    bw = plot_w / float(len(cats))
    zero_y = _sy(0, y0, y1, h)
    for i, (c, v) in enumerate(zip(cats, clean)):
        x = PAD_L + i * bw + bw * 0.18
        bwid = bw * 0.64
        y = _sy(v, y0, y1, h)
        top = min(y, zero_y)
        hh = abs(y - zero_y)
        color = COLORS[0] if v >= 0 else COLORS[3]
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" '
                     'fill-opacity="0.82" rx="2"/>' % (x, top, bwid, max(hh, 0.5), color))
        if i < len(errors) and errors[i]:
            se = abs(_sy(errors[i], y0, y1, h) - _sy(0, y0, y1, h))
            cx = x + bwid / 2.0
            parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                         'stroke-width="1.2"/>' % (cx, y - se, cx, y + se, INK))
            parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                         'stroke-width="1.2"/>' % (cx - 4, y - se, cx + 4, y - se, INK))
            parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                         'stroke-width="1.2"/>' % (cx - 4, y + se, cx + 4, y + se, INK))
        parts.append(_text(x + bwid / 2.0, h - PAD_B + 18, _short(c), size=11,
                           anchor="end", fill=GRAY, transform="rotate(-30 %.1f %.1f)"
                           % (x + bwid / 2.0, h - PAD_B + 18)))
    parts.append(_text(16, h / 2.0, spec.get("y_label", ""), size=11, anchor="middle",
                       fill=GRAY, transform="rotate(-90 16 %.1f)" % (h / 2.0)))
    if spec.get("note"):
        parts.append(_text(w - PAD_R, h - 8, spec["note"], size=10, anchor="end", fill=GRAY))
    parts.append("</svg>")
    return "\n".join(parts)


def _line(spec, w, h):
    series = [s for s in (spec.get("series") or []) if s.get("points")]
    if not series:
        return _empty(w, h, "无数据")
    labels = spec.get("x_labels") or None
    xs, ys = [], []
    for s in series:
        for p in s["points"]:
            if p and p[0] is not None and p[1] is not None:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
    if not xs:
        return _empty(w, h, "无有效数据点")
    xt = _ticks(min(xs), max(xs), 5)
    yt = _ticks(min(ys), max(ys), 4)
    x0, x1 = xt[0], xt[-1]
    y0, y1 = yt[0], yt[-1]
    parts = [_head(w, h, spec.get("title", "")), _axes_frame(w, h)]
    for t in yt:
        y = _sy(t, y0, y1, h)
        parts.append(_grid_y(y, w))
        parts.append(_text(PAD_L - 8, y + 4, _num(t), size=11, anchor="end", fill=GRAY))
    for t in xt:
        x = _sx(t, x0, x1, w)
        if labels:
            # 横轴是「2023第2季度」这类文本时，按序号贴标签，不硬转成数字
            idx = int(round(t))
            lab = labels[idx] if 0 <= idx < len(labels) else ""
        else:
            lab = _num(t, 0)
        parts.append(_text(x, h - PAD_B + 18, lab, size=11, anchor="middle", fill=GRAY))
    for i, s in enumerate(series):
        pts = [(p[0], p[1]) for p in s["points"] if p and p[1] is not None]
        if not pts:
            continue
        d = " ".join("%.1f,%.1f" % (_sx(float(p[0]), x0, x1, w), _sy(float(p[1]), y0, y1, h))
                     for p in pts)
        color = COLORS[i % len(COLORS)]
        dash = ' stroke-dasharray="6 4"' if s.get("dashed") else ""
        parts.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2.2" '
                     'stroke-linejoin="round"%s/>' % (d, color, dash))
        for p in pts:
            parts.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="%s"/>'
                         % (_sx(float(p[0]), x0, x1, w), _sy(float(p[1]), y0, y1, h), color))
        parts.append(_text(PAD_L + 4, PAD_T - 18 + i * 16, s.get("name", ""), size=11,
                           anchor="start", fill=color))
    parts.append(_text(w / 2.0, h - 8, spec.get("x_label", ""), size=11, anchor="middle", fill=GRAY))
    parts.append("</svg>")
    return "\n".join(parts)


def _scatter(spec, w, h):
    pts = [p for p in (spec.get("points") or []) if p and p[0] is not None and p[1] is not None]
    if not pts:
        return _empty(w, h, "无数据")
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    xt = _ticks(min(xs), max(xs), 5)
    yt = _ticks(min(ys), max(ys), 4)
    x0, x1, y0, y1 = xt[0], xt[-1], yt[0], yt[-1]
    parts = [_head(w, h, spec.get("title", "")), _axes_frame(w, h)]
    for t in yt:
        y = _sy(t, y0, y1, h)
        parts.append(_grid_y(y, w))
        parts.append(_text(PAD_L - 8, y + 4, _num(t), size=11, anchor="end", fill=GRAY))
    for t in xt:
        parts.append(_text(_sx(t, x0, x1, w), h - PAD_B + 18, _num(t, 3), size=11,
                           anchor="middle", fill=GRAY))
    for x, y in pts:
        parts.append('<circle cx="%.1f" cy="%.1f" r="3.2" fill="%s" fill-opacity="0.55"/>'
                     % (_sx(x, x0, x1, w), _sy(y, y0, y1, h), COLORS[0]))
    fit = spec.get("fit")
    if fit and len(fit) == 2:
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                     'stroke-width="1.8" stroke-dasharray="6 4"/>'
                     % (_sx(fit[0][0], x0, x1, w), _sy(fit[0][1], y0, y1, h),
                        _sx(fit[1][0], x0, x1, w), _sy(fit[1][1], y0, y1, h), COLORS[3]))
    parts.append(_text(w / 2.0, h - 8, spec.get("x_label", ""), size=11, anchor="middle", fill=GRAY))
    parts.append(_text(16, h / 2.0, spec.get("y_label", ""), size=11, anchor="middle", fill=GRAY,
                       transform="rotate(-90 16 %.1f)" % (h / 2.0)))
    parts.append("</svg>")
    return "\n".join(parts)


def _scatter_groups(spec, w, h):
    groups = [g for g in (spec.get("groups") or []) if g.get("points")]
    if not groups:
        return _empty(w, h, "无数据")
    allp = [p for g in groups for p in g["points"] if p]
    xs = [float(p[0]) for p in allp]
    ys = [float(p[1]) for p in allp]
    xt = _ticks(min(xs), max(xs), 5)
    yt = _ticks(min(ys), max(ys), 4)
    x0, x1, y0, y1 = xt[0], xt[-1], yt[0], yt[-1]
    parts = [_head(w, h, spec.get("title", "")), _axes_frame(w, h)]
    for t in yt:
        y = _sy(t, y0, y1, h)
        parts.append(_grid_y(y, w))
        parts.append(_text(PAD_L - 8, y + 4, _num(t), size=11, anchor="end", fill=GRAY))
    for t in xt:
        parts.append(_text(_sx(t, x0, x1, w), h - PAD_B + 18, _num(t, 3), size=11,
                           anchor="middle", fill=GRAY))
    for i, g in enumerate(groups):
        color = COLORS[i % len(COLORS)]
        for p in g["points"]:
            if not p:
                continue
            parts.append('<circle cx="%.1f" cy="%.1f" r="3.4" fill="%s" fill-opacity="0.7"/>'
                         % (_sx(float(p[0]), x0, x1, w), _sy(float(p[1]), y0, y1, h), color))
        parts.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="%s"/>'
                     % (PAD_L + 6 + i * 76, PAD_T - 22, color))
        parts.append(_text(PAD_L + 16 + i * 76, PAD_T - 18, g.get("name", ""), size=11,
                           anchor="start", fill=GRAY))
    parts.append(_text(w / 2.0, h - 8, spec.get("x_label", ""), size=11, anchor="middle", fill=GRAY))
    parts.append(_text(16, h / 2.0, spec.get("y_label", ""), size=11, anchor="middle", fill=GRAY,
                       transform="rotate(-90 16 %.1f)" % (h / 2.0)))
    parts.append("</svg>")
    return "\n".join(parts)


# ------------------------------------------------------------------ 脚手架
def _head(w, h, title):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="100%%" '
            'height="auto" role="img" aria-label="%s">'
            '<rect width="100%%" height="100%%" fill="#ffffff"/>'
            '<text x="%d" y="24" font-family="%s" font-size="13" font-weight="600" '
            'fill="%s">%s</text>' % (w, h, _esc(title), PAD_L - 8, FONT, INK, _esc(title)))


def _axes_frame(w, h):
    return ('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#cbd5e1" stroke-width="1"/>'
            '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#cbd5e1" stroke-width="1"/>'
            % (PAD_L, h - PAD_B, w - PAD_R, h - PAD_B,
               PAD_L, PAD_T, PAD_L, h - PAD_B))


def _grid_y(y, w):
    return ('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1" '
            'stroke-dasharray="3 3"/>' % (PAD_L, y, w - PAD_R, y, GRID))


def _text(x, y, s, size=11, anchor="start", fill=INK, transform=None):
    tr = ' transform="%s"' % transform if transform else ""
    return ('<text x="%.1f" y="%.1f" font-family="%s" font-size="%d" fill="%s" '
            'text-anchor="%s"%s>%s</text>' % (x, y, FONT, size, fill, anchor, tr, _esc(s)))


def _empty(w, h, msg):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="100%%" '
            'height="auto"><rect width="100%%" height="100%%" fill="#f8fafc"/>'
            '<text x="%d" y="%d" font-family="%s" font-size="12" fill="%s" '
            'text-anchor="middle">%s</text></svg>'
            % (w, h, w // 2, h // 2, FONT, GRAY, _esc(msg)))


# ------------------------------------------------------------------ 坐标
def _sx(v, lo, hi, w):
    if hi == lo:
        return PAD_L + (w - PAD_L - PAD_R) / 2.0
    return PAD_L + (v - lo) / float(hi - lo) * (w - PAD_L - PAD_R)


def _sy(v, lo, hi, h):
    if hi == lo:
        return PAD_T + (h - PAD_T - PAD_B) / 2.0
    return h - PAD_B - (v - lo) / float(hi - lo) * (h - PAD_T - PAD_B)


def _ticks(lo, hi, count=5):
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return [0.0, 1.0]
    if not (hi > lo):
        hi = lo + 1.0
    span = hi - lo
    raw = span / float(max(count, 1))
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = None
    for m in (1, 2, 2.5, 5, 10):
        if raw <= mag * m:
            step = mag * m
            break
    if step is None:
        step = mag * 10
    start = math.floor(lo / step) * step
    out = []
    v = start
    i = 0
    while v <= hi + step * 1e-9 and i < 40:
        out.append(round(v, 10))
        i += 1
        v = start + i * step
    if len(out) < 2:
        out = [lo, hi]
    elif out[-1] < hi and len(out) < 40:
        # 关键：坐标轴上界必须覆盖最大值。少了这一步，最大的几个点会画到画布外面
        # （曾经出现过 cy=-65 这种坐标——数据没错，是轴没包住数据）。
        out.append(round(start + len(out) * step, 10))
    return out


def _bin_counts(vals, nbin):
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return [(lo, len(vals))]
    width = (hi - lo) / float(nbin)
    counts = [0] * nbin
    for v in vals:
        idx = int((v - lo) / width)
        if idx >= nbin:
            idx = nbin - 1
        counts[idx] += 1
    return [(lo + (i + 0.5) * width, counts[i]) for i in range(nbin)]


def _num(v, nd=0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) >= 10000 and nd == 0:
        if abs(f) >= 100000000:
            return "%.1f亿" % (f / 100000000.0)
        if abs(f) >= 10000:
            return "%.1f万" % (f / 10000.0)
    if nd == 0 and abs(f) >= 100:
        return "%.0f" % f
    if abs(f) >= 1:
        return ("%." + str(nd) + "f") % f
    if f == 0:
        return "0"
    return ("%." + str(max(nd, 3)) + "f") % f


def _short(s, limit=8):
    s = str(s)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
