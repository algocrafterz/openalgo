import type { CoverageClass, SessionCoverage } from '@/api/backtestResults'
import {
  COVERAGE_LABEL,
  classCounts,
  missingOfExpected,
  NOT_RECORDED_FOR_RUN,
  pct,
} from './engineBlocks'
import { InfoTip } from './InfoTip'

const SEVERITY: Record<CoverageClass, number> = {
  SEVERE_GAPS: 0,
  MATERIAL_GAPS: 1,
  MINOR_GAPS: 2,
  COMPLETE: 3,
}

/** The record's note without the internal reason code. */
const plainNote = (note: string) => note.replace(/\s*\(SESSION_AFTER_GAP\)/g, '')

/**
 * Level 5: every listed symbol, worst first, with the missing-session ranges. Collapsed until
 * opened. Whole missing sessions and bars missing inside sessions are separate columns.
 */
export function SessionCoverageTable({ coverage }: { coverage: SessionCoverage | null }) {
  if (!coverage) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="session-coverage">
        Coverage by symbol: {NOT_RECORDED_FOR_RUN}.
      </p>
    )
  }
  const rows = [...coverage.symbols].sort(
    (a, b) =>
      SEVERITY[a.class] - SEVERITY[b.class] ||
      (b.missing_full_sessions.share ?? 0) - (a.missing_full_sessions.share ?? 0) ||
      a.symbol.localeCompare(b.symbol)
  )
  const basis = coverage.session_basis.used === 'common' ? 'common sessions' : 'own sessions'
  const dropped = coverage.effective.sessions_excluded_by_basis.count
  return (
    <div className="space-y-2" data-testid="session-coverage">
      <p className="text-sm">
        Session basis: {basis}
        <InfoTip term="common_session_basis" />. {coverage.effective.sessions_kept} sessions kept
        {dropped > 0 ? `, ${dropped} dropped by this basis` : ''}.
      </p>
      {missingOfExpected(coverage) && (
        <p className="text-sm" data-testid="coverage-denominator">
          Missing whole sessions: {missingOfExpected(coverage)}, counted after leaving out sessions
          that are out of scope or were excluded as probable splits. Symbols:{' '}
          {classCounts(coverage)}.
        </p>
      )}
      <details className="rounded-md border" data-testid="session-coverage-details">
        <summary className="cursor-pointer select-none p-2 text-sm font-medium">
          Coverage by symbol ({rows.length} listed, worst first)
        </summary>
        <div className="space-y-2 overflow-x-auto p-2">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="text-xs text-muted-foreground">
                <th className="pr-3">Symbol</th>
                <th className="pr-3">Coverage</th>
                <th className="pr-3">Sessions loaded</th>
                <th className="pr-3">Expected sessions</th>
                <th className="pr-3">Whole sessions missing</th>
                <th className="pr-3">Missing share</th>
                <th className="pr-3">Missing session ranges</th>
                <th className="pr-3">Sessions with missing bars</th>
                <th>Missing bars</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const partial = row.partial_sessions
                return (
                  <tr
                    key={row.symbol}
                    className="align-top"
                    data-testid={`coverage-row-${row.symbol}`}
                  >
                    <td className="pr-3 font-mono">{row.symbol}</td>
                    <td className="pr-3">
                      {COVERAGE_LABEL[row.class]} ({pct(row.coverage_share)})
                    </td>
                    <td className="pr-3 font-mono">
                      {row.loaded_sessions} of {row.calendar_sessions}
                    </td>
                    <td className="pr-3 font-mono">
                      {row.expected_sessions_after_exclusions ?? 'n/a'}
                    </td>
                    <td className="pr-3 font-mono">{row.missing_full_sessions.count}</td>
                    <td className="pr-3 font-mono">
                      {pct(row.missing_share ?? row.missing_full_sessions.share, 2)}
                    </td>
                    <td className="pr-3 font-mono">
                      {row.missing_full_sessions.ranges.length > 0
                        ? row.missing_full_sessions.ranges.join(', ')
                        : 'none'}
                    </td>
                    <td className="pr-3 font-mono">
                      {partial?.audited ? partial.count : 'not checked'}
                    </td>
                    <td className="font-mono">
                      {partial?.audited ? partial.missing_bars : 'not checked'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {!!coverage.symbols_not_listed && (
            <p className="text-xs text-muted-foreground">
              {coverage.symbols_not_listed} further symbols with better coverage are not listed.
            </p>
          )}
          <p className="text-xs text-muted-foreground" data-testid="continuity-note">
            {plainNote(coverage.continuity_note)}
          </p>
        </div>
      </details>
    </div>
  )
}
