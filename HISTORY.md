# Trading Bot Change History

코드/설정 변경 이력과 거래 성과를 함께 기록하여 어떤 변경이 성과에 영향을 미쳤는지 추적합니다.
Trade ID 기준으로 어떤 로직이 적용되었는지 추적합니다.

---

## Algorithm Version by Trade ID

| Trade ID 범위 | 적용 로직 | 비고 |
|---------------|-----------|------|
| Paper #1 ~ #3 | Diffusion + 3 judge (2/3), EV_FIRST, no macro filter | 03-13 첫 거래 |
| Paper #4 ~ #20 | 위와 동일, env overrides (느슨한 진입) | 03-14, 대부분 DOWN 편향 손실 |
| Paper #21 | 위와 동일 (서버 재시작 전) | 03-15, hard_adverse_flush 조기청산 |
| Paper #22 ~ #24 | Cautious Calibration + Chainlink보정 + 만장일치(3/3) + macro trend filter, exit_policy 여전히 공격적 | 03-15 변경 직후, trailing_stop이 여전히 수익 깎음 |
| Paper #25 ~ #38 | hold-to-expiry **의도**했으나 코드 clamp 버그로 exit policy 여전히 공격적 (trailing ≤35%, stop_loss ≥-45%) | 03-15~03-16, env=999/-85 설정했으나 코드가 무시 |
| Paper #39~ | **clamp 버그 수정 완료**: trailing/profit_take=999 실제 적용, stop_loss=-85 실제 적용 + BET_PCT_MAX 20% | 03-16~ |
| Live #1 ~ #9 | Diffusion + 3 judge (2/3), adaptive 5-15% sizing | 03-12, 67% 승률 |
| Live #10 ~ #12 | env overrides 적용 후 | 03-13, 0% 승률 |
| Live #13 ~ #14 | 위와 동일 | 03-14, 0% 승률 |
| Live #15 ~ #28 | Paper와 동일한 새 로직 적용 예정 | 미적용 |
| Live #29~ | jury=3/3 + parity mode | 03-16, 1T |

> **사용법**: 새 알고리즘/설정 변경 시 현재 마지막 Trade ID를 확인하고, "Paper #N까지는 구 로직, #N+1부터 신 로직" 형태로 기록할 것.

---

## 2026-03-16

### 변경사항 (Paper #39~)
- **EXIT POLICY CLAMP 버그 수정** (`paper_trade_sim.py`) — Paper 손실의 근본 원인
  - `max(-45.0, env_value)` → env에서 -85 설정해도 -45로 강제됨 → **제거**
  - `min(35.0, env_value)` → env에서 999 설정해도 35로 강제됨 → **제거**
  - Paper #25~#38: 25/38건이 조기 exit, -$877 손실 (exit policy 때문)
  - Expiry까지 홀드한 13건은 62% WR, +$332 → 백테스트와 일치
- **백테스트 속도 최적화** (`backtest.py`)
  - `_get_btc_price`, `_get_odds_at`: O(n) linear scan → O(log n) binary search
  - 886K ticks에서 ~10x 속도 향상, 12 combo sweep 7분 완료
- **파라미터 sweep 실행** — entry 최적화 여지 분석
  - lag_edge (0.005~0.030): **효과 없음** — min_edge=0.06이 이미 더 strict
  - min_edge (0.03~0.12): **효과 없음** — 259 trades 전부 높은 expected_roi
  - min_roi (0.010~0.025): **효과 없음** — AGGRESSIVE 모드가 이미 relaxation
  - jury=3 vs jury=2: jury=3 우수 (PF 1.50 vs 1.35, PnL $6,944 vs $6,780)
  - **결론: 현재 entry 파라미터 최적. 259 trades는 jury 3/3 + boundary distance로 결정**
- **베팅 사이즈 최적화** (`risk_manager.py`)
  - BET_PCT_MAX: 15% → 20% (sweep 결과: PnL +$768, drawdown 동일)
  - max_bet_size가 실제 바인딩 제약 ($200→$400이면 PnL 거의 2배, 오더북 유동성 의존)
- **sweep 인프라 개선** (`backtest.py`)
  - `--auto-sweep`에 lag_edge sweep 추가 (env var override)
  - `--size-sweep` 옵션 추가 (bet sizing 파라미터 sweep)
  - sweep에서도 max_bet_size ceiling 적용 (기존 $5 cap 버그 수정)

### 거래 성과 (03-16 KST)
| 구분 | 건수 | 승률 | PnL |
|------|------|------|-----|
| Paper | 5T (1W/4L) | 20% | -$320.46 |
| Live | 1T (0W/1L) | 0% | -$6.13 |

### 분석
- Paper 5건 중 4건 expiry settlement, 1건 stop_loss 조기청산
  - #37: stop_loss(roi=-45.68%) — **clamp 버그**로 env의 -85% 대신 -45%에 청산됨
  - 이 거래가 clamp 없었으면 홀드했을 것 (실제 outcome=DOWN이라 어차피 손실이지만)
- Paper 전체 (#1~#38): 12W/26L = 31.6% WR, -$544.67
  - Expiry 홀드 13건: 8W/5L = **62% WR**, +$332
  - 조기 exit 25건: outcome 불명이나 -$877 손실
- **핵심: exit policy clamp가 hold-to-expiry 전략을 무력화시킴**

---

## 2026-03-15

### 변경사항 (2차 — HOLD-TO-EXPIRY 전환, Paper #25~)
- **EXIT POLICY 근본 변경** — Binary market hold-to-expiry 전략
  - **trailing_stop 비활성** (999%): 이기는 거래를 +3~45%에 잘라내던 문제 해결
  - **profit_take 비활성** (999%): binary market에서 settlement=$1이 최대 수익
  - **break_even_protect 비활성** (999%): 아직 이길 수 있는 거래를 포기하던 문제 해결
  - **opposite_prob_surge**: 0.78 → 0.90, 확인 횟수 3 → 5회
  - **hard_adverse_flush**: opposite ≥ 0.93, remaining ≤ 90초일 때만
  - **stop_loss**: -40% → -85% (거의 비활성)
  - **min_elapsed**: 35 → 60초
- **핵심 발견**: 백테스트는 hold-to-expiry인데 paper는 exit_policy가 공격적으로 조기청산
  - 백테스트: 65.6% WR, +$5,067 (hold-to-expiry)
  - Paper #1~#24: 25% WR, -$387 (exit_policy가 수익 깎고 승률 떨어뜨림)
  - 이기는 거래: 백테스트 avg +$140 vs paper avg +$55 (trailing_stop)
  - 지는 거래로 분류됨: exit이 "아직 이길 수 있는" 거래를 손실 처리

### 변경사항 (1차 — Paper #22~#24)
- **Cautious Calibration 적용** (`judges.py`)
  - `_close_prob_from_diffusion()`, `estimate_ensemble_close_probability()`에 time-adaptive shrinkage
  - `shrinkage = 0.75 + 0.17 * progress` (초반 0.75 → 만기 0.92)
  - 과신 방지: raw probability를 0.5 방향으로 수축
- **Chainlink 가격 보정** (`binance_ws.py`, `data_collector.py`)
  - `ChainlinkCalibrator`: Polygon on-chain BTC/USD 폴링 (27초 heartbeat)
  - `adjusted_price = binance - offset` (offset = binance@chainlink_update - chainlink)
  - Polymarket settlement 기준(Chainlink)에 더 가까운 가격으로 판단
- **Unanimous jury** (`env/runtime.public.env`)
  - `JURY_THRESHOLD=3` (2/3 → 3/3 만장일치)
- **Macro trend filter** (`paper_trade_sim.py`)
  - 15분 lookback, 0.04% 이상 반대 트렌드면 진입 차단
- **hard_adverse_flush 시간 조건 추가** (`exit_policy.py`)
  - remaining > 120초면 발동 안 함 (opposite_ask ≥ 0.92만 예외)
  - Trade #21: DOWN 맞았지만 22초만에 -69%로 잘려서 손실 → 이 문제 해결
- **Exit policy 보수화** (`env/runtime.public.env`)
  - `MIN_ELAPSED_SEC`: 20 → 35초 (최소 보유 시간 증가)
  - `OPPOSITE_ASK`: 0.68 → 0.78 (반대 prob surge 기준 상향)
- **MariaDB 단일화**: SQLite 코드 전부 제거 (`db_config.py`, `data_collector.py`, `main.py`, `dashboard_server.py`, `backtest.py`)
- **Daily Loss Limit**: Seed Capital의 40% 기준, KST 자정 초기화
- **Seed Capital UI 즉시 반영**: Save 후 refreshLiveStatus(), 새로고침 시 서버값 로드

### 거래 성과 (KST 기준)
| 날짜 | Paper | Live |
|------|-------|------|
| 03-15 | 1T, 0W/1L, PnL -$52.14 | - |

### 분석
- Trade #21 (03-15 17:28): DOWN @0.44, 22초 후 hard_adverse_flush로 -69% 손실
  - 실제 윈도우 결과는 DOWN → 방향 맞았지만 조기 청산으로 손실
  - 원인: 윈도우 초반 odds 급변 + min_elapsed 20초가 너무 짧음
  - 대응: hard_adverse_flush에 remaining>120s 억제 + min_elapsed 35초로 상향

---

## 2026-03-14

### 변경사항 (이전 세션)
- env 설정 다수 조정 (이전 커밋 기반 추정)
- EV_FIRST 모드 적용
- PAPER_MAX_ENTRY_PRICE=0.52

### 거래 성과
| 구분 | 건수 | 승률 | PnL |
|------|------|------|-----|
| Paper | 17T (4W/13L) | 24% | -$208.93 |
| Live | 2T (0W/2L) | 0% | -$3.21 |

### 분석
- Paper 17건 중 대부분 DOWN 진입 → UP 시장에서 역방향 베팅 반복
- 10/11 DOWN 편향 문제 식별 → macro trend filter 필요성 확인
- entry_price가 0.37~0.49로 낮아서 변동성 리스크 과대

---

## 2026-03-13

### 변경사항
- `33467af` Add live telemetry/auth UX updates and stabilize trading guards

### 거래 성과
| 구분 | 건수 | 승률 | PnL |
|------|------|------|-----|
| Paper | 3T (1W/2L) | 33% | -$95.98 |
| Live | 3T (0W/3L) | 0% | -$12.52 |

---

## 2026-03-12

### 변경사항
- `33467af` Live telemetry/auth UX, trading guards 안정화
- `9eaf7c2` Live entry lag-edge gating, backtest sweep 정렬

### 거래 성과
| 구분 | 건수 | 승률 | PnL |
|------|------|------|-----|
| Live | 9T (6W/3L) | 67% | +$17.99 |

### 분석
- Live 최고 성과 구간. 67% 승률로 수익 달성
- 이후 설정 변경(env overrides)으로 성과 하락

---

## 2026-03-11

### 변경사항
- `9eaf7c2` Live entry lag-edge gating 개선
- `be5b830` Post-settlement exit flow, live auth UI 간소화

---

## 2026-03-10

### 변경사항
- `e863427` Aggressive profit mode (live+paper)
- `aa9bbca` Probability-first entry gate, adaptive paper stop-loss
- `04cf1ae` UP-only regime meta filter
- `99c6515` Live auth modal
- `b3a0d12` Live adaptive sizing mode (5-15% of equity)
- `5fd4d49` Paper entry strictness 조정
- `82dbe4a` Opposite-implied guard 0.62로 완화 ← **성과 하락 원인 추정**

### 분석
- Paper $1000 → $2000 달성 구간 (초반)
- 이후 env overrides로 entry 느슨해짐: MIN_EXPECTED_ROI 4%→2.5%, MIN_SUPPORT_RATIO 0.70→0.50
- 느슨한 설정이 이후 연패의 원인

---

## 2026-03-09

### 변경사항
- `c361176` Jury weighting by market regime, early-exit defaults
- `9931a6f` Judge feed 통일 (fixed bars), BTC 5m chart
- `fe78b70` Fast-lane judge bypass for lag-arb
- `ae561ba` Statistical judge + jump-robust regime model
- `2d77742` Diffusion-based mispricing judge 추가

### 분석
- 핵심 아키텍처 확립: diffusion model + 3 judge ensemble
- Echo chamber 문제 발견: 9개 judge → 3개로 축소 (Statistical, Arbitrage, Orderbook)

---

## 2026-03-08

### 변경사항
- `3ec753c` Signal gating + paper filter sync
- `c26b6c8` Paper entry quality filter 강화
- `507cd9d` Adaptive conservatism (30m trade lock 제거)
- `3d5b37f` Ultra-conservative paper (gap + drawdown stops)
- `7fbcb04` Paper equity-based sizing + stricter entry

---

## Key Lessons Log

| 날짜 | 교훈 |
|------|------|
| 03-10 | Judge 9개→3개: echo chamber 제거, consensus 품질 ↑ |
| 03-10 | MIN_EXPECTED_ROI/MIN_SUPPORT_RATIO 낮추면 noise 진입 ↑ |
| 03-14 | DOWN 편향: macro trend filter 없으면 역추세 반복 진입 |
| 03-15 | hard_adverse_flush 22초 조기청산: 방향 맞아도 odds 급변에 손실 |
| 03-15 | entry_price 0.37~0.44: 너무 낮으면 변동성 리스크 극대화 |
| 03-15 | Cautious Calibration: 모델 과신 억제 필수 |
| 03-15 | **Binary market에서 조기 청산 = 수익 파괴**. trailing_stop이 이기는 거래를 +3~45%에 자르면, 만기 +127% 수익 대부분 날림. hold-to-expiry가 정답 |
| 03-15 | 백테스트와 실거래 exit 전략 불일치 → 백테스트 결과 신뢰 불가. 동일 전략이어야 비교 가능 |
| 03-16 | **코드 clamp가 env 설정 무시**: `min(35, env)` / `max(-45, env)` 같은 safety clamp가 hold-to-expiry 설정(999/-85)을 덮어씀. env만 바꾸면 안 되고 코드의 clamp도 확인해야 함 |
| 03-16 | Entry 파라미터(lag_edge, min_edge, min_roi) 전부 non-binding — 실제 gate는 jury 3/3 consensus + boundary distance. 수익 극대화 레버는 bet sizing(max_bet_size)뿐 |
| 03-16 | 백테스트 속도: pandas `.abs().idxmin()` O(n) → `np.searchsorted` O(log n)로 10x 개선. sweep 가능해짐 |
