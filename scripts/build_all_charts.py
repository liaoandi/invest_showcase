#!/usr/bin/env python3
"""
多品种投资信号走势图 - 单 HTML 输出

从 podcast_juicer_sv101 提取所有高频 ticker 的信号，
逐个叠加在 yfinance 走势图上，输出一个带 tab 切换的单 HTML。

Usage:
    python scripts/build_all_charts.py
    python scripts/build_all_charts.py --min-signals 2
    python scripts/build_all_charts.py --tickers NVDA,GOOGL,META
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import plotly.graph_objects as go
import yfinance as yf

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SV101_OUTPUT = Path.home() / "Desktop/podcast_juicer_sv101/output"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

OUTCOME_COLOR = {
    "verified":     "#5b8a6e",
    "contradicted": "#b06060",
    "partial":      "#8a8a8a",
    "unknown":      "#8a8a8a",
}
OUTCOME_LABEL = {
    "verified":     "判断准确",
    "contradicted": "判断失误",
    "partial":      "待观察",
    "unknown":      "待观察",
}

MARKER_SIZE = 12
PRICE_COLOR = "#7e9bb5"
BG_COLOR = "#f5f3ef"
GRID_COLOR = "#e0dbd4"
TEXT_COLOR = "#4a4a4a"


# ---------------------------------------------------------------------------
# Data loading (from chart_signals.py)
# ---------------------------------------------------------------------------

def load_signals_for_ticker(ticker: str) -> list[dict]:
    """Load all SV101 signals mentioning a given ticker."""
    signals = []

    for sig_file in sorted(SV101_OUTPUT.glob("sv101_ep*/sv101_ep*_verified_signals.json")):
        data = json.loads(sig_file.read_text())
        meta = data.get("metadata", {})

        publish_date = meta.get("record_date") or meta.get("publish_date")
        if not publish_date:
            meta_files = list(sig_file.parent.glob("*_metadata.json"))
            if meta_files:
                m = json.loads(meta_files[0].read_text())
                publish_date = m.get("publish_date") or m.get("record_date")
        if not publish_date:
            continue

        ep_id = meta.get("episode_id", "?")

        guest_bg = {}
        parts_files = list(sig_file.parent.glob("*_participants.json"))
        if parts_files:
            parts_data = json.loads(parts_files[0].read_text())
            guest_bg = parts_data.get("guest_background", {})
        else:
            guest_bg = meta.get("guest_background", {})

        seg_speaker = {}
        transcript_files = list(sig_file.parent.glob("*_transcript_gemini.json"))
        if transcript_files:
            try:
                tr = json.loads(transcript_files[0].read_text())
                for seg in tr.get("segments", []):
                    seg_speaker[seg["id"]] = seg.get("speaker", "")
            except Exception:
                pass

        for sig in data.get("signals", []):
            entities = sig.get("entities", [])
            tickers_in_sig = {(e.get("ticker") or "").upper() for e in entities}
            names_in_sig = {(e.get("name") or "").upper() for e in entities}

            if ticker.upper() not in tickers_in_sig and ticker.upper() not in names_in_sig:
                continue

            verification = sig.get("verification", {})
            v_status = (
                verification.get("verification_status")
                or verification.get("status")
                or "unknown"
            ).lower()
            if "contradict" in v_status:
                v_status = "contradicted"
            elif "partial" in v_status:
                v_status = "partial"
            elif "verif" in v_status:
                v_status = "verified"
            else:
                v_status = "unknown"

            key_quotes = sig.get("key_quotes", [])
            speaker = ""
            if key_quotes:
                seg_id = key_quotes[0].get("seg_id")
                if seg_id is not None:
                    speaker = seg_speaker.get(seg_id, "")
            speaker_with_bg = (
                f"{speaker}（{guest_bg[speaker]}）" if speaker and speaker in guest_bg else speaker
            )

            # Entity display name for this ticker
            entity_names = [e.get("name", "") for e in entities
                           if (e.get("ticker") or "").upper() == ticker.upper()
                           or (e.get("name") or "").upper() == ticker.upper()]

            signals.append({
                "episode": f"E{ep_id}",
                "date": publish_date,
                "speaker": speaker_with_bg,
                "claim": sig.get("claim", ""),
                "signal_type": sig.get("signal_type", ""),
                "verification_status": v_status,
                "key_quotes": key_quotes,
                "entity_name": entity_names[0] if entity_names else ticker,
            })

    signals.sort(key=lambda s: s["date"])
    return signals


def discover_tickers(min_signals: int = 3) -> list[tuple[str, int]]:
    """Find all tickers with at least min_signals signals, sorted by count desc."""
    counts = Counter()
    for sig_file in SV101_OUTPUT.glob("sv101_ep*/sv101_ep*_verified_signals.json"):
        data = json.loads(sig_file.read_text())
        for sig in data.get("signals", []):
            for e in sig.get("entities", []):
                t = e.get("ticker")
                if t and t not in ("null", "unknown"):
                    counts[t] += 1
    return [(t, c) for t, c in counts.most_common() if c >= min_signals]


# ---------------------------------------------------------------------------
# Chart building (one per ticker)
# ---------------------------------------------------------------------------

def build_chart_div(ticker: str, signals: list[dict], hist, info: dict) -> str | None:
    """Build a Plotly chart and return its HTML div string."""
    if hist.empty or not signals:
        return None

    hist.index = hist.index.tz_localize(None) if hist.index.tz else hist.index
    name = info.get("shortName") or info.get("longName") or ticker

    fig = go.Figure()

    # Price line
    fig.add_trace(go.Scatter(
        x=hist.index,
        y=hist["Close"],
        mode="lines",
        name="收盘价",
        line=dict(color=PRICE_COLOR, width=2),
        hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>",
    ))

    # Pre-compute y-range for offset calculation
    y_min = float(hist["Close"].min())
    y_max = float(hist["Close"].max())
    y_offset_unit = (y_max - y_min) * 0.04  # 4% of price range per step

    # Group signals by date to detect overlaps
    from collections import defaultdict
    date_counts = defaultdict(int)
    date_index = {}
    for sig in signals:
        d = sig["date"]
        date_index[id(sig)] = date_counts[d]
        date_counts[d] += 1

    # Signal markers
    for sig in signals:
        try:
            sig_dt = datetime.strptime(sig["date"], "%Y-%m-%d")
        except ValueError:
            continue

        future = hist[hist.index >= sig_dt]
        if future.empty:
            continue
        price_at_signal = float(future["Close"].iloc[0])

        # Offset overlapping markers: spread vertically around the price
        n_same_date = date_counts[sig["date"]]
        if n_same_date > 1:
            idx = date_index[id(sig)]
            # Center the group around the price point
            offset = (idx - (n_same_date - 1) / 2) * y_offset_unit
            price_at_signal += offset

        v_status = sig["verification_status"]
        color = OUTCOME_COLOR.get(v_status, OUTCOME_COLOR["unknown"])
        outcome_label = OUTCOME_LABEL.get(v_status, "待观察")

        speaker = sig.get("speaker", "")
        if speaker and "（" in speaker:
            sp_name, sp_bg = speaker.split("（", 1)
            sp_bg = sp_bg.rstrip("）")
            sp_bg = sp_bg[:18] + "…" if len(sp_bg) > 18 else sp_bg
            speaker_str = f"{sp_name}（{sp_bg}）"
        else:
            speaker_str = speaker

        # Wrap claim text
        claim = sig["claim"]
        wrapped_lines = []
        cur_line = ""
        for ch in claim:
            cur_line += ch
            if ch in "，。；：" and len(cur_line) >= 28:
                wrapped_lines.append(cur_line)
                cur_line = ""
                if len(wrapped_lines) >= 5:
                    break
        if cur_line and len(wrapped_lines) < 5:
            wrapped_lines.append(cur_line)
        claim_html = "<br>".join(wrapped_lines)
        if len(claim) > sum(len(l) for l in wrapped_lines):
            claim_html += "…"

        hover = (
            f"<b style='font-size:1.05em'>{sig['episode']}</b>"
            + f"<span style='color:#999'>　{sig['date']}</span><br>"
            + (f"<span style='color:#777'>{speaker_str}</span><br>" if speaker_str else "")
            + f"<br>{claim_html}<br>"
            + f"<br><span style='color:{color}'><b>{outcome_label}</b></span>"
        )

        fig.add_trace(go.Scatter(
            x=[sig_dt],
            y=[price_at_signal],
            mode="markers",
            name=f"{sig['episode']}",
            marker=dict(
                symbol="circle",
                size=MARKER_SIZE,
                color=color,
                line=dict(color="white", width=1.5),
            ),
            hovertemplate=hover + "<extra></extra>",
            showlegend=False,
        ))

        fig.add_shape(
            type="line",
            x0=sig_dt, x1=sig_dt,
            y0=0, y1=1,
            xref="x", yref="paper",
            line=dict(color=color, width=1, dash="dot"),
            opacity=0.4,
        )

    # Legend
    for outcome, label in [
        ("verified", "判断准确"),
        ("contradicted", "判断失误"),
        ("unknown", "待观察"),
    ]:
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers",
            marker=dict(symbol="circle", size=11,
                        color=OUTCOME_COLOR[outcome],
                        line=dict(color="white", width=1.5)),
            name=label, showlegend=True,
        ))

    date_range_start = min(datetime.strptime(s["date"], "%Y-%m-%d") for s in signals) - timedelta(days=30)
    fig.update_layout(
        title=dict(
            text=f"{name} ({ticker})",
            font=dict(size=18, color=TEXT_COLOR),
            x=0.02,
        ),
        xaxis=dict(
            title="日期",
            range=[date_range_start, hist.index[-1]],
            gridcolor=GRID_COLOR,
            showgrid=True,
            tickfont=dict(color=TEXT_COLOR),
        ),
        yaxis=dict(
            title="收盘价 (USD)",
            gridcolor=GRID_COLOR,
            showgrid=True,
            tickfont=dict(color=TEXT_COLOR),
        ),
        plot_bgcolor=BG_COLOR,
        paper_bgcolor="#faf8f5",
        font=dict(family="'Helvetica Neue', Arial, sans-serif", color=TEXT_COLOR),
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,
            xanchor="right", x=1,
            bgcolor="rgba(0,0,0,0)",
            borderwidth=0,
            font=dict(size=12),
        ),
        hoverlabel=dict(
            bgcolor="#faf8f5",
            bordercolor="#d0ccc6",
            font=dict(size=13, family="'Helvetica Neue', Arial, sans-serif", color="#333"),
        ),
        hovermode="closest",
        height=520,
        margin=dict(l=60, r=30, t=60, b=60),
    )

    return fig.to_html(include_plotlyjs=False, full_html=False, div_id=f"chart-{ticker}")


# ---------------------------------------------------------------------------
# Combined HTML
# ---------------------------------------------------------------------------

def build_combined_html(chart_data: list[dict]) -> str:
    """
    chart_data: list of {ticker, name, signal_count, verified, contradicted, div_html}
    """
    # Group tickers by sector
    SECTOR_MAP = {
        # AI & 科技
        "GOOGL": "tech", "NVDA": "tech", "META": "tech", "MSFT": "tech",
        "AAPL": "tech", "AMD": "tech", "AVGO": "tech", "IBM": "tech",
        "AMZN": "tech", "ORCL": "tech", "TSM": "tech", "PLTR": "tech",
        "CRM": "tech",
        # 新能源 & 前沿
        "TSLA": "frontier", "GEV": "frontier", "OKLO": "frontier",
        "IONQ": "frontier", "RGTI": "frontier", "RKLB": "frontier",
        "QS": "frontier", "LAC": "frontier",
        # 加密 & 金融科技
        "MSTR": "crypto", "COIN": "crypto", "HOOD": "crypto",
        # 金融
        "JPM": "finance", "V": "finance", "BLK": "finance", "MA": "finance",
        # 医药
        "LLY": "pharma", "NVO": "pharma",
        # 消费 & 媒体
        "NFLX": "consumer", "WBD": "consumer", "LULU": "consumer",
        "WMT": "consumer", "ADDYY": "consumer",
        # 中概股
        "BABA": "china", "LI": "china", "0700.HK": "china",
        "1810.HK": "china", "002594.SZ": "china",
        # 欧洲 (奢侈品 & 汽车)
        "MC.PA": "europe", "MC": "europe", "MBG.DE": "europe",
    }

    SECTOR_META = [
        ("tech",     "AI & 科技"),
        ("frontier", "新能源 & 前沿科技"),
        ("crypto",   "加密 & 金融科技"),
        ("finance",  "金融"),
        ("pharma",   "医药"),
        ("consumer", "消费 & 媒体"),
        ("china",    "中概股"),
        ("europe",   "欧洲"),
        ("other",    "其他"),
    ]

    def ticker_group(ticker):
        return SECTOR_MAP.get(ticker, "other")

    # Build chart divs (shared across groups)
    chart_divs = []
    first_ticker = chart_data[0]["ticker"] if chart_data else ""

    for cd in chart_data:
        display = "block" if cd["ticker"] == first_ticker else "none"
        chart_divs.append(
            f'<div class="chart-panel" id="panel-{cd["ticker"]}" style="display:{display}">'
            f'{cd["div_html"]}</div>'
        )

    # Build grouped tabs HTML
    tabs_html_parts = []
    for group_id, group_label in SECTOR_META:
        members = [cd for cd in chart_data if ticker_group(cd["ticker"]) == group_id]
        if not members:
            continue

        default_open = (group_id == "tech")  # Only tech group open by default
        open_class = "open" if default_open else ""
        buttons = []
        for cd in members:
            active = "active" if cd["ticker"] == first_ticker else ""
            total = cd["signal_count"]
            v_pct = round(cd["verified"] / total * 100) if total else 0
            c_pct = round(cd["contradicted"] / total * 100) if total else 0
            buttons.append(
                f'<button class="tab-btn {active}" data-ticker="{cd["ticker"]}">'
                f'<div class="tab-top"><span class="tab-ticker">${cd["ticker"]}</span>'
                f'<span class="tab-count">{total}</span></div>'
                f'<div class="tab-bar">'
                f'<div class="bar-fill bar-green" style="width:{v_pct}%"></div>'
                f'<div class="bar-fill bar-red" style="width:{c_pct}%"></div>'
                f'</div></button>'
            )

        tabs_html_parts.append(
            f'<div class="tab-group">'
            f'<div class="group-header {open_class}" data-group="{group_id}">'
            f'<span class="group-arrow">&#9654;</span>'
            f'<span class="group-label">{group_label}</span>'
            f'<span class="group-count">{len(members)}</span></div>'
            f'<div class="group-body {"" if default_open else "collapsed"}" id="group-{group_id}">'
            f'{"".join(buttons)}</div></div>'
        )

    tabs_html = "\n".join(tabs_html_parts)
    charts_html = "\n".join(chart_divs)

    # Stats
    total_tickers = len(chart_data)
    total_signals = sum(cd["signal_count"] for cd in chart_data)
    total_verified = sum(cd["verified"] for cd in chart_data)
    total_contradicted = sum(cd["contradicted"] for cd in chart_data)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SV101 投资信号走势图</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {{
    --bg: #f5f3ef;
    --card: #faf8f5;
    --border: #e0dbd4;
    --text: #4a4a4a;
    --text2: #7a7672;
    --accent: #7e9bb5;
    --accent-bg: #e8f0f6;
    --green: #5b8a6e;
    --green-bg: #e8f2ec;
    --red: #b06060;
    --red-bg: #f4e8e8;
  }}

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
  }}

  .header {{
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 20px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 16px;
  }}

  .header h1 {{
    font-size: 20px;
    font-weight: 600;
  }}

  .header-stats {{
    display: flex;
    gap: 20px;
    font-size: 13px;
    color: var(--text2);
  }}

  .header-stats .stat {{
    display: flex;
    align-items: baseline;
    gap: 4px;
  }}

  .header-stats .stat-val {{
    font-size: 18px;
    font-weight: 700;
    color: var(--text);
  }}

  .header-stats .stat-val.green {{ color: var(--green); }}
  .header-stats .stat-val.red {{ color: var(--red); }}
  .header-stats .stat-val.blue {{ color: var(--accent); }}

  /* Tabs */
  .tabs {{
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 8px 32px 12px;
  }}

  .tab-group {{
    margin-bottom: 4px;
  }}

  .group-header {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 0;
    cursor: pointer;
    user-select: none;
    width: 100%;
  }}

  .group-header:hover .group-label {{ color: var(--accent); }}

  .group-arrow {{
    font-size: 10px;
    color: var(--text2);
    display: inline-block;
    transition: transform 0.2s;
  }}

  .group-header.open .group-arrow {{ transform: rotate(90deg); }}

  .group-label {{
    font-size: 12px;
    font-weight: 500;
    color: var(--text2);
  }}

  .group-count {{
    font-size: 11px;
    color: var(--text2);
    opacity: 0.6;
  }}

  .group-body {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 4px 0 8px 16px;
  }}

  .group-body.collapsed {{
    display: none;
  }}

  .tab-btn {{
    display: flex;
    flex-direction: column;
    padding: 6px 14px 8px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--bg);
    cursor: pointer;
    transition: all 0.15s;
    min-width: 72px;
  }}

  .tab-btn:hover {{
    border-color: var(--accent);
    background: var(--accent-bg);
  }}

  .tab-btn.active {{
    background: var(--accent-bg);
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent);
  }}

  .tab-top {{
    display: flex;
    align-items: baseline;
    gap: 6px;
  }}

  .tab-ticker {{
    font-size: 14px;
    font-weight: 600;
    color: var(--text);
  }}

  .tab-count {{
    font-size: 11px;
    color: var(--text2);
    font-weight: 400;
  }}

  .tab-bar {{
    display: flex;
    height: 3px;
    border-radius: 2px;
    background: var(--border);
    margin-top: 5px;
    overflow: hidden;
  }}

  .bar-fill {{
    height: 100%;
  }}

  .bar-green {{ background: var(--green); }}
  .bar-red {{ background: var(--red); }}

  /* Charts */
  .chart-container {{
    padding: 20px 32px;
  }}

  .chart-panel {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.04);
  }}

  /* Footer */
  .footer {{
    text-align: center;
    padding: 16px;
    font-size: 11px;
    color: var(--text2);
  }}
</style>
</head>
<body>

<div class="header">
  <h1>SV101 投资信号走势图</h1>
  <div class="header-stats">
    <div class="stat"><span class="stat-val blue">{total_tickers}</span> 品种</div>
    <div class="stat"><span class="stat-val">{total_signals}</span> 信号</div>
    <div class="stat"><span class="stat-val green">{total_verified}</span> 准确</div>
    <div class="stat"><span class="stat-val red">{total_contradicted}</span> 失误</div>
  </div>
</div>

<div class="tabs" id="tabsBar">
  {tabs_html}
</div>

<div class="chart-container">
  {charts_html}
</div>

<div class="footer">
  硅谷101播客 - 投资信号提取与交叉验证 · 数据截至 {datetime.now().strftime("%Y-%m-%d")}
</div>

<script>
// Group toggle
document.querySelectorAll('.group-header').forEach(header => {{
  header.addEventListener('click', () => {{
    header.classList.toggle('open');
    const body = document.getElementById('group-' + header.dataset.group);
    if (body) body.classList.toggle('collapsed');
  }});
}});

// Tab click
document.querySelectorAll('.tab-btn').forEach(btn => {{
  btn.addEventListener('click', (e) => {{
    e.stopPropagation();
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const ticker = btn.dataset.ticker;
    document.querySelectorAll('.chart-panel').forEach(p => p.style.display = 'none');
    const panel = document.getElementById('panel-' + ticker);
    if (panel) {{
      panel.style.display = 'block';
      const plotDiv = panel.querySelector('.plotly-graph-div');
      if (plotDiv) Plotly.Plots.resize(plotDiv);
    }}
  }});
}});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate multi-ticker signal charts")
    parser.add_argument("--min-signals", type=int, default=3,
                        help="Minimum signals to include a ticker (default 3)")
    parser.add_argument("--tickers", type=str, default=None,
                        help="Comma-separated list of tickers (overrides --min-signals)")
    parser.add_argument("--period", default="2y", help="Price history period")
    parser.add_argument("--out", default=None, help="Output HTML path")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else OUTPUT_DIR / "index.html"

    # Determine tickers
    if args.tickers:
        ticker_list = [(t.strip().upper(), 0) for t in args.tickers.split(",")]
    else:
        ticker_list = discover_tickers(args.min_signals)

    print(f"[info] {len(ticker_list)} tickers to process")
    total = len(ticker_list)

    chart_data = []
    failed = []

    for i, (ticker, count) in enumerate(ticker_list):
        pct = (i + 1) / total * 100
        print(f"\n[{i+1}/{total} {pct:.0f}%] Processing ${ticker} ({count} signals)...")

        # Load signals
        signals = load_signals_for_ticker(ticker)
        if len(signals) < args.min_signals:
            print(f"  -> skipped: only {len(signals)} signals (need {args.min_signals})")
            failed.append((ticker, f"only {len(signals)} signals"))
            continue

        # Fetch price
        try:
            print(f"  -> fetching price data...")
            t = yf.Ticker(ticker)
            hist = t.history(period=args.period)
            info = t.info
        except Exception as e:
            print(f"  -> skipped: yfinance error: {e}")
            failed.append((ticker, str(e)))
            continue

        if hist.empty:
            print(f"  -> skipped: no price data")
            failed.append((ticker, "no price data"))
            continue

        # Build chart
        div_html = build_chart_div(ticker, signals, hist, info)
        if not div_html:
            print(f"  -> skipped: chart build failed")
            failed.append((ticker, "chart build failed"))
            continue

        verified = sum(1 for s in signals if s["verification_status"] == "verified")
        contradicted = sum(1 for s in signals if s["verification_status"] == "contradicted")

        chart_data.append({
            "ticker": ticker,
            "name": info.get("shortName") or ticker,
            "signal_count": len(signals),
            "verified": verified,
            "contradicted": contradicted,
            "div_html": div_html,
        })

        print(f"  -> {len(signals)} signals, {verified} verified, {contradicted} contradicted")

    if not chart_data:
        print("\n[error] No charts generated. Exiting.")
        sys.exit(1)

    # Build combined HTML
    print(f"\n[build] Generating combined HTML for {len(chart_data)} tickers...")
    html = build_combined_html(chart_data)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    print(f"\n{'='*50}")
    print(f"[ok] Saved: {out_path}")
    print(f"[ok] File size: {out_path.stat().st_size / 1024:.0f} KB")
    print(f"[ok] {len(chart_data)} tickers, {sum(c['signal_count'] for c in chart_data)} signals")
    if failed:
        print(f"\n[warn] {len(failed)} tickers skipped:")
        for t, reason in failed:
            print(f"  {t}: {reason}")


if __name__ == "__main__":
    main()
