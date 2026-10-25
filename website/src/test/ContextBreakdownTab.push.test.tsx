/**
 * The Context tab runs no timer. It reads its trace once on mount, then re-reads
 * only when a pushed `usage` frame says the trace moved, or when the socket
 * reconnects (`hooks/websocket/contextTraceRefresh.ts`).
 *
 * Driven through the real tab and a real QueryClient with the app's own default
 * staleTime (Infinity), so a test that passed here would also hold in the app.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'

const traceMock = vi.fn()
const capsMock = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    telemetryContextTrace: (slot: string) => traceMock(slot),
    chatSlotSelectionCapabilities: (slot: string) => capsMock(slot),
  },
}))

import { ContextBreakdownTab, type ContextTrace } from '../pages/ContextBreakdownPanel'
import {
  CONTEXT_TRACE_COALESCE_MS as COALESCE_MS,
  contextTraceKey,
  onUsageFrame,
  readSessionProjectionFrame,
  rereadAllContextTraces,
  resetContextTraceRefresh,
} from '../hooks/useWebSocket'

type SessionProjectionFrame = NonNullable<ReturnType<typeof readSessionProjectionFrame>>

const SLOT = 'chat-7'
const EMPTY = 'No context breakdown recorded for this session yet.'

function trace(turns: number): ContextTrace {
  return {
    slot: SLOT,
    turns: Array.from({ length: turns }, (_, i) => ({
      ts: `2026-10-04T05:0${i}:00Z`,
      phase: 'turn',
      blocks: { your_message: 10 },
      total_chars: 10,
      context_used: 0,
      context_window: 0,
      model: 'm',
      ordinal: i + 1,
    })),
    totals: { your_message: 10 * turns },
    injected_chars: 0,
    user_chars: 10 * turns,
    peak_context_used: 0,
    context_window: 0,
    window_days: 14,
    latest:
      turns > 0
        ? {
            turn: turns,
            phase: 'per_turn',
            ours: { message: 10 * turns, memory: 0, rules: 0, skills: 0, other: 0 },
            loop_chars: 0,
            loop_estimated: true,
            engine: null,
            ttft_ms: null,
            context_used: 0,
            context_window: 0,
          }
        : null,
    trend: [],
  }
}

function frame(revision: number, over: Partial<SessionProjectionFrame> = {}): SessionProjectionFrame {
  return { unit: 'unit-1', slot: `dashboard:${SLOT}`, name: 'usage', seq: 1, value: {}, revision, ...over }
}

/** A read the test resolves by hand, to hold one in flight. */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

let qc: QueryClient

function mount() {
  return render(createElement(QueryClientProvider, { client: qc }, createElement(ContextBreakdownTab, { slot: SLOT })))
}

/** Let resolved promises and react-query's notify batch run. */
async function settle() {
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
}

/** Fire the coalesce window, then settle. */
async function window() {
  await act(async () => { await vi.advanceTimersByTimeAsync(COALESCE_MS) })
  await settle()
}

// react-query batches its observer notifications on a setTimeout it captured at
// import, before the fake clock existed; run them as microtasks so a settled read
// reaches the rendered tab inside `settle()`.
notifyManager.setScheduler(queueMicrotask)

beforeEach(() => {
  vi.useFakeTimers()
  traceMock.mockReset()
  capsMock.mockReset()
  capsMock.mockResolvedValue({ known: true, backend: 'kas' })
  resetContextTraceRefresh()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
})

afterEach(() => {
  cleanup()
  qc.clear()
  vi.useRealTimers()
})

describe('Context tab: pushed frames replace the poll', () => {
  it('reads once on mount, and a usage frame updates the panel with no poll', async () => {
    traceMock.mockResolvedValueOnce(trace(0)).mockResolvedValueOnce(trace(1))
    mount()
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(1)
    expect(screen.getByText(EMPTY)).toBeTruthy()

    expect(onUsageFrame(qc, frame(5))).toBe('requested')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(EMPTY)).toBeNull()
  })

  it('leaves no interval running: minutes pass and nothing reads', async () => {
    // react-query's refetchInterval is a setInterval; the tab must arm none.
    const intervals = vi.spyOn(globalThis, 'setInterval')
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
    expect(traceMock).toHaveBeenCalledTimes(1)
    expect(intervals).not.toHaveBeenCalled()
    intervals.mockRestore()
  })

  it('drops a frame at or below the revision already seen for its unit', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    expect(onUsageFrame(qc, frame(5))).toBe('requested')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)

    expect(onUsageFrame(qc, frame(5))).toBe('stale')
    expect(onUsageFrame(qc, frame(4))).toBe('stale')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)
  })

  it('judges revisions per unit, so a new unit of the slot still re-reads', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    onUsageFrame(qc, frame(9))
    await window()
    expect(onUsageFrame(qc, frame(1, { unit: 'unit-2' }))).toBe('requested')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(3)
  })

  it('ignores frames for other folds', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    expect(onUsageFrame(qc, frame(5, { name: 'status' }))).toBe('ignored')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(1)
  })

  it('merges a burst of frames into one read', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    for (let r = 1; r <= 6; r++) onUsageFrame(qc, frame(r, { unit: r % 2 ? 'unit-1' : 'unit-2' }))
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)
  })

  it('a frame during a read owes exactly one more read, and the newer one lands last', async () => {
    traceMock.mockResolvedValueOnce(trace(0))
    mount()
    await settle()

    const first = deferred<ContextTrace>()
    const second = deferred<ContextTrace>()
    traceMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    onUsageFrame(qc, frame(1))
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)

    // Three more windows' worth of frames while that read is on the wire.
    onUsageFrame(qc, frame(2))
    await window()
    onUsageFrame(qc, frame(3))
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)

    // The old read settles; exactly one follow-up starts.
    first.resolve(trace(1))
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(3)
    second.resolve(trace(3))
    await settle()
    expect(qc.getQueryData<ContextTrace>(contextTraceKey(SLOT))?.turns).toHaveLength(3)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(traceMock).toHaveBeenCalledTimes(3)
  })

  it('re-reads once on a reconnect, and a restarted gateway\'s low revisions are not stale', async () => {
    traceMock.mockResolvedValue(trace(0))
    mount()
    await settle()
    onUsageFrame(qc, frame(40))
    await window()
    expect(traceMock).toHaveBeenCalledTimes(2)

    // The socket reopens on a restarted gateway: what useWebSocket's onopen does.
    resetContextTraceRefresh()
    rereadAllContextTraces(qc)
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(3)

    // The new process mints from 1 again; its first frame must not be dropped.
    expect(onUsageFrame(qc, frame(1))).toBe('requested')
    await window()
    expect(traceMock).toHaveBeenCalledTimes(4)
  })

  it('names the harness the selection-capabilities route reports, Kiro CLI included', async () => {
    const unsplit = (): ContextTrace => {
      const t = trace(1)
      return { ...t, latest: { ...t.latest!, engine: null, loop_estimated: true, context_used: 9_000, context_window: 1_000_000 } }
    }
    traceMock.mockResolvedValue(unsplit())
    mount()
    await settle()
    expect(capsMock).toHaveBeenCalledWith(SLOT)
    // Kiro Agent: the turn is not split yet, never "switch to Kiro Agent".
    const kasGlyph = screen.getByRole('button', { name: /more information/i })
    await act(async () => { kasGlyph.focus(); kasGlyph.dispatchEvent(new FocusEvent('focus')) })
    expect(document.body.textContent).toMatch(/Not measured for this turn yet\./)
    expect(document.body.textContent).not.toMatch(/pick Kiro Agent/)
    cleanup()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    // Kiro CLI's runtime id is the empty string.
    capsMock.mockResolvedValue({ known: true, backend: '' })
    mount()
    await settle()
    const glyph = screen.getByRole('button', { name: /more information/i })
    await act(async () => { glyph.focus(); glyph.dispatchEvent(new FocusEvent('focus')) })
    expect(document.body.textContent).toMatch(/Kiro CLI doesn't report/)
  })

  it('re-reads on every mount, even over a cached trace', async () => {
    traceMock.mockResolvedValue(trace(0))
    const first = mount()
    await settle()
    first.unmount()
    mount()
    await settle()
    expect(traceMock).toHaveBeenCalledTimes(2)
  })

  it('does nothing for a slot no tab has read', async () => {
    expect(onUsageFrame(qc, frame(1, { slot: 'dashboard:other' }))).toBe('requested')
    await window()
    expect(traceMock).not.toHaveBeenCalled()
  })
})
