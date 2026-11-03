/**
 * Put text on the clipboard.
 *
 * `navigator.clipboard` is undefined on plain http, which a self-hosted install
 * reached over a LAN address is. Returns false when nothing could copy, so the
 * caller can show the text selected and ask the trader to press Ctrl+C.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall through. A permissions policy can reject it even on https.
    }
  }
  if (typeof document === 'undefined') return false
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  try {
    return typeof document.execCommand === 'function' && document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(area)
  }
}
