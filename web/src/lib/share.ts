// Handing text to another app (an invite code to a messenger): the Android app's share sheet (bridge
// `shareText`, app 1.3.0+), the browser's Web Share, or the clipboard.
import { toast } from './ui';

interface ShareBridge {
  shareText?: (text: string) => void;
}

function bridge(): ShareBridge | undefined {
  return (window as unknown as { HomeMusicAndroid?: ShareBridge }).HomeMusicAndroid;
}

function supportsWebShare(): boolean {
  if (typeof navigator === 'undefined' || typeof navigator.share !== 'function') return false;
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    return window.matchMedia('(pointer: coarse)').matches;
  }
  return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
}

export function canShare(): boolean {
  return typeof bridge()?.shareText === 'function' || supportsWebShare();
}

/** Resolves false when the person closed the share sheet; other errors reach the copy fallback. */
export async function shareText(text: string): Promise<boolean> {
  const b = bridge();
  if (typeof b?.shareText === 'function') {
    b.shareText(text);
    return true;
  }
  if (!supportsWebShare()) return false;
  try {
    await navigator.share({ text });
    return true;
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') return false;
    throw error;
  }
}

/** Copies within the tap: the old way first — the app's WebView has no Clipboard API permission (its
 * `writeText` fails) and plain http has no Clipboard API at all; then the Clipboard API. */
export async function copyText(text: string): Promise<boolean> {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '0';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  ta.remove();
  if (ok) return true;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function shareLink(path: string, hubUrl = window.location.origin): string {
  return `${hubUrl.replace(/\/$/, '')}/#${path}`;
}

export function shareLines(...lines: Array<string | null | undefined>): string {
  return lines.filter((line): line is string => !!line).join('\n');
}

export async function shareOrCopy(text: string, copiedMessage: string) {
  if (canShare()) {
    try {
      if (await shareText(text)) return;
      return;
    } catch {
      // Fall back to copying when the share provider fails for a reason other than cancellation.
    }
  }
  const copied = await copyText(text);
  toast(copied ? copiedMessage : 'Не удалось скопировать', copied ? 'success' : 'error');
}
