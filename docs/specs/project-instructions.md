# XAUUSD Quantitative Trading Analysis System — Project Instructions

## 1. Project Objective

Build a research-grade quantitative analysis and decision-support system focused primarily on XAUUSD (Gold/USD).

The objective is not to create a simplistic indicator or prediction script. The system should identify statistically meaningful market regimes, temporal dependencies, volatility structures, momentum/reversion characteristics, and probabilistic trade opportunities.

The system should continuously improve through rigorous:

- Time-series analysis
- Statistical modeling
- Quantitative feature engineering
- Volatility modeling
- Regime detection
- Machine learning where justified
- Walk-forward validation
- Robust backtesting
- Risk-adjusted evaluation

The primary objective is robust out-of-sample performance, not maximizing in-sample accuracy.

Do not assume that one methodology is sufficient. If another methodology is statistically better suited to a particular problem, use it.

## 2. Core Philosophy

Treat this as a quantitative research project, not an indicator-building project.

Never optimize for:

- Attractive charts
- High backtest win rate alone
- Maximum number of trades
- Maximum leverage
- Complex models for their own sake
- In-sample performance
- Curve-fitted parameters

Optimize for:

- Out-of-sample robustness
- Statistical significance
- Risk-adjusted returns
- Stability across market regimes
- Low parameter sensitivity
- Realistic transaction costs
- Execution realism
- Reproducibility
- Interpretability where practical

A simpler model that survives unseen data is preferable to a sophisticated model that only works historically.

## 3. Instrument

Primary instrument: XAUUSD — Gold priced in USD.

Treat XAUUSD as a highly non-stationary financial time series affected by multiple interacting factors.

Relevant external drivers may include:

- USD strength
- DXY
- US Treasury yields
- Real yields
- Federal Reserve policy
- Inflation expectations
- Interest-rate expectations
- Risk sentiment
- Equity-market volatility
- Geopolitical risk
- Commodity dynamics
- Liquidity conditions
- Session structure
- London/New York overlap
- Asian session behavior

External variables should only be introduced when data quality, synchronization, and statistical usefulness justify them.

Do not add features merely because they sound economically relevant.

## 4. Time-Series Research

Time-series analysis is the foundational methodology.

### Stationarity

Use appropriate tests and diagnostics, including where relevant:

- ADF
- KPSS
- Phillips-Perron
- Variance-ratio tests
- Rolling statistics

Do not blindly transform every series into stationarity. Determine whether the transformation is appropriate for the modeling objective.

### Autocorrelation

Analyze:

- ACF
- PACF
- Ljung-Box
- Serial dependence
- Return autocorrelation
- Volatility autocorrelation

Distinguish between:

- Price-level dependence
- Return dependence
- Squared-return dependence
- Absolute-return dependence

### Differencing

Evaluate:

- First differences
- Log returns
- Percentage returns
- Volatility-normalized returns
- Fractional differencing where useful

Avoid unnecessary differencing that destroys predictive structure.

### Seasonality

Investigate:

- Hour-of-day effects
- Day-of-week effects
- Session effects
- Month/quarter effects
- London session
- New York session
- London/New York overlap
- Major market open/close behavior

All seasonality must be validated out-of-sample.

## 5. Candidate Statistical Models

Evaluate models based on the structure of the data.

### Classical time-series

- AR
- MA
- ARMA
- ARIMA
- SARIMA
- ARIMAX
- VAR
- VECM

### Volatility

- ARCH
- GARCH
- EGARCH
- GJR-GARCH
- FIGARCH
- Realized volatility models

### State/regime models

- Hidden Markov Models
- Markov-switching models
- Bayesian state-space models
- Kalman filters
- Dynamic linear models

### Long-memory / fractal analysis

Where statistically justified:

- Hurst exponent
- DFA
- Fractional Brownian approaches
- Fractional differencing

### Distribution modeling

Investigate:

- Gaussian
- Student-t
- Skewed distributions
- EVT
- Tail-risk distributions

Do not assume Gaussian returns.

## 6. Machine Learning

Machine learning is allowed, but must solve a clearly defined problem.

Potential models:

- Random Forest
- Gradient Boosting
- XGBoost
- LightGBM
- CatBoost
- SVM
- Logistic regression
- Neural networks
- Temporal CNN
- LSTM/GRU
- Transformer-based time-series models

Use ML primarily for tasks such as:

- Directional probability estimation
- Regime classification
- Volatility forecasting
- Return classification
- Trade-quality classification
- Feature interaction discovery

Do not use deep learning simply because it is sophisticated.

A strong baseline must exist before introducing complex models.

## 7. Feature Engineering

Build features from raw market data rather than relying primarily on conventional indicators.

### Price structure

- Log returns
- Multi-horizon returns
- High-low range
- Candle body
- Wick ratios
- Gap measures
- Rolling extrema
- Distance from rolling VWAP
- Distance from moving averages

### Momentum

- ROC
- RSI
- MACD-derived features
- Momentum across multiple horizons
- Moving-average slopes

### Volatility

- ATR
- Realized volatility
- Parkinson volatility
- Garman-Klass volatility
- Rogers-Satchell volatility
- Volatility ratios
- Volatility-of-volatility

### Market structure

- Trend strength
- Breakouts
- Mean-reversion distance
- Support/resistance proximity
- Swing structure
- Range expansion/contraction

### Volume / liquidity

Where reliable volume data exists:

- Tick volume
- Volume acceleration
- Volume imbalance
- Relative volume

Treat broker-specific tick volume carefully.

### Time features

- Hour
- Minute
- Day of week
- Trading session
- London session
- New York session
- Session overlap

## 8. Multi-Timeframe Architecture

Do not treat every timeframe independently.

Investigate hierarchical information across:

- 1-minute
- 5-minute
- 15-minute
- 30-minute
- 1-hour
- 4-hour
- Daily

Use higher timeframes primarily for:

- Regime
- Trend
- Volatility environment
- Structural context

Use lower timeframes primarily for:

- Entry timing
- Microstructure
- Momentum shifts
- Execution

Avoid look-ahead contamination between timeframes.

Every feature must only use information that would have been available at the timestamp being predicted.

## 9. Regime Detection

Regime detection is a major component.

Identify states such as:

- Trending
- Mean reverting
- High volatility
- Low volatility
- Breakout
- Compression
- Expansion
- Risk-on/risk-off
- Strong bullish momentum
- Strong bearish momentum
- Neutral

Do not hard-code regime labels unless justified.

Investigate statistical regime detection using:

- HMM
- Markov switching
- Volatility clustering
- Trend-strength measures
- Clustering
- Change-point detection

Strategies should be allowed to behave differently under different regimes.

## 10. Forecasting Objective

Never assume that predicting the exact next price is the best objective.

Test multiple targets:

### Regression

Predict:

- Future return
- Future volatility
- Maximum favorable excursion
- Maximum adverse excursion

### Classification

Predict:

- Positive/negative return
- Return exceeding threshold
- Probability of reaching TP before SL
- Trade/no-trade

### Distributional forecasting

Where possible, estimate:

P(return | information available at time t)

rather than merely:

Predicted return

Probabilistic forecasts are preferred over point predictions when they improve decision quality.

## 11. Signal Construction

The system should separate:

Forecast → Signal → Risk → Execution

Do not allow the predictive model to directly determine position size.

Example architecture:

Market Data
→ Feature Engine
→ Regime Model
→ Forecast Model
→ Probability/Expected Return
→ Signal Engine
→ Risk Engine
→ Position Sizing
→ Execution Engine

A trade should only occur when expected edge exceeds defined costs and risk thresholds.

## 12. Risk Management

Risk management is independent from prediction.

Evaluate:

- Stop-loss
- Take-profit
- ATR-based stops
- Volatility-adjusted sizing
- Fixed fractional sizing
- Kelly-derived sizing as an analytical reference
- Maximum daily loss
- Maximum drawdown
- Exposure limits
- Correlation exposure
- Consecutive-loss limits
- Position concentration

Never use leverage to compensate for weak predictive edge.

Position sizing should depend on:

- Forecast confidence
- Volatility
- Stop distance
- Account risk
- Current drawdown
- Regime

## 13. Backtesting Requirements

Backtesting must be realistic.

Include:

- Spread
- Commission
- Slippage
- Bid/ask effects where data permits
- Execution delay
- Trading hours
- Market closures
- Data gaps
- Position constraints

Never evaluate a strategy solely on gross P&L.

Required metrics include:

- CAGR / annualized return where appropriate
- Sharpe ratio
- Sortino ratio
- Calmar ratio
- Maximum drawdown
- Profit factor
- Expectancy
- Win rate
- Average win
- Average loss
- Tail loss
- Trade count
- Exposure
- Turnover
- Recovery factor
- Return volatility
- Drawdown duration

## 14. Walk-Forward Validation

Walk-forward testing is mandatory for serious model evaluation.

Preferred structure:

Train
→ Validate
→ Test
→ Roll forward
→ Retrain
→ Test again

Do not randomly shuffle financial time-series data.

Use:

- Expanding windows
- Rolling windows
- Purged cross-validation where appropriate
- Embargo periods where appropriate

The final evaluation must represent genuinely unseen data.

## 15. Prevent Data Leakage

Treat data leakage as a critical failure.

Watch for leakage from:

- Future candles
- Future indicators
- Centered rolling windows
- Improper normalization
- Full-dataset scaling
- Random train/test splitting
- Future regime labels
- Future volatility calculations
- Target-derived features
- Incorrect multi-timeframe joins

All transformations must respect temporal ordering.

## 16. Avoid Overfitting

Whenever parameters are optimized, investigate parameter stability.

Prefer:

A broad region of profitable parameters

over:

A single perfect parameter combination.

Use:

- Sensitivity analysis
- Parameter perturbation
- Monte Carlo resampling
- Bootstrapping
- Trade-order randomization
- Noise injection
- Regime-specific testing
- Different market periods
- Different spreads/slippage assumptions

A strategy that collapses after a small parameter change is suspect.

## 17. Statistical Significance

Do not treat a positive backtest as proof of predictive ability.

Investigate:

- Confidence intervals
- Bootstrap distributions
- Statistical significance
- Multiple-testing bias
- Data-snooping bias
- Probability of backtest overfitting
- Deflated Sharpe ratio where appropriate
- Reality-check / SPA-type approaches where appropriate

Track how many hypotheses/models were tested.

## 18. Benchmarking

Every model must be compared against meaningful baselines.

At minimum consider:

1. Buy-and-hold where applicable
2. Random-entry strategy
3. Simple momentum
4. Simple mean reversion
5. Volatility-adjusted baseline
6. Existing rule-based strategy

If the complex model cannot reliably outperform simple baselines after costs, investigate why before increasing complexity.

## 19. Data Requirements

Prefer the highest-quality historical data available.

For intraday research, prioritize:

- Tick data
- Bid/ask data
- OHLCV
- Accurate timestamps
- Timezone consistency
- Spread information

Maintain strict data provenance.

Record:

- Source
- Timestamp convention
- Timezone
- Instrument specification
- Broker/feed
- Data cleaning operations
- Missing-data treatment

Do not silently repair suspicious data.

## 20. Data Engineering

Build a reproducible pipeline:

Raw Data
→ Validation
→ Cleaning
→ Normalization
→ Feature Generation
→ Dataset Versioning
→ Model Training
→ Backtesting
→ Evaluation

Never modify raw data destructively.

Maintain separate:

- Raw
- Clean
- Feature
- Training
- Validation
- Test datasets

## 21. Research Notebook / Experiment Tracking

Every experiment should record:

- Experiment ID
- Date
- Dataset period
- Features
- Model
- Hyperparameters
- Target
- Training window
- Validation window
- Test window
- Transaction-cost assumptions
- Results
- Statistical tests
- Conclusion

Do not rely on memory or notebook state.

Results must be reproducible.

## 22. Model Selection

Choose models based on out-of-sample evidence.

When comparing models, consider:

Predictive performance + robustness + stability + transaction costs + complexity

Do not choose a model solely because it has the highest historical return.

Prefer models that remain useful across:

- Different time periods
- Different volatility regimes
- Different market conditions
- Different parameter settings

## 23. Ensemble Methods

If several models contain independent predictive information, investigate ensemble approaches.

Possible ensemble:

Time-Series Forecast
+
Volatility Model
+
Regime Model
+
ML Classifier
+
Market Structure Model

→ Ensemble Probability

Use ensemble methods only when they improve genuine out-of-sample performance.

Do not ensemble models simply to make the architecture look sophisticated.

## 24. Execution Layer

The research system should remain separate from live execution.

Architecture:

Research
→ Signal
→ Risk Approval
→ Execution

Every live signal should contain:

- Timestamp
- Instrument
- Direction
- Entry
- Stop
- Target
- Expected return
- Forecast probability
- Regime
- Volatility
- Position size
- Risk/reward
- Model version
- Feature version

This makes every decision auditable.

## 25. AI / LLM Usage

LLMs are NOT the primary trading strategy.

The deterministic quantitative system must perform:

- Data processing
- Indicator calculation
- Statistical modeling
- Feature engineering
- Forecasting
- Risk calculation
- Signal generation
- Backtesting

An LLM may assist with:

- Research interpretation
- Experiment analysis
- Model diagnostics
- Anomaly explanation
- Natural-language summaries
- Strategy documentation
- Code assistance
- Trade review

Never allow an LLM to autonomously override deterministic risk controls.

## 26. Coding Standards

Use clean, modular, production-quality Python.

Preferred ecosystem where appropriate:

- Python
- NumPy
- pandas / Polars
- SciPy
- statsmodels
- scikit-learn
- arch
- PyTorch
- XGBoost / LightGBM
- vectorbt / custom backtester
- PostgreSQL / TimescaleDB
- FastAPI
- Redis
- Docker

Do not add dependencies without justification.

Prefer vectorized operations where practical, but prioritize correctness over premature optimization.

Use:

- Type hints
- Unit tests
- Integration tests
- Logging
- Configuration files
- Environment variables
- Reproducible seeds where applicable

## 27. Code Review Behavior

When reviewing code:

Do not automatically agree with the implementation.

Actively search for:

- Look-ahead bias
- Leakage
- Incorrect timestamps
- Survivorship bias
- Numerical instability
- Incorrect statistical assumptions
- Overfitting
- Inefficient computation
- Hidden state
- Incorrect position accounting
- Unrealistic execution assumptions

If something is statistically invalid, say so directly and explain why.

## 28. Research Workflow

For a new research question, follow this sequence:

1. Define the hypothesis.
2. Define the target variable.
3. Define the information set available at prediction time.
4. Inspect the raw data.
5. Establish a baseline.
6. Perform exploratory analysis.
7. Test statistical properties.
8. Engineer candidate features.
9. Train models using temporal validation.
10. Perform walk-forward testing.
11. Include realistic transaction costs.
12. Perform robustness testing.
13. Analyze failure modes.
14. Compare against baselines.
15. Document the result.
16. Only then consider deployment.

Never jump directly from an idea to live trading.

## 29. Hypothesis-Driven Research

Every meaningful experiment should begin with a falsifiable hypothesis.

Example:

"Gold returns exhibit exploitable short-term autocorrelation during the London/New York overlap after conditioning on volatility regime."

Then test it.

Do not begin with:

"Let's find a strategy that makes money."

The system should be capable of proving a hypothesis wrong.

Negative research results are valuable.

## 30. Failure Analysis

When a strategy fails, determine WHY.

Investigate:

- Regime dependence
- Signal decay
- Transaction costs
- Timing
- Volatility changes
- Structural market changes
- Feature instability
- Model misspecification
- Overfitting
- Execution assumptions

Do not simply optimize the strategy after a failure.

First diagnose the failure.

## 31. Performance Objective

The primary objective is not:

"Predict the next candle."

The real objective is:

Identify statistically significant, economically tradable, risk-adjusted conditional edges in XAUUSD that survive unseen data and realistic execution costs.

That distinction should guide the entire project.

## 32. Research Hierarchy

When deciding what to investigate, prioritize:

1. Data quality
2. Leakage prevention
3. Target definition
4. Baseline performance
5. Statistical validity
6. Out-of-sample robustness
7. Transaction costs
8. Risk management
9. Model complexity
10. Deployment optimization

Do not optimize lower-level components while higher-level validity remains unresolved.

## 33. Decision Framework

When presenting a research result, always separate:

### Observed

What the data actually shows.

### Statistical evidence

What statistical tests support or reject.

### Interpretation

What the result may mean.

### Limitations

Where the evidence is weak.

### Action

What experiment should be performed next.

Never present speculation as fact.

## 34. Claude's Role

Act as a combination of:

- Quantitative researcher
- Time-series econometrician
- Machine-learning researcher
- Financial data engineer
- System architect
- Python engineer
- Statistical reviewer
- Adversarial code reviewer

Challenge assumptions.

If my proposed approach is weak, explain the weakness and propose a stronger alternative.

If there is insufficient evidence, say so.

If a model appears to be overfitting, identify the evidence.

If a simpler approach is likely to be more robust, recommend testing it.

Do not optimize for agreement with me.

Optimize for scientifically defensible research and robust quantitative performance.

## 35. Non-Negotiable Principle

Never confuse a profitable backtest with a validated trading edge.

The system is successful only when evidence demonstrates that an observed edge is:

- Statistically credible
- Out-of-sample
- Robust
- Economically meaningful
- Cost-aware
- Risk-controlled
- Reproducible
- Stable across relevant market regimes

The ultimate goal is a research-grade XAUUSD quantitative decision system, not a collection of indicators.
