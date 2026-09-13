"""
Streamlit frontend for the backtest engine (engine/backtest/) -- separate
from dashboard.py (the tkinter app for the extraction/AI pipeline) because
this needs real charts (equity curve, drawdown) and a browser-based look,
which tkinter can't give well. Run with:

    streamlit run frontend/app.py

(or double-click Launch_Backtest_Frontend.command in the project root).

Two pages, matching the roadmap in PROJECT memory: Backtest (functional
now) and Live Trading (a placeholder -- no broker has been chosen yet, so
there's nothing real to show there; see engine/backtest/broker.py's
docstring for how a LiveBroker will eventually plug into the exact same
Strategy code this page already runs).
"""

import importlib
import inspect
import pkgutil
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "engine"))

from backtest.data_feed import CSVDataFeed, YFinanceDataFeed
from backtest.engine import BacktestEngine
from backtest.metrics import compute_metrics, trade_log
from backtest.signals import check_latest_signal
from backtest.strategy import Strategy
from backtest.telegram_alert import send_alert
import backtest.strategies as strategies_pkg

MARKET_DATA_DIR = PROJECT_DIR / "market_data"

st.set_page_config(page_title="TradingIntelligence Backtest", page_icon="📈", layout="wide")


def discover_strategy_classes() -> dict:
    found = {}
    for _, module_name, _ in pkgutil.iter_modules(strategies_pkg.__path__):
        module = importlib.import_module(f"backtest.strategies.{module_name}")
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module.__name__:
                found[name] = obj
    return found


def discover_local_symbols() -> list:
    if not MARKET_DATA_DIR.exists():
        return []
    return sorted(p.stem for p in MARKET_DATA_DIR.glob("*.csv"))


def equity_chart(equity: pd.Series) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=equity.index, y=equity.values, mode="lines", name="Equity",
        line=dict(color="#00C805", width=1.6),
        hovertemplate="%{x|%Y-%m-%d}<br>₹%{y:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        title="Equity Curve", height=340, margin=dict(l=10, r=10, t=40, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#FAFAFA"), xaxis=dict(gridcolor="#262A34"), yaxis=dict(gridcolor="#262A34"),
    )
    return fig


def drawdown_chart(equity: pd.Series) -> go.Figure:
    drawdown = (equity / equity.cummax() - 1) * 100
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=drawdown.index, y=drawdown.values, mode="lines", name="Drawdown", fill="tozeroy",
        line=dict(color="#FF4B4B", width=1.2),
        hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        title="Drawdown (%)", height=220, margin=dict(l=10, r=10, t=40, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#FAFAFA"), xaxis=dict(gridcolor="#262A34"), yaxis=dict(gridcolor="#262A34"),
    )
    return fig


def style_trade_log(df: pd.DataFrame):
    def color_pnl(val):
        if not isinstance(val, (int, float)):
            return ""
        return f"color: {'#00C805' if val >= 0 else '#FF4B4B'}"
    return df.style.applymap(color_pnl, subset=["pnl", "return_pct"]).format({
        "entry_price": "{:.2f}", "exit_price": "{:.2f}", "pnl": "{:.2f}", "return_pct": "{:.2f}%",
    })


def render_backtest_page():
    strategy_classes = discover_strategy_classes()
    local_symbols = discover_local_symbols()

    st.title("📈 Strategy Backtest")

    with st.sidebar:
        st.header("Configuration")
        if not strategy_classes:
            st.error("No Strategy subclass found under engine/backtest/strategies/.")
            st.stop()
        strategy_name = st.selectbox("Strategy", list(strategy_classes.keys()))
        source = st.radio("Data source", ["Local (market_data/)", "Yahoo Finance (live download)", "Arrow (your account)"])

        arrow_interval = "day"
        if source.startswith("Local"):
            if not local_symbols:
                st.warning("No local data yet -- run engine/backtest/fetch_market_data.py first.")
            symbols = st.multiselect("Symbols", local_symbols, default=local_symbols[:1])
        elif source.startswith("Yahoo"):
            raw = st.text_input("Symbols (comma-separated Yahoo tickers)", "^NSEI")
            symbols = [s.strip() for s in raw.split(",") if s.strip()]
        else:
            st.caption("Uses arrow_credentials.local.json -- fill that file in yourself first (never share it).")
            raw = st.text_input("Symbols (NIFTY, BANKNIFTY, or a stock symbol)", "NIFTY")
            symbols = [s.strip() for s in raw.split(",") if s.strip()]
            arrow_interval = st.selectbox(
                "Interval", ["min", "3min", "5min", "10min", "15min", "30min",
                             "hour", "2hours", "3hours", "4hours", "day", "week", "month"],
                index=10,
            )

        col1, col2 = st.columns(2)
        start = col1.date_input("Start", date(2015, 1, 1))
        end = col2.date_input("End", date.today())
        starting_cash = st.number_input("Starting cash (₹)", min_value=1000.0, value=1_000_000.0, step=10000.0)

        run_clicked = st.button("▶ Run Backtest", type="primary", use_container_width=True)

    if run_clicked:
        if not symbols:
            st.error("Select at least one symbol.")
            st.stop()
        if source.startswith("Local"):
            data_feed = CSVDataFeed(str(MARKET_DATA_DIR))
        elif source.startswith("Yahoo"):
            data_feed = YFinanceDataFeed()
        else:
            from backtest.live_data_feed_arrow import ArrowDataFeed
            try:
                data_feed = ArrowDataFeed(interval=arrow_interval)
            except Exception as e:
                st.error(f"Couldn't connect to Arrow: {type(e).__name__}: {e}")
                st.stop()
        with st.spinner(f"Running {strategy_name} on {', '.join(symbols)}..."):
            try:
                engine = BacktestEngine(
                    data_feed=data_feed, symbols=symbols,
                    start=start.isoformat(), end=end.isoformat(),
                    strategy_cls=strategy_classes[strategy_name], starting_cash=starting_cash,
                )
                result = engine.run()
                st.session_state["bt_result"] = result
                st.session_state["bt_metrics"] = compute_metrics(result)
                st.session_state["bt_log"] = trade_log(result)
            except Exception as e:
                st.error(f"Backtest failed: {type(e).__name__}: {e}")
                st.stop()

    result = st.session_state.get("bt_result")
    metrics = st.session_state.get("bt_metrics")
    log = st.session_state.get("bt_log")

    if result is None:
        st.info("Configure a backtest in the sidebar and click **Run Backtest**.")
        return

    if metrics.get("num_trades", 0) == 0:
        st.warning("No trades were closed during this backtest.")
        return

    st.caption(f"{result.strategy_name} · {', '.join(result.symbols)} · starting cash ₹{result.starting_cash:,.0f}")

    kpis = st.columns(6)
    kpis[0].metric("Trades", metrics["num_trades"])
    kpis[1].metric("Win Rate", f"{metrics['win_rate']:.1%}")
    kpis[2].metric("Profit Factor", f"{metrics['profit_factor']:.2f}")
    kpis[3].metric("Total Return", f"{metrics.get('total_return_pct', 0):.2f}%")
    kpis[4].metric("Max Drawdown", f"{metrics.get('max_drawdown_pct', 0):.2f}%")
    kpis[5].metric("Sharpe", f"{metrics.get('sharpe_ratio', 0):.2f}")

    equity = result.equity_series()
    st.plotly_chart(equity_chart(equity), use_container_width=True)
    st.plotly_chart(drawdown_chart(equity), use_container_width=True)

    st.subheader(f"Trade Log ({len(log)} trades)")
    st.dataframe(style_trade_log(log), use_container_width=True, height=360)


def render_signals_page():
    strategy_classes = discover_strategy_classes()
    local_symbols = discover_local_symbols()

    st.title("🔔 Signals")
    st.caption(
        "Checks whether a strategy would place a NEW entry today, given the most recent data. "
        "This never places a real order -- confirming below only logs the decision locally "
        "(and optionally sends a Telegram message), same alert-then-confirm pattern as the "
        "reference video's Telegram bot integration. Wiring this to a real broker is a separate, "
        "later step (see the Live Trading page)."
    )

    if not strategy_classes:
        st.error("No Strategy subclass found under engine/backtest/strategies/.")
        return
    if not local_symbols:
        st.warning("No local data yet -- run engine/backtest/fetch_market_data.py first.")
        return

    col1, col2 = st.columns(2)
    strategy_name = col1.selectbox("Strategy", list(strategy_classes.keys()), key="sig_strategy")
    symbols = col2.multiselect("Symbols", local_symbols, default=local_symbols[:2], key="sig_symbols")

    if st.button("Check for Signal", type="primary"):
        data_feed = CSVDataFeed(str(MARKET_DATA_DIR))
        try:
            signals = check_latest_signal(strategy_classes[strategy_name], symbols, data_feed)
            st.session_state["signals"] = signals
        except Exception as e:
            st.error(f"Signal check failed: {type(e).__name__}: {e}")
            return

    signals = st.session_state.get("signals")
    if signals is None:
        st.info("Click **Check for Signal** to see whether the selected strategy would act today.")
        return
    if not signals:
        st.success("No new-entry signal right now for the selected strategy/symbols.")
        return

    for i, sig in enumerate(signals):
        with st.container(border=True):
            st.markdown(f"**{sig.description}**")
            c1, c2 = st.columns(2)
            if c1.button("Log confirmation (no order placed)", key=f"confirm_{i}"):
                from datetime import datetime
                log_path = PROJECT_DIR / "logs" / "confirmed_signals.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a") as f:
                    f.write(f"[{datetime.now().isoformat(timespec='seconds')}] CONFIRMED (not executed): {sig.description}\n")
                st.success("Logged locally. No real order was placed -- no live broker is wired up yet.")
            if c2.button("Send Telegram alert", key=f"telegram_{i}"):
                sent = send_alert(sig.description)
                if sent:
                    st.success("Sent via Telegram.")
                else:
                    st.info("Telegram not configured (or send failed) -- logged to logs/alerts_local.log instead. "
                            "Fill in telegram_credentials.local.json to enable real notifications.")


def render_live_page():
    st.title("🔴 Live Trading")
    st.warning(
        "Not built yet -- intentionally. This page will exist once a broker is chosen "
        "(the market/broker decision is still open) and its API is wired up as a "
        "`LiveBroker` implementing the exact same interface `SimulatedBroker` does "
        "(see `engine/backtest/broker.py`'s module docstring) -- so a strategy that's "
        "already been backtested here runs live with no strategy-code changes."
    )
    st.markdown("""
**Gates before this page does anything real**, per the agreed roadmap:
1. Every open question on a strategy's spec resolved into a precise, computable rule
2. Backtested over full history, in/out-of-sample, parameter-stress-tested
3. Go/no-go criteria (decided in advance) cleared
4. Paper-traded on live data for weeks-to-months
5. Only then: small-size live rollout, with kill criteria set in advance

No strategy has cleared step 1 yet.
""")


page = st.sidebar.radio("View", ["Backtest", "Signals", "Live Trading"], label_visibility="collapsed")
if page == "Backtest":
    render_backtest_page()
elif page == "Signals":
    render_signals_page()
else:
    render_live_page()
