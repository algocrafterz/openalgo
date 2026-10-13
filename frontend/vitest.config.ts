import path from 'path'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  oxc: {
    jsx: {
      runtime: 'automatic',
      importSource: 'react',
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{js,mjs,cjs,ts,mts,cts,jsx,tsx}'],
    exclude: ['node_modules', 'dist', '.idea', '.git', '.cache'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      exclude: [
        'node_modules/',
        'src/test/',
        '**/*.d.ts',
        '**/*.config.*',
        '**/types/*',
      ],
    },
    css: true,
    // jsdom integration tests (StrategyBuilder, flow panels) ran 2-3.5x slower when every core
    // but one ran a worker, and timed out: 15 failures at 11 workers, 3 at 6, on a 12-core host.
    maxWorkers: '50%',
    // `npm run test:coverage` pins 2 workers, as CI's 4-vCPU runner gets from 50%. On a 12-core
    // host, 6 coverage workers ran the slow integration tests 3-4x slower than alone (calendar
    // 11.2 s vs 3.7 s at 2 workers; flow lookup 3.8 s vs 1.4 s) for no wall-time gain (124 s vs
    // 129 s), and intermittently past their timeouts.
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
