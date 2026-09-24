import { expect, test } from '@playwright/test'

test.describe('Strategy P&L page', () => {
  test('unauthenticated visit redirects to login rather than rendering the page', async ({
    page,
  }) => {
    await page.goto('/strategy-pnl')
    await page.waitForLoadState('networkidle')

    // Unauthenticated sessions can render the login page before the URL changes,
    // same pattern as navigation.spec.ts.
    try {
      await expect(page).toHaveURL(/\/(login)?$/, { timeout: 5000 })
    } catch {
      await expect(page.locator('input[type="password"]')).toBeVisible()
    }
  })

  test('route is registered and does not 404', async ({ page }) => {
    const response = await page.goto('/strategy-pnl')
    expect(response?.status()).not.toBe(404)
  })
})
