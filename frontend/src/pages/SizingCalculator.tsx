import { Calculator, ChevronDown, ChevronUp, Info, Loader2, Zap } from 'lucide-react'
import { useState } from 'react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { useAuthStore } from '@/stores/authStore'
import { useSizing, type SizingRequest } from '@/hooks/useSizing'
import { useORBPreset, type ORBLevels } from '@/hooks/useORBPreset'

type SizingMode = 'fixed_fractional' | 'pct_of_capital'

interface FormState {
  symbol: string
  exchange: string
  side: 'BUY' | 'SELL'
  product: 'MIS' | 'NRML' | 'CNC'
  entry_price: string
  stop_loss: string
  target: string
  capital: string
  sizing_mode: SizingMode
  risk_per_trade: string
  pct_of_capital: string
  slippage_factor: string
  max_sl_pct_for_sizing: string
  min_entry_price: string
  max_entry_price: string
}

const DEFAULT_FORM: FormState = {
  symbol: '',
  exchange: 'NSE',
  side: 'BUY',
  product: 'MIS',
  entry_price: '',
  stop_loss: '',
  target: '',
  capital: '',
  sizing_mode: 'fixed_fractional',
  risk_per_trade: '0.01',
  pct_of_capital: '0.10',
  slippage_factor: '0',
  max_sl_pct_for_sizing: '0',
  min_entry_price: '0',
  max_entry_price: '0',
}

function ResultRow({
  label,
  value,
  highlight,
  tooltip,
}: {
  label: string
  value: string
  highlight?: boolean
  tooltip?: string
}) {
  return (
    <div
      className={`flex items-center justify-between py-2 px-3 rounded-md ${
        highlight ? 'bg-primary/10 font-semibold' : 'bg-muted/50'
      }`}
    >
      <span className="text-sm text-muted-foreground flex items-center gap-1">
        {label}
        {tooltip && (
          <span title={tooltip} className="cursor-help">
            <Info className="h-3 w-3" />
          </span>
        )}
      </span>
      <span className={`text-sm ${highlight ? 'text-primary text-base' : ''}`}>{value}</span>
    </div>
  )
}

interface ORBFormState {
  symbol: string
  exchange: string
  orb_minutes: string
  tp_rr: string
}

const DEFAULT_ORB_FORM: ORBFormState = {
  symbol: '',
  exchange: 'NSE',
  orb_minutes: '15',
  tp_rr: '2.0',
}

function ORBBadge({ side }: { side: ORBLevels['side'] }) {
  const color =
    side === 'BUY'
      ? 'bg-green-100 text-green-800'
      : side === 'SELL'
        ? 'bg-red-100 text-red-800'
        : 'bg-yellow-100 text-yellow-800'
  return <Badge className={`${color} text-xs font-semibold`}>{side}</Badge>
}

export default function SizingCalculator() {
  const { apiKey } = useAuthStore()
  const [form, setForm] = useState<FormState>(DEFAULT_FORM)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [orbForm, setOrbForm] = useState<ORBFormState>(DEFAULT_ORB_FORM)
  const { mutate: calculate, data: result, isPending, error } = useSizing()
  const {
    mutate: loadORB,
    data: orbResult,
    isPending: orbLoading,
    error: orbError,
  } = useORBPreset()

  function setField<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  function setOrbField<K extends keyof ORBFormState>(key: K, value: string) {
    setOrbForm((prev) => ({ ...prev, [key]: value }))
  }

  function handleLoadORB() {
    if (!apiKey || !orbForm.symbol) return
    loadORB(
      {
        apikey: apiKey,
        symbol: orbForm.symbol.toUpperCase(),
        exchange: orbForm.exchange,
        orb_minutes: parseInt(orbForm.orb_minutes),
        tp_rr: parseFloat(orbForm.tp_rr),
      },
      {
        onSuccess(data) {
          if (data.status === 'success' && data.data) {
            const { inputs, sizing } = data.data
            setForm((prev) => ({
              ...prev,
              symbol: inputs.symbol,
              exchange: inputs.exchange,
              side: inputs.side as 'BUY' | 'SELL',
              product: inputs.product as 'MIS' | 'NRML' | 'CNC',
              entry_price: String(inputs.entry_price),
              stop_loss: String(inputs.stop_loss),
              target: String(inputs.target),
              sizing_mode: inputs.sizing_mode as SizingMode,
              risk_per_trade: String(inputs.risk_per_trade),
              slippage_factor: String(inputs.slippage_factor),
            }))
            // If sizing already ran with 0 capital, clear it so user can fill capital
            if (sizing.skip_reason?.includes('Capital')) {
              setForm((prev) => ({ ...prev, capital: '' }))
            }
          }
        },
      }
    )
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!apiKey) return

    const payload: SizingRequest = {
      apikey: apiKey,
      symbol: form.symbol || undefined,
      exchange: form.exchange || undefined,
      side: form.side,
      product: form.product,
      entry_price: parseFloat(form.entry_price),
      stop_loss: parseFloat(form.stop_loss),
      target: form.target ? parseFloat(form.target) : undefined,
      capital: form.capital ? parseFloat(form.capital) : null,
      sizing_mode: form.sizing_mode,
      risk_per_trade:
        form.sizing_mode === 'fixed_fractional' ? parseFloat(form.risk_per_trade) : undefined,
      pct_of_capital:
        form.sizing_mode === 'pct_of_capital' ? parseFloat(form.pct_of_capital) : null,
      slippage_factor: parseFloat(form.slippage_factor) || 0,
      max_sl_pct_for_sizing: parseFloat(form.max_sl_pct_for_sizing) || 0,
      min_entry_price: parseFloat(form.min_entry_price) || 0,
      max_entry_price: parseFloat(form.max_entry_price) || 0,
    }

    calculate(payload)
  }

  const data = result?.data
  const isSuccess = result?.status === 'success'

  return (
    <div className="py-6 space-y-6 max-w-4xl mx-auto">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Calculator className="h-6 w-6" />
          Position Sizing Calculator
        </h1>
        <p className="text-muted-foreground mt-1">
          Calculate optimal position size based on capital, risk tolerance, and entry/stop levels.
        </p>
      </div>

      {!apiKey && (
        <Alert variant="destructive">
          <AlertDescription>
            API key not found. Please generate one from the API Key page before using this tool.
          </AlertDescription>
        </Alert>
      )}

      {/* ORB Preset */}
      <Card className="border-orange-200 bg-orange-50/40 dark:bg-orange-950/10 dark:border-orange-900">
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <Zap className="h-4 w-4 text-orange-500" />
            ORB Preset
            <Badge variant="secondary" className="text-xs">
              Auto-fill
            </Badge>
          </CardTitle>
          <CardDescription>
            Enter a symbol — entry, SL, and TP are derived from live ORB levels. All other
            parameters are pre-set to ORB strategy defaults.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div className="space-y-1.5 sm:col-span-1">
              <Label htmlFor="orb-symbol">Symbol</Label>
              <Input
                id="orb-symbol"
                placeholder="SBIN"
                value={orbForm.symbol}
                onChange={(e) => setOrbField('symbol', e.target.value.toUpperCase())}
                onKeyDown={(e) => e.key === 'Enter' && handleLoadORB()}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="orb-exchange">Exchange</Label>
              <Select value={orbForm.exchange} onValueChange={(v) => setOrbField('exchange', v)}>
                <SelectTrigger id="orb-exchange">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['NSE', 'BSE'].map((ex) => (
                    <SelectItem key={ex} value={ex}>
                      {ex}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="orb-minutes">ORB Period</Label>
              <Select
                value={orbForm.orb_minutes}
                onValueChange={(v) => setOrbField('orb_minutes', v)}
              >
                <SelectTrigger id="orb-minutes">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {['5', '15', '30', '60'].map((m) => (
                    <SelectItem key={m} value={m}>
                      {m} min
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="orb-tprr">TP (R×)</Label>
              <Input
                id="orb-tprr"
                type="number"
                step="0.5"
                min="0.5"
                max="10"
                value={orbForm.tp_rr}
                onChange={(e) => setOrbField('tp_rr', e.target.value)}
              />
            </div>
          </div>

          <Button
            type="button"
            variant="outline"
            className="border-orange-300 hover:bg-orange-100 dark:hover:bg-orange-950/30"
            disabled={orbLoading || !apiKey || !orbForm.symbol}
            onClick={handleLoadORB}
          >
            {orbLoading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                Fetching ORB levels...
              </>
            ) : (
              <>
                <Zap className="h-4 w-4 mr-2 text-orange-500" />
                Load ORB Preset
              </>
            )}
          </Button>

          {orbError && (
            <p className="text-sm text-destructive">
              {orbError.message || 'Failed to load ORB preset'}
            </p>
          )}

          {orbResult?.status === 'error' && (
            <p className="text-sm text-destructive">{orbResult.message}</p>
          )}

          {orbResult?.status === 'success' && orbResult.data && (
            <div className="flex flex-wrap gap-3 pt-1">
              <div className="flex items-center gap-1.5 text-sm">
                <span className="text-muted-foreground">Side:</span>
                <ORBBadge side={orbResult.data.orb.side} />
              </div>
              <div className="flex items-center gap-1.5 text-sm">
                <span className="text-muted-foreground">LTP:</span>
                <span className="font-medium">₹{orbResult.data.orb.ltp}</span>
              </div>
              <div className="flex items-center gap-1.5 text-sm">
                <span className="text-muted-foreground">ORB High:</span>
                <span className="font-medium">₹{orbResult.data.orb.orb_high}</span>
              </div>
              <div className="flex items-center gap-1.5 text-sm">
                <span className="text-muted-foreground">ORB Low:</span>
                <span className="font-medium">₹{orbResult.data.orb.orb_low}</span>
              </div>
              <div className="flex items-center gap-1.5 text-sm">
                <span className="text-muted-foreground">Range:</span>
                <span className="font-medium">₹{orbResult.data.orb.orb_range}</span>
              </div>
              {orbResult.data.orb.side === 'INSIDE' && (
                <p className="w-full text-xs text-yellow-600 dark:text-yellow-400">
                  Price is inside the ORB range — no breakout yet. Levels pre-filled for reference.
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Separator />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Input form */}
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Parameters</CardTitle>
            <CardDescription>Enter trade parameters to calculate position size</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSubmit} className="space-y-4">
              {/* Trade setup */}
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label htmlFor="symbol">Symbol</Label>
                  <Input
                    id="symbol"
                    placeholder="SBIN"
                    value={form.symbol}
                    onChange={(e) => setField('symbol', e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="exchange">Exchange</Label>
                  <Select value={form.exchange} onValueChange={(v) => setField('exchange', v)}>
                    <SelectTrigger id="exchange">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {['NSE', 'BSE', 'NFO', 'MCX', 'CDS'].map((ex) => (
                        <SelectItem key={ex} value={ex}>
                          {ex}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label htmlFor="side">Side</Label>
                  <Select
                    value={form.side}
                    onValueChange={(v) => setField('side', v as 'BUY' | 'SELL')}
                  >
                    <SelectTrigger id="side">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="BUY">BUY</SelectItem>
                      <SelectItem value="SELL">SELL</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="product">Product</Label>
                  <Select
                    value={form.product}
                    onValueChange={(v) => setField('product', v as 'MIS' | 'NRML' | 'CNC')}
                  >
                    <SelectTrigger id="product">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="MIS">MIS (Intraday)</SelectItem>
                      <SelectItem value="CNC">CNC (Delivery)</SelectItem>
                      <SelectItem value="NRML">NRML (Futures)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              {/* Price levels */}
              <div className="grid grid-cols-3 gap-3">
                <div className="space-y-1.5">
                  <Label htmlFor="entry_price">Entry Price *</Label>
                  <Input
                    id="entry_price"
                    type="number"
                    step="0.01"
                    min="0.01"
                    required
                    placeholder="820.00"
                    value={form.entry_price}
                    onChange={(e) => setField('entry_price', e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="stop_loss">Stop Loss *</Label>
                  <Input
                    id="stop_loss"
                    type="number"
                    step="0.01"
                    min="0"
                    required
                    placeholder="815.00"
                    value={form.stop_loss}
                    onChange={(e) => setField('stop_loss', e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="target">Target</Label>
                  <Input
                    id="target"
                    type="number"
                    step="0.01"
                    min="0"
                    placeholder="830.00"
                    value={form.target}
                    onChange={(e) => setField('target', e.target.value)}
                  />
                </div>
              </div>

              {/* Capital */}
              <div className="space-y-1.5">
                <Label htmlFor="capital">
                  Capital{' '}
                  <span className="text-xs text-muted-foreground">
                    (leave blank to use live account balance)
                  </span>
                </Label>
                <Input
                  id="capital"
                  type="number"
                  step="1"
                  min="0"
                  placeholder="35000"
                  value={form.capital}
                  onChange={(e) => setField('capital', e.target.value)}
                />
              </div>

              {/* Sizing mode */}
              <div className="space-y-1.5">
                <Label htmlFor="sizing_mode">Sizing Mode *</Label>
                <Select
                  value={form.sizing_mode}
                  onValueChange={(v) => setField('sizing_mode', v as SizingMode)}
                >
                  <SelectTrigger id="sizing_mode">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="fixed_fractional">
                      Fixed Fractional (risk % of capital)
                    </SelectItem>
                    <SelectItem value="pct_of_capital">
                      Percent of Capital (allocate % to position)
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {/* Mode-specific parameter */}
              {form.sizing_mode === 'fixed_fractional' ? (
                <div className="space-y-1.5">
                  <Label htmlFor="risk_per_trade">
                    Risk Per Trade{' '}
                    <span className="text-xs text-muted-foreground">(e.g. 0.01 = 1%)</span>
                  </Label>
                  <Input
                    id="risk_per_trade"
                    type="number"
                    step="0.001"
                    min="0"
                    max="1"
                    placeholder="0.01"
                    value={form.risk_per_trade}
                    onChange={(e) => setField('risk_per_trade', e.target.value)}
                  />
                </div>
              ) : (
                <div className="space-y-1.5">
                  <Label htmlFor="pct_of_capital">
                    Allocation{' '}
                    <span className="text-xs text-muted-foreground">(e.g. 0.10 = 10%)</span>
                  </Label>
                  <Input
                    id="pct_of_capital"
                    type="number"
                    step="0.01"
                    min="0"
                    max="1"
                    placeholder="0.10"
                    value={form.pct_of_capital}
                    onChange={(e) => setField('pct_of_capital', e.target.value)}
                  />
                </div>
              )}

              {/* Advanced options toggle */}
              <button
                type="button"
                className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors"
                onClick={() => setShowAdvanced((v) => !v)}
              >
                {showAdvanced ? (
                  <ChevronUp className="h-4 w-4" />
                ) : (
                  <ChevronDown className="h-4 w-4" />
                )}
                Advanced Options
              </button>

              {showAdvanced && (
                <div className="space-y-3 pl-3 border-l-2 border-muted">
                  <div className="space-y-1.5">
                    <Label htmlFor="slippage_factor">
                      Slippage Factor{' '}
                      <span className="text-xs text-muted-foreground">(e.g. 0.05 = 5%)</span>
                    </Label>
                    <Input
                      id="slippage_factor"
                      type="number"
                      step="0.01"
                      min="0"
                      placeholder="0"
                      value={form.slippage_factor}
                      onChange={(e) => setField('slippage_factor', e.target.value)}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="max_sl_pct">
                      Max SL % for Sizing{' '}
                      <span className="text-xs text-muted-foreground">(0 = disabled)</span>
                    </Label>
                    <Input
                      id="max_sl_pct"
                      type="number"
                      step="0.001"
                      min="0"
                      placeholder="0"
                      value={form.max_sl_pct_for_sizing}
                      onChange={(e) => setField('max_sl_pct_for_sizing', e.target.value)}
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1.5">
                      <Label htmlFor="min_price">Min Entry Price</Label>
                      <Input
                        id="min_price"
                        type="number"
                        step="1"
                        min="0"
                        placeholder="0"
                        value={form.min_entry_price}
                        onChange={(e) => setField('min_entry_price', e.target.value)}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="max_price">Max Entry Price</Label>
                      <Input
                        id="max_price"
                        type="number"
                        step="1"
                        min="0"
                        placeholder="0"
                        value={form.max_entry_price}
                        onChange={(e) => setField('max_entry_price', e.target.value)}
                      />
                    </div>
                  </div>
                </div>
              )}

              <Button
                type="submit"
                className="w-full"
                disabled={isPending || !apiKey || !form.entry_price || !form.stop_loss}
              >
                {isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin mr-2" />
                    Calculating...
                  </>
                ) : (
                  <>
                    <Calculator className="h-4 w-4 mr-2" />
                    Calculate Position Size
                  </>
                )}
              </Button>
            </form>
          </CardContent>
        </Card>

        {/* Results */}
        <div className="space-y-4">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>
                {error.message || 'Failed to calculate position size. Please try again.'}
              </AlertDescription>
            </Alert>
          )}

          {result && !isSuccess && (
            <Alert variant="destructive">
              <AlertDescription>{result.message || 'Calculation failed.'}</AlertDescription>
            </Alert>
          )}

          {isSuccess && data && (
            <>
              {data.skip_reason && (
                <Alert>
                  <Info className="h-4 w-4" />
                  <AlertDescription>
                    <strong>Trade Skipped:</strong> {data.skip_reason}
                  </AlertDescription>
                </Alert>
              )}

              {data.warnings.length > 0 && (
                <Alert>
                  <AlertDescription>
                    <strong>Warnings:</strong>
                    <ul className="list-disc list-inside mt-1">
                      {data.warnings.map((w, i) => (
                        <li key={i} className="text-sm">
                          {w}
                        </li>
                      ))}
                    </ul>
                  </AlertDescription>
                </Alert>
              )}

              <Card>
                <CardHeader>
                  <CardTitle className="text-lg">Results</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  <ResultRow
                    label="Quantity"
                    value={data.quantity.toLocaleString()}
                    highlight
                    tooltip="Number of shares/units to trade"
                  />
                  <ResultRow
                    label="Position Value"
                    value={`₹${data.position_value.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`}
                    tooltip="Total capital deployed (qty × entry)"
                  />
                  <ResultRow
                    label="Risk Amount"
                    value={`₹${data.risk_amount.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`}
                    tooltip="Maximum loss if stop-loss is hit"
                  />
                  <ResultRow
                    label="Risk % of Capital"
                    value={`${(data.risk_pct_of_capital * 100).toFixed(3)}%`}
                    tooltip="Risk amount as % of capital"
                  />
                  {data.reward_amount > 0 && (
                    <ResultRow
                      label="Reward Amount"
                      value={`₹${data.reward_amount.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`}
                      tooltip="Potential profit if target is hit"
                    />
                  )}
                  {data.rr_ratio > 0 && (
                    <ResultRow
                      label="R:R Ratio"
                      value={`${data.rr_ratio.toFixed(2)} : 1`}
                      tooltip="Reward-to-Risk ratio"
                    />
                  )}
                  <ResultRow
                    label="SL Distance"
                    value={`${(data.sl_distance_pct * 100).toFixed(3)}%`}
                    tooltip="Stop-loss distance as % of entry"
                  />
                </CardContent>
              </Card>
            </>
          )}

          {!result && !isPending && (
            <Card className="border-dashed">
              <CardContent className="flex flex-col items-center justify-center py-12 text-muted-foreground">
                <Calculator className="h-12 w-12 mb-3 opacity-30" />
                <p className="text-sm">Fill in the parameters and click Calculate</p>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
