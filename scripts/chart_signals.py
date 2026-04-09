#!/usr/bin/env python3
"""
投资信号走势图生成器

从 podcast_juicer_sv101 提取特定股票的投资信号，
叠加在 yfinance 价格走势图上，用 Plotly 输出交互式 HTML。

Usage:
    python scripts/chart_signals.py --ticker NVDA
    python scripts/chart_signals.py --ticker GOOGL --period 2y
    python scripts/chart_signals.py --ticker NVDA --out output/charts/nvda.html
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import plotly.graph_objects as go
import yfinance as yf

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SV101_OUTPUT = Path.home() / "Desktop/podcast_juicer_sv101/output"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output/charts"

OUTCOME_COLOR = {
    "verified":     "#5b8a6e",  # Morandi green
    "contradicted": "#b06060",  # Morandi red
    "partial":      "#8a8a8a",  # grey
    "unknown":      "#8a8a8a",  # grey
}
OUTCOME_LABEL = {
    "verified":     "判断准确",
    "contradicted": "判断失误",
    "partial":      "待观察",
    "unknown":      "待观察",
}

MARKER_SIZE = 12


# ---------------------------------------------------------------------------
# Data loading
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

        # Participants (guests + background)
        guest_bg = {}
        parts_files = list(sig_file.parent.glob("*_participants.json"))
        if parts_files:
            parts_data = json.loads(parts_files[0].read_text())
            guest_bg = parts_data.get("guest_background", {})
        else:
            guest_bg = meta.get("guest_background", {})

        # Transcript segments indexed by id for speaker lookup
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

            # Identify the speaker of the first key quote
            key_quotes = sig.get("key_quotes", [])
            speaker = ""
            if key_quotes:
                seg_id = key_quotes[0].get("seg_id")
                if seg_id is not None:
                    speaker = seg_speaker.get(seg_id, "")
            speaker_with_bg = (
                f"{speaker}（{guest_bg[speaker]}）" if speaker and speaker in guest_bg else speaker
            )

            signals.append({
                "episode": f"E{ep_id}",
                "date": publish_date,
                "speaker": speaker_with_bg,
                "claim": sig.get("claim", ""),
                "signal_type": sig.get("signal_type", ""),
                "verification_status": v_status,
                "key_quotes": key_quotes,
            })

    # Sort by date
    signals.sort(key=lambda s: s["date"])
    return signals


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def build_chart(ticker: str, period: str = "2y") -> go.Figure:
    signals = load_signals_for_ticker(ticker)
    if not signals:
        raise ValueError(f"No signals found for {ticker}")

    print(f"[data] {len(signals)} signals for {ticker}")

    # Fetch price data
    print(f"[data] Fetching {ticker} price history ({period})...")
    t = yf.Ticker(ticker)
    hist = t.history(period=period)
    if hist.empty:
        raise ValueError(f"No price data for {ticker}")

    hist.index = hist.index.tz_localize(None)
    info = t.info
    name = info.get("shortName") or info.get("longName") or ticker

    # Morandi color palette
    price_color = "#7e9bb5"
    bg_color = "#f5f3ef"
    grid_color = "#e0dbd4"
    text_color = "#4a4a4a"

    fig = go.Figure()

    # Price line
    fig.add_trace(go.Scatter(
        x=hist.index,
        y=hist["Close"],
        mode="lines",
        name="收盘价",
        line=dict(color=price_color, width=2),
        hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>",
    ))

    # Signal markers
    for sig in signals:
        try:
            sig_dt = datetime.strptime(sig["date"], "%Y-%m-%d")
        except ValueError:
            continue

        # Find closest price on or after signal date
        future = hist[hist.index >= sig_dt]
        if future.empty:
            continue
        price_at_signal = float(future["Close"].iloc[0])

        v_status = sig["verification_status"]
        color = OUTCOME_COLOR.get(v_status, OUTCOME_COLOR["unknown"])
        outcome_label = OUTCOME_LABEL.get(v_status, "待观察")

        # Build hover card
        speaker = sig.get("speaker", "")
        if speaker and "（" in speaker:
            sp_name, sp_bg = speaker.split("（", 1)
            sp_bg = sp_bg.rstrip("）")
            sp_bg = sp_bg[:18] + "…" if len(sp_bg) > 18 else sp_bg
            speaker_str = f"{sp_name}（{sp_bg}）"
        else:
            speaker_str = speaker

        # Claim: wrap at sentence boundaries, up to 5 lines (~150 chars)
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

        # Vertical dashed line
        fig.add_shape(
            type="line",
            x0=sig_dt, x1=sig_dt,
            y0=0, y1=1,
            xref="x", yref="paper",
            line=dict(color=color, width=1, dash="dot"),
            opacity=0.4,
        )

    # Legend: 3 items max
    for outcome, label in [
        ("verified",     "判断准确"),
        ("contradicted", "判断失误"),
        ("unknown",      "待观察"),
    ]:
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers",
            marker=dict(symbol="circle", size=11,
                        color=OUTCOME_COLOR[outcome],
                        line=dict(color="white", width=1.5)),
            name=label, showlegend=True,
        ))

    # Layout
    date_range_start = min(datetime.strptime(s["date"], "%Y-%m-%d") for s in signals) - timedelta(days=30)
    fig.update_layout(
        title=dict(
            text=f"{name} ({ticker}) · SV101 投资信号追踪",
            font=dict(size=18, color=text_color),
            x=0.02,
        ),
        xaxis=dict(
            title="日期",
            range=[date_range_start, hist.index[-1]],
            gridcolor=grid_color,
            showgrid=True,
            tickfont=dict(color=text_color),
        ),
        yaxis=dict(
            title="收盘价 (USD)",
            gridcolor=grid_color,
            showgrid=True,
            tickfont=dict(color=text_color),
        ),
        plot_bgcolor=bg_color,
        paper_bgcolor="#faf8f5",
        font=dict(family="'Helvetica Neue', Arial, sans-serif", color=text_color),
        legend=dict(
            orientation="v",
            yanchor="top", y=0.99,
            xanchor="left", x=1.01,
            bgcolor="rgba(250,248,245,0.9)",
            bordercolor=grid_color,
            borderwidth=1,
            title=dict(text="验证状态", font=dict(size=11, color=text_color)),
        ),
        annotations=[dict(
            text="圆圈大小 = 置信度",
            xref="paper", yref="paper",
            x=1.01, y=0.52,
            xanchor="left", yanchor="top",
            showarrow=False,
            font=dict(size=10, color="#999999"),
        )],
        hoverlabel=dict(
            bgcolor="#faf8f5",
            bordercolor="#d0ccc6",
            font=dict(size=13, family="'Helvetica Neue', Arial, sans-serif", color="#333"),
        ),
        hovermode="closest",
        height=520,
        margin=dict(l=60, r=140, t=60, b=60),
    )

    return fig, signals


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="NVDA", help="Stock ticker, e.g. NVDA")
    parser.add_argument("--period", default="2y", help="Price history period, e.g. 1y 2y")
    parser.add_argument("--out", default=None, help="Output HTML path")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else OUTPUT_DIR / f"{args.ticker.lower()}_signals.html"

    fig, signals = build_chart(args.ticker, args.period)
    fig.write_html(str(out_path), include_plotlyjs="cdn")

    print(f"\n[ok] Chart saved: {out_path}")
    print(f"[ok] {len(signals)} signals plotted")

    # Summary
    from collections import Counter
    status_counts = Counter(s["verification_status"] for s in signals)
    for status, count in status_counts.most_common():
        print(f"  {status}: {count}")


if __name__ == "__main__":
    main()
