/**
 * Plain-language definitions for the Backtests pages.
 * Verdict wording follows signal_engine/docs/STRATEGY-ACCEPTANCE-CRITERIA.md.
 */

export type GlossaryCategory =
  | 'Basics'
  | 'Evidence'
  | 'Metrics'
  | 'Verdicts and lifecycle'
  | 'Versions and runs'
  | 'Change classes'
  | 'Assumptions'

export interface GlossaryEntry<Id extends string = string> {
  id: Id
  term: string
  short: string
  long: string
  category: GlossaryCategory
}

type Def = readonly [term: string, category: GlossaryCategory, short: string, long: string]

const DEFS = {
  backtest: [
    'Backtest',
    'Basics',
    'A simulated run of a strategy against historical market data.',
    'A backtest replays past market data and applies the strategy rules as if they had been traded then. It shows what the rules would have done under the stated assumptions. It does not promise future results.',
  ],
  dataset: [
    'Dataset',
    'Basics',
    'The market data used by the backtest.',
    'The dataset is the set of historical prices (symbols, bar size and date range) a run was tested on. Two runs on different datasets are not directly comparable.',
  ],
  strategy_version: [
    'Strategy Version',
    'Versions and runs',
    'A frozen definition of strategy rules and parameters.',
    'Once a version is frozen its rules and parameters never change. Any change to the rules or parameters creates a new version, so every result can be traced to exactly what was tested.',
  ],
  run: [
    'Backtest Run',
    'Versions and runs',
    'One execution of one strategy version against a specific dataset and execution context.',
    'A run records the version, dataset, costs, execution assumptions and results. Runs are immutable: to test something different you start a new run rather than editing an old one.',
  ],
  category: [
    'Strategy Category',
    'Versions and runs',
    'A label used to find and group strategies, such as intraday or swing.',
    'Category is only a label for finding strategies. Changing it creates no new version and does not change any run or verdict.',
  ],
  tags: [
    'Tags',
    'Versions and runs',
    'Free-form labels attached to a strategy to help you find it.',
    'Tags are labels for finding strategies. Adding or removing a tag creates no new version and does not change any run, result or verdict.',
  ],
  in_sample: [
    'In-Sample',
    'Evidence',
    'Data used during strategy development or parameter selection.',
    'Results on in-sample data are flattering by construction, because the rules were shaped while looking at it. They are not evidence that the strategy works on new data.',
  ],
  out_of_sample: [
    'Out-of-Sample',
    'Evidence',
    'Data not used during strategy development or parameter selection.',
    'Out-of-sample data was kept unseen while the strategy was built, so results on it are a fairer test of whether the idea generalizes.',
  ],
  walk_forward: [
    'Walk-Forward',
    'Evidence',
    'Repeatedly develop and tune on historical data, then test on the following unseen period.',
    'Walk-forward moves a window through time: tune on one stretch, test on the next, then roll forward and repeat. It mimics how the strategy would have been maintained in real life.',
  ],
  robustness: [
    'Robustness',
    'Evidence',
    'Evidence that results do not depend excessively on one narrow parameter or assumption set.',
    'A robust result survives small changes to parameters, costs, symbols and time periods. A result that only works at one exact setting is fragile.',
  ],
  parameter_sensitivity: [
    'Parameter Sensitivity',
    'Evidence',
    'How much performance changes when strategy parameters are varied.',
    'If a small nudge to a parameter swings results from good to bad, the strategy is sensitive and the good result may be luck. Stable results across nearby settings are more trustworthy.',
  ],
  regime: [
    'Regime',
    'Evidence',
    'A period with different market characteristics, such as trending, ranging, high-volatility or low-volatility conditions.',
    'Strategies often work in one regime and fail in another. Regime breakdowns show whether the result depends on a single kind of market.',
  ],
  overfitting: [
    'Overfitting',
    'Evidence',
    'When a strategy becomes too tailored to historical data and fails to generalize to unseen data.',
    'Trying many variations until one looks good fits noise rather than a real pattern. Overfit strategies look excellent in a backtest and disappoint in live trading, which is why the number of trials is counted.',
  ],
  expectancy: [
    'Expectancy',
    'Metrics',
    'Average amount the strategy makes or loses per trade under the tested assumptions.',
    'Expectancy is the mean result per trade, often shown in R (multiples of the amount risked) or basis points. Net expectancy is measured after costs and is the number that decides whether there is an edge.',
  ],
  profit_factor: [
    'Profit Factor',
    'Metrics',
    'Gross profit divided by gross loss.',
    'Gross profit divided by gross loss. It describes the gross profitability of the trades before considering whether the result is statistically and operationally robust. Above 1 means winners outweighed losers.',
  ],
  max_drawdown: [
    'Maximum Drawdown',
    'Metrics',
    'Largest peak-to-trough decline in equity.',
    'Maximum drawdown is the worst fall from a high point of the equity curve to a later low, before a new high. It tells you the worst pain the tested history would have caused.',
  ],
  sharpe_ratio: [
    'Sharpe Ratio',
    'Metrics',
    'Risk-adjusted return measure relating return to variability.',
    'The Sharpe ratio divides average return by how much returns bounce around. Higher means smoother returns for the same profit, but it can look good on short or lucky samples.',
  ],
  slippage: [
    'Slippage',
    'Metrics',
    'Difference between assumed and actual execution price.',
    'Slippage is the gap between the price you expected and the price you actually got. Cost-stress runs double it to check that the result is not eaten by realistic execution losses.',
  ],
  transaction_costs: [
    'Transaction Costs',
    'Metrics',
    'Brokerage, exchange fees, taxes, and other modeled trading costs.',
    'Costs are charged on every trade and can turn a small gross profit into a net loss. Verdicts are judged on results after costs, never before.',
  ],
  verdict: [
    'Verdict',
    'Verdicts and lifecycle',
    'Research assessment generated from defined evaluation criteria and available evidence.',
    'A verdict is calculated from the gate results, not a personal judgement. Four outcomes exist: EDGE, MARGINAL, NO_EDGE and INCONCLUSIVE.',
  ],
  promotion_gate: [
    'Promotion Gate',
    'Verdicts and lifecycle',
    'Criteria that must be satisfied before a strategy can move to the next research stage.',
    'Gates are fixed pass or fail checks on validity, costs, statistics and robustness. A hard gate failure stops promotion until the evidence improves.',
  ],
  run_verdict: [
    'Run Verdict',
    'Verdicts and lifecycle',
    'The result of one run, calculated from its gate results and never edited afterwards.',
    'Each run gets exactly one verdict, calculated from that run gate results. It is immutable: it describes that run only, and is separate from the research state, the stage and the promotion decision.',
  ],
  research_state: [
    'Research State',
    'Verdicts and lifecycle',
    'Where the strategy stands in research overall, such as being tested, paused or retired.',
    'Research state is about the strategy as a whole and can change over time. It is not a verdict: a verdict belongs to a single run, the research state to the strategy.',
  ],
  stage: [
    'Stage',
    'Verdicts and lifecycle',
    'The step of the research pipeline a strategy has reached, from idea through testing to paper and live.',
    'A strategy moves forward one stage at a time and only after passing the gates for that stage. Stage is separate from any run verdict.',
  ],
  promotion_decision: [
    'Promotion Decision',
    'Verdicts and lifecycle',
    'The decision, based on the latest evidence, on whether a strategy may move to the next stage.',
    'The decision is READY_FOR_NEXT_STAGE, NOT_READY or BLOCKED. It is derived from run verdicts and gate results, and it is separate from the stage the strategy is in now.',
  ],
  ready_for_next_stage: [
    'READY_FOR_NEXT_STAGE',
    'Verdicts and lifecycle',
    'The evidence meets the gates, so the strategy may move to the next stage.',
    'All required gates for the current stage pass. It does not mean the strategy is proven, only that the next stage of testing is justified.',
  ],
  not_ready: [
    'NOT_READY',
    'Verdicts and lifecycle',
    'The evidence is not yet enough to move to the next stage.',
    'Some requirements are unmet or still unrun. More evidence or a better strategy version is needed before promotion.',
  ],
  blocked: [
    'BLOCKED',
    'Verdicts and lifecycle',
    'A hard failure stops the strategy from moving forward.',
    'A validity problem or a hard gate failure blocks promotion. The blocking reason is recorded and must be resolved first.',
  ],
  not_run: [
    'NOT_RUN',
    'Verdicts and lifecycle',
    'A required check or stage has not been run yet.',
    'NOT_RUN means no evidence exists yet. It is not a pass and not a fail, and it is never treated as a favorable result.',
  ],
  verdict_mapping: [
    'Verdict mapping note',
    'Verdicts and lifecycle',
    'EDGE with later stages still to run reads as ready for the next stage, not as proven.',
    'A run verdict of EDGE, while later stages are still to run, reads as ready for the next stage. PROMISING and ROBUST are not verdicts: the only verdicts are EDGE, MARGINAL, NO_EDGE and INCONCLUSIVE.',
  ],
  edge: [
    'EDGE',
    'Verdicts and lifecycle',
    'Calculated verdict: no validity failure, every hard gate passes, and net expectancy is positive after costs.',
    'EDGE is calculated from the gate results, not judged by hand. No validity failure exists, every hard gate G0 to G6 passes, and net expectancy is positive after costs with the statistical requirements met. The verdict is immutable for that run.',
  ],
  marginal: [
    'MARGINAL',
    'Verdicts and lifecycle',
    'Calculated verdict: some positive net evidence, but one or more promotion requirements fail.',
    'MARGINAL is calculated when positive net evidence exists, for example positive out-of-sample net expectancy, but something fails such as cost stress, sample size, parameter stability or symbol dispersion. The failing requirement is recorded.',
  ],
  no_edge: [
    'NO_EDGE',
    'Verdicts and lifecycle',
    'Calculated verdict: valid evidence, but no positive net result after realistic costs.',
    'NO_EDGE is calculated when the evidence is valid and net expectancy is not positive after realistic costs, or positive evidence does not survive validation. It is a real answer, not a failure of the process.',
  ],
  inconclusive: [
    'INCONCLUSIVE',
    'Verdicts and lifecycle',
    'Calculated verdict: a validity failure or too little valid evidence to decide. Not a synonym for failed.',
    'INCONCLUSIVE is calculated when there is a validity failure (look-ahead, invalid data or configuration, malformed strategy, impossible execution assumption) or too little valid evidence. It means we cannot tell, not that the strategy failed.',
  ],
  strategy_change: [
    'STRATEGY_CHANGE',
    'Change classes',
    'The entry, exit or filter logic changed.',
    'A change to the rules that decide which signals become trades. It creates a new strategy version and counts as a new trial.',
  ],
  parameter_change: [
    'PARAMETER_CHANGE',
    'Change classes',
    'A tunable value such as a period or threshold changed.',
    'The rules are the same but a parameter differs. It creates a new version and counts as a trial.',
  ],
  execution_change: [
    'EXECUTION_CHANGE',
    'Change classes',
    'How orders are assumed to be filled changed.',
    'Examples are the fill price rule, order type or holding rules. Results are not comparable to runs with other execution assumptions.',
  ],
  cost_change: [
    'COST_CHANGE',
    'Change classes',
    'The brokerage, tax or slippage assumptions changed.',
    'Only the cost model differs. Net results change while gross results stay the same.',
  ],
  universe_change: [
    'UNIVERSE_CHANGE',
    'Change classes',
    'The set of symbols tested changed.',
    'Adding or removing symbols changes what the result is evidence for. The full tradable universe should be tested rather than a convenient subset.',
  ],
  deployment_profile_change: [
    'DEPLOYMENT_PROFILE_CHANGE',
    'Change classes',
    'The capital, position limits or session rules of the deployment profile changed.',
    'The deployment profile describes how the strategy would actually be run, such as capital and number of concurrent positions. Changing it changes realistic results.',
  ],
  dataset_change: [
    'DATASET_CHANGE',
    'Change classes',
    'The underlying market data changed.',
    'A different or corrected data source or refresh. Results may shift even if everything else is identical.',
  ],
  data_range_change: [
    'DATA_RANGE_CHANGE',
    'Change classes',
    'The start or end dates tested changed.',
    'The same strategy tested on a longer, shorter or shifted period. Results are only comparable over the same range.',
  ],
  engine_change: [
    'ENGINE_CHANGE',
    'Change classes',
    'The backtest engine code changed.',
    'The platform that simulates trades was updated. Results can differ with no change to the strategy, which is why the engine version is recorded.',
  ],
  research_rerun: [
    'RESEARCH_RERUN',
    'Change classes',
    'The same inputs were run again to check or refresh a result.',
    'Nothing material changed. A rerun should reproduce the earlier result and is not counted as a new trial.',
  ],
  reproducible_run: [
    'Reproducible run',
    'Versions and runs',
    'A run whose inputs are all recorded so it can be repeated and give the same result.',
    'Reproducible means the strategy version, parameters, dataset, costs and engine are all recorded, so rerunning gives the same numbers. A run that cannot be reproduced is weaker evidence.',
  ],
  fingerprint: [
    'Fingerprint',
    'Versions and runs',
    'A short code that identifies the exact inputs of a run.',
    'The fingerprint is computed from the strategy, parameters, dataset and settings. Two runs with the same fingerprint used the same inputs; any difference changes it.',
  ],
  source_user: [
    'Source: user',
    'Assumptions',
    'The value was chosen and entered by you.',
    'A user-sourced assumption is something you stated explicitly, such as a capital amount.',
  ],
  source_derived: [
    'Source: derived',
    'Assumptions',
    'The value was calculated from other recorded values.',
    'A derived assumption follows from other inputs, for example a risk amount computed from capital and risk percent.',
  ],
  source_platform_default: [
    'Source: platform default',
    'Assumptions',
    'The value is the platform standard because nothing else was specified.',
    'A platform default was applied because you did not set the value. Check that it fits your case.',
  ],
  source_external: [
    'Source: external',
    'Assumptions',
    'The value came from outside the platform, such as a broker schedule or an exchange rule.',
    'External assumptions come from a third party, for example fee tables or exchange tick sizes, and may change over time.',
  ],
  source_inferred: [
    'Source: inferred',
    'Assumptions',
    'The value was guessed from context rather than stated or measured.',
    'Inferred assumptions are the weakest kind and are flagged so they can be confirmed before the result is relied on.',
  ],
  materiality: [
    'Materiality',
    'Assumptions',
    'Whether an assumption is important enough to change the result.',
    'A material assumption could change the verdict if it were different. Material assumptions are shown prominently and need confirmation.',
  ],
  ledger: [
    'Ledger',
    'Assumptions',
    'The recorded list of assumptions, with their sources, behind a strategy or run.',
    'The assumption ledger keeps each assumption with where it came from and how much it matters, so nothing hidden affects a result. The trials ledger similarly counts every evaluated change.',
  ],
  data_quality: [
    'Data Quality',
    'Evidence',
    'The audit of the market data a run was tested on: gaps, bad rows, split days and dropped symbols.',
    'Before a run is trusted, the data is audited for missing sessions, impossible or duplicate rows and split days. A warning that affects validity makes the run INCONCLUSIVE, because the result may be an artifact of bad data rather than of the strategy.',
  ],
  validity_checks: [
    'Validity Checks',
    'Evidence',
    'Integrity checks run on the run itself: data quality, configuration, lineage and the look-ahead check.',
    'Each check is shown as passed, failed or did not run. A check that did not run is not a pass: it simply provides no assurance either way.',
  ],
  lookahead_check: [
    'Look-ahead (Truncation) Check',
    'Evidence',
    'Re-runs the signals on truncated data to prove they do not use the future.',
    'The check cuts the data off earlier and confirms the same signals appear. It covers single-symbol inputs derived from the price frame only. Regime or cross-sectional inputs are not covered by it.',
  ],
  lineage: [
    'Lineage',
    'Evidence',
    'The chain from a signal to its order, fills and position, so any trade can be traced back.',
    'Lineage follows a trade through signal, order, entry fill, exit fill or fills and position, with the ids and times the engine recorded. It lets you see exactly why a trade exists.',
  ],
  exclusion: [
    'Exclusion',
    'Evidence',
    'A signal that did not become a trade, with the reason code that stopped it.',
    'Every signal is either accepted or excluded with a code, such as the daily trade limit or the entry cutoff. Exclusions are counted so no signal disappears without a reason.',
  ],
  reconciliation: [
    'Reconciliation',
    'Evidence',
    'The check that every signal, order and fill is accounted for in the ledgers.',
    'Reconciliation confirms that signals equal accepted plus excluded, orders equal filled plus dropped, and fills match trades. Any problem is listed so the counts can be trusted or questioned.',
  ],
  trial_ledger: [
    'Trial Ledger',
    'Evidence',
    'The count of variants tried, kept in trials.yaml, used to judge how lucky a result could be.',
    'Material trials are variants of this idea that changed results; external trials come from outside ideas. The more variants tried, the higher the bar a result must clear, so the ledger is read from the record, never estimated.',
  ],
  cost_components: [
    'Modelled Cost Components',
    'Metrics',
    'A breakdown of a realistic per-trade cost into brokerage, taxes, fees and slippage.',
    'The headline net result uses the engine flat cost. The modelled components show where a realistic cost comes from, in basis points of entry notional, averaged over trades. Neither figure is called right or wrong.',
  ],
  trade_subsets: [
    'Trade Subsets',
    'Evidence',
    'Slices of the trade list that back a gate: all, in-sample, out-of-sample, losing trades or gap exits.',
    'A gate result is built from a particular group of trades. Showing the subset lets you inspect the exact trades behind a number instead of trusting the summary.',
  ],
} as const satisfies Record<string, Def>

export type GlossaryTermId = keyof typeof DEFS

export const GLOSSARY: Record<GlossaryTermId, GlossaryEntry<GlossaryTermId>> = Object.fromEntries(
  (Object.entries(DEFS) as [GlossaryTermId, Def][]).map(([id, [term, category, short, long]]) => [
    id,
    { id, term, short, long, category },
  ])
) as Record<GlossaryTermId, GlossaryEntry<GlossaryTermId>>

export const GLOSSARY_IDS = Object.keys(DEFS) as GlossaryTermId[]

export function getTerm(id: string | null | undefined): GlossaryEntry<GlossaryTermId> | undefined {
  if (!id || !Object.hasOwn(GLOSSARY, id)) return undefined
  return GLOSSARY[id as GlossaryTermId]
}
