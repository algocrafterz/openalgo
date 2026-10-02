import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { GLOSSARY_IDS } from '@/lib/backtestGlossary'

const DIR = __dirname
const SOURCES = readdirSync(DIR).filter((f) => f.endsWith('.tsx') && !f.endsWith('.test.tsx'))

describe('InfoTip term ids', () => {
  it('scans at least the known components', () => {
    expect(SOURCES).toContain('DecisionPanel.tsx')
  })

  it.each(SOURCES)('%s only uses real glossary ids', (file) => {
    const text = readFileSync(join(DIR, file), 'utf8')
    const terms = [...text.matchAll(/\bterm="([^"]+)"/g)].map((m) => m[1])
    for (const term of terms) {
      expect(GLOSSARY_IDS as string[], `${file} uses unknown term "${term}"`).toContain(term)
    }
  })
})
