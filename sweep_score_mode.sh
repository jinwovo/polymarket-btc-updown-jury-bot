#!/bin/bash
# Score-based entry sweep: compare AND-gate (current) vs score-based entry
# Run on VPS: bash sweep_score_mode.sh
#
# Score system (max 10 points):
#   VWAP agree: +2, BB extreme: +1, Low drift: +1, BTC still: +1,
#   Late entry(>150s): +1, Low ask(<=0.45): +2, High EV(>=30%): +1, High conf(>=0.7): +1
#
# Usage: bash sweep_score_mode.sh [hours]  (default: 120)

HOURS=${1:-120}
STAKE=100

echo "========================================"
echo " SCORE MODE SWEEP - ${HOURS}h, \$${STAKE}/trade"
echo "========================================"
echo ""

# 1) Baseline: current AND-gate (BB+VWAP+drift+btc_still+start100+ask0.50)
echo "--- [1/8] BASELINE: AND-gate (current production) ---"
python paper_replay.py --last-hours $HOURS --stake $STAKE \
    --no-lag-arb --require-bb-extreme --require-vwap-agree \
    --max-ask-drift 0.08 --entry-start 100 --max-ask 0.50 \
    2>/dev/null | grep -E "Trades:|Win rate:|Total PnL:|Profit factor:|Return:"
echo ""

# 2) AND-gate relaxed: no BB, no btc_still (keep VWAP+drift)
echo "--- [2/8] AND-gate relaxed: VWAP+drift only ---"
PAPER_REQUIRE_BTC_STILL_MOVING=false \
python paper_replay.py --last-hours $HOURS --stake $STAKE \
    --no-lag-arb --require-vwap-agree \
    --max-ask-drift 0.08 --entry-start 100 --max-ask 0.50 \
    2>/dev/null | grep -E "Trades:|Win rate:|Total PnL:|Profit factor:|Return:"
echo ""

# 3) AND-gate wider: VWAP only, ask0.55, start80
echo "--- [3/8] AND-gate wide: VWAP only, ask0.55, start80 ---"
PAPER_REQUIRE_BTC_STILL_MOVING=false \
python paper_replay.py --last-hours $HOURS --stake $STAKE \
    --no-lag-arb --require-vwap-agree \
    --entry-start 80 --max-ask 0.55 \
    2>/dev/null | grep -E "Trades:|Win rate:|Total PnL:|Profit factor:|Return:"
echo ""

# 4) No filters at all (just gate_allow + timing)
echo "--- [4/8] NO FILTERS (gate_allow + timing only) ---"
PAPER_REQUIRE_BTC_STILL_MOVING=false \
python paper_replay.py --last-hours $HOURS --stake $STAKE \
    --no-lag-arb --entry-start 80 --max-ask 0.58 \
    2>/dev/null | grep -E "Trades:|Win rate:|Total PnL:|Profit factor:|Return:"
echo ""

# 5) Score mode threshold=4 (loose)
echo "--- [5/8] SCORE MODE: threshold=4 (loose) ---"
PAPER_REQUIRE_BTC_STILL_MOVING=false \
python paper_replay.py --last-hours $HOURS --stake $STAKE \
    --no-lag-arb --score-mode --score-threshold 4 \
    --score-entry-start 80 --score-max-ask 0.55 \
    2>/dev/null | grep -E "Trades:|Win rate:|Total PnL:|Profit factor:|Return:"
echo ""

# 6) Score mode threshold=5 (balanced)
echo "--- [6/8] SCORE MODE: threshold=5 (balanced) ---"
PAPER_REQUIRE_BTC_STILL_MOVING=false \
python paper_replay.py --last-hours $HOURS --stake $STAKE \
    --no-lag-arb --score-mode --score-threshold 5 \
    --score-entry-start 80 --score-max-ask 0.55 \
    2>/dev/null | grep -E "Trades:|Win rate:|Total PnL:|Profit factor:|Return:"
echo ""

# 7) Score mode threshold=6 (tight)
echo "--- [7/8] SCORE MODE: threshold=6 (tight) ---"
PAPER_REQUIRE_BTC_STILL_MOVING=false \
python paper_replay.py --last-hours $HOURS --stake $STAKE \
    --no-lag-arb --score-mode --score-threshold 6 \
    --score-entry-start 80 --score-max-ask 0.55 \
    2>/dev/null | grep -E "Trades:|Win rate:|Total PnL:|Profit factor:|Return:"
echo ""

# 8) Score mode threshold=5, wider ask (0.58)
echo "--- [8/8] SCORE MODE: threshold=5, ask0.58, start80 ---"
PAPER_REQUIRE_BTC_STILL_MOVING=false \
python paper_replay.py --last-hours $HOURS --stake $STAKE \
    --no-lag-arb --score-mode --score-threshold 5 \
    --score-entry-start 80 --score-max-ask 0.58 \
    2>/dev/null | grep -E "Trades:|Win rate:|Total PnL:|Profit factor:|Return:"
echo ""

echo "========================================"
echo " SWEEP COMPLETE"
echo "========================================"
echo ""
echo "Key comparison:"
echo "  - [1] = current production (likely few trades)"
echo "  - [5-8] = score mode (should have MORE trades)"
echo "  - Best = highest PnL with PF > 1.3"
