import type { FieldDiff, RunComparison, SpecDiff } from '@/api/backtestResults'
import { NA } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'
import { ShortHash } from './ShortHash'

export function showValue(v: unknown): string {
  if (v === null || v === undefined) return NA
  return typeof v === 'string' ? v : JSON.stringify(v)
}

const TH = 'text-left font-medium py-1 pr-4'
const TD = 'py-1 pr-4 font-mono align-top'

/** Rows of compared fields; rows that differ are emphasised. No value is judged. */
export function FieldDiffTable({ rows, caption }: { rows: FieldDiff[]; caption: string }) {
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">Nothing recorded to compare.</p>
  }
  return (
    <table className="w-full text-sm">
      <caption className="sr-only">{caption}</caption>
      <thead>
        <tr>
          <th scope="col" className={TH}>
            Field
          </th>
          <th scope="col" className={TH}>
            Run A
          </th>
          <th scope="col" className={TH}>
            Run B
          </th>
          <th scope="col" className={TH}>
            Status
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr
            key={r.field}
            data-changed={r.changed ? 'true' : 'false'}
            className={cn('border-t', r.changed && 'bg-muted font-medium')}
          >
            <th scope="row" className="text-left py-1 pr-4 font-normal">
              {r.label}
            </th>
            <td className={TD}>{showValue(r.a)}</td>
            <td className={TD}>{showValue(r.b)}</td>
            <td className="py-1">{r.changed ? 'Changed' : 'Same'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function SpecDiffTable({
  name,
  diff,
  fingerprintChanged,
}: {
  name: string
  diff: SpecDiff
  fingerprintChanged: boolean
}) {
  const rows: { kind: string; key: string; a: unknown; b: unknown }[] = [
    ...Object.entries(diff.changed).map(([key, v]) => ({ kind: 'changed', key, a: v.a, b: v.b })),
    ...Object.entries(diff.added).map(([key, v]) => ({ kind: 'added', key, a: null, b: v })),
    ...Object.entries(diff.removed).map(([key, v]) => ({ kind: 'removed', key, a: v, b: null })),
  ]
  if (rows.length === 0) {
    return fingerprintChanged ? (
      <p className="text-sm">{name}: Fingerprints differ; the detailed change was not recorded</p>
    ) : (
      <p className="text-sm">{name}: unchanged</p>
    )
  }
  return (
    <table className="w-full text-sm">
      <caption className="text-left font-medium pb-1">{name} differences</caption>
      <thead className="sr-only">
        <tr>
          <th scope="col">Name</th>
          <th scope="col">Change</th>
          <th scope="col">Run A</th>
          <th scope="col">Run B</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.kind}-${r.key}`} className="border-t">
            <th scope="row" className="text-left py-1 pr-4 font-mono font-normal">
              {r.key}
            </th>
            <td className="py-1 pr-4">{r.kind}</td>
            <td className={TD}>{showValue(r.a)}</td>
            <td className={TD}>{showValue(r.b)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function StrategySection({ strategy }: { strategy: RunComparison['strategy'] }) {
  const fp = strategy.fingerprints
  const pairs: [string, typeof fp.rules][] = [
    ['Rules', fp.rules],
    ['Parameters', fp.params],
    ['Strategy', fp.strategy],
  ]
  return (
    <section aria-labelledby="cmp-strategy" className="space-y-3">
      <h3 id="cmp-strategy" className="font-medium">
        Strategy
      </h3>
      <p className="text-sm">
        Version: run A {strategy.version.a ?? NA}, run B {strategy.version.b ?? NA}
        {strategy.same_version ? ' (same version)' : ''}
      </p>
      <SpecDiffTable name="Rules" diff={strategy.rules} fingerprintChanged={fp.rules.changed} />
      <SpecDiffTable
        name="Parameters"
        diff={strategy.parameters}
        fingerprintChanged={fp.params.changed}
      />
      <ul className="text-sm space-y-1" aria-label="Fingerprints">
        {pairs.map(([label, p]) => (
          <li key={label}>
            {label} fingerprint: <ShortHash value={p.a} /> to <ShortHash value={p.b} />{' '}
            <span className="text-muted-foreground">{p.changed ? 'changed' : 'unchanged'}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function DiffSection({
  id,
  title,
  rows,
  children,
}: {
  id: string
  title: string
  rows: FieldDiff[]
  children?: React.ReactNode
}) {
  return (
    <section aria-labelledby={id} className="space-y-2">
      <h3 id={id} className="font-medium">
        {title}
      </h3>
      {children}
      <FieldDiffTable rows={rows} caption={`${title} differences`} />
    </section>
  )
}
