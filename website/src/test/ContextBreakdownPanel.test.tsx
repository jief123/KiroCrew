/**
 * Context Breakdown panel: the properties that make the chart honest.
 *
 *  - every backend block label lands in exactly one of the five categories, and
 *    an unknown label goes to "other" rather than vanishing from the total.
 *  - the five every-turn boilerplate blocks collapse into one disclosure row.
 *  - only the newest 30 turns are drawn; older ones are counted, not dropped.
 *  - selecting a turn (click or arrow keys) drives the detail below the chart,
 *    and the newest turn is selected by default.
 *  - a session-start turn is listed above the chart, never drawn in it, so its
 *    size cannot pin the y-axis; axis labels are strided so they never overprint.
 *  - an un-recorded session degrades to a readable empty state, not a crash.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'

import {
  ContextBreakdownPanel,
  groupBlocks,
  categorise,
  categoryOf,
  niceTicks,
  axisLabelIndices,
  CATEGORIES,
  CATEGORY_OF,
  EVERY_TURN_MEMBERS,
  MAX_CHART_TURNS,
  USER_LABEL,
  type ContextTrace,
  type ContextTurn,
  type TrendRow,
} from '../pages/ContextBreakdownPanel'
import type { LatestTurn } from '../pages/ContextEngineBreakdown'

afterEach(cleanup)

/**
 * Budget for the two placement tests that dynamically import
 * `pages/chat/SidePanel`.
 *
 * Whichever of them runs first pays that module graph's resolve+transform, which
 * exceeded the 15s global default on a Windows checkout — so both failed for
 * every Windows contributor while passing on CI's Linux runner. The import stays
 * DYNAMIC deliberately: hoisting it to a static top-level import moved the same
 * work into the file's import phase and took the whole file from 32s to 259s
 * (measured), because then every test in it waits on that graph. This is a real,
 * bounded cost rather than a hang, so a per-test budget is the honest fix; the
 * global default stays tight for everything else.
 */
const SIDE_PANEL_IMPORT_TIMEOUT_MS = 45_000

const turn = (over: Partial<ContextTurn> = {}): ContextTurn => ({
  ts: '2026-08-04T00:00:00Z',
  phase: 'per_turn',
  blocks: { request_header: 1576, your_message: 6 },
  total_chars: 1582,
  context_used: 2000,
  context_window: 200000,
  model: 'claude',
  ordinal: 0,
  ...over,
})

const trace = (over: Partial<ContextTrace> = {}): ContextTrace => ({
  slot: '578c537a',
  turns: [],
  totals: {},
  injected_chars: 0,
  user_chars: 0,
  peak_context_used: 0,
  context_window: 0,
  window_days: 14,
  ...over,
})

/** A turn whose blocks sum to `total_chars`, so the chart's stack is honest. */
const turnOf = (blocks: Record<string, number>, over: Partial<ContextTurn> = {}): ContextTurn =>
  turn({ blocks, total_chars: Object.values(blocks).reduce((a, b) => a + b, 0), ...over })

describe('categoryOf / categorise — five plain-language buckets', () => {
  it('maps every listed label to one of the five categories', () => {
    for (const [label, cat] of Object.entries(CATEGORY_OF)) {
      expect(CATEGORIES).toContain(cat)
      expect(categoryOf(label)).toBe(cat)
    }
    expect(categoryOf(USER_LABEL)).toBe('message')
    expect(categoryOf('lessons')).toBe('rules')
    expect(categoryOf('response_preferences')).toBe('rules')
    expect(categoryOf('loaded_skill')).toBe('skills')
    expect(categoryOf('semantic_memory')).toBe('memory')
    expect(categoryOf('task_facts')).toBe('memory')
  })

  it('sends the every-turn members, unclassified and unknown labels to "other"', () => {
    for (const member of EVERY_TURN_MEMBERS) expect(categoryOf(member)).toBe('other')
    expect(categoryOf('unclassified')).toBe('other')
    expect(categoryOf('some_future_block')).toBe('other')
  })

  it('never drops an unknown label from the total', () => {
    const cats = categorise({ your_message: 10, memory: 100, brand_new_label: 7, request_header: 3 })
    const sum = Object.values(cats).reduce((a, b) => a + b, 0)
    expect(sum).toBe(120)
    expect(cats.other).toBe(10)
    expect(cats.message).toBe(10)
    expect(cats.memory).toBe(100)
    // Every category is present so the stack always has five layers.
    expect(Object.keys(cats).sort()).toEqual([...CATEGORIES].sort())
  })
})

describe('groupBlocks — every-turn bucket', () => {
  it('collapses exactly the five every-turn members into one label', () => {
    const grouped = groupBlocks({
      surface: 100,
      working_folder: 50,
      request_header: 200,
      reply_format_rules: 30,
      user_display: 20,
      memory: 1000,
      your_message: 6,
    })
    expect(grouped.every_turn).toBe(400)
    expect(grouped.memory).toBe(1000)
    expect(grouped[USER_LABEL]).toBe(6)
    for (const member of EVERY_TURN_MEMBERS) {
      expect(grouped[member]).toBeUndefined()
    }
  })

  it('leaves a trace with no every-turn members untouched', () => {
    expect(groupBlocks({ memory: 10, loaded_skill: 20 })).toEqual({ memory: 10, loaded_skill: 20 })
  })
})

describe('niceTicks — three to four round gridlines', () => {
  it('rounds the ceiling up to a whole step and starts at zero', () => {
    expect(niceTicks(2400)).toEqual([0, 1000, 2000, 3000])
    expect(niceTicks(116652)).toEqual([0, 50000, 100000, 150000])
    expect(niceTicks(7)).toEqual([0, 5, 10])
  })

  it('keeps the tick count small for any magnitude', () => {
    for (const max of [1, 13, 999, 4321, 87654, 1234567]) {
      const ticks = niceTicks(max)
      expect(ticks[0]).toBe(0)
      expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(max)
      expect(ticks.length).toBeGreaterThanOrEqual(3)
      expect(ticks.length).toBeLessThanOrEqual(6)
    }
  })

  it('is defensive about an all-zero trace', () => {
    expect(niceTicks(0)).toEqual([0, 1])
  })
})

const latestOf = (over: Partial<LatestTurn> = {}): LatestTurn => ({
  turn: 6,
  phase: 'per_turn',
  ours: { message: 80, memory: 1200, rules: 500, skills: 240, other: 140 },
  loop_chars: 6000,
  loop_estimated: true,
  engine: null,
  ttft_ms: null,
  context_used: 2000,
  context_window: 200000,
  ...over,
})

const rowOf = (turn: number, loop = 1000): TrendRow => ({
  turn,
  ours: { message: 80, memory: 100 * turn, rules: 500, skills: 240, other: 140 },
  loop,
})

describe('ContextBreakdownPanel rendering, from the fold\'s bounded state', () => {
  const fiveTurns = () =>
    trace({
      latest: latestOf(),
      trend: [2, 3, 4, 5, 6].map(n => rowOf(n)),
      start: rowOf(1, 20000),
      earlier: 1,
      turn_count: 6,
      chars_per_token: 4,
    })

  it('draws one band per category and one for the rest of the context, bottom to top', () => {
    const { container } = render(<ContextBreakdownPanel trace={fiveTurns()} chartWidth={600} />)
    const layers = Array.from(container.querySelectorAll('svg g[data-category]')).map(g => g.getAttribute('data-category'))
    expect(layers).toEqual([...CATEGORIES, 'loop'])
    // The panel shows the newest turn only: the chart is a picture, not a picker.
    expect(container.querySelectorAll('[data-turn]')).toHaveLength(0)
  })

  it('heads an unsplit newest turn with its own token total, not Kiro Crew blocks alone', () => {
    render(<ContextBreakdownPanel trace={fiveTurns()} chartWidth={600} />)
    const detail = screen.getByTestId('selected-turn-detail')
    expect(within(detail).getByText(/Turn 6 · latest/)).toBeInTheDocument()
    expect(screen.getByTestId('whole-total').textContent).not.toMatch(/^2,160/)
    // Kiro Crew is the first row, with its total.
    const crew = detail.querySelector('[data-engine-row="crew"]') as HTMLElement
    expect(crew.textContent).toMatch(/Kiro Crew.*2,080/)
  })

  it('lists only the chart parts that are non-zero in the plotted turns', () => {
    const rows = [2, 3].map(n => ({ ...rowOf(n), ours: { ...rowOf(n).ours, memory: 0 } }))
    render(<ContextBreakdownPanel trace={trace({ latest: latestOf({ turn: 3 }), trend: rows })} chartWidth={600} />)
    const legend = screen.getByTestId('trend').textContent ?? ''
    expect(legend).not.toContain('Kiro Crew memory')
    expect(legend).toContain('Kiro Crew rules')
    expect(legend).toContain('Rest of the context')
  })

  it('lists the newest session-start turn above the chart and counts it among the earlier turns', () => {
    render(<ContextBreakdownPanel trace={fiveTurns()} chartWidth={600} />)
    const startRow = document.querySelector('[data-start-row]') as HTMLElement
    expect(startRow.textContent).toContain('Turn 1 · session start')
    // The only earlier turn is the start row above, so no line repeats it.
    expect(screen.queryByTestId('earlier-turns')).toBeNull()
  })

  it('says how many turns precede the ring, exactly as the fold counted them', () => {
    const rows = Array.from({ length: MAX_CHART_TURNS }, (_, i) => rowOf(i + 46))
    render(<ContextBreakdownPanel trace={trace({ latest: latestOf({ turn: 75 }), trend: rows, earlier: 45 })} chartWidth={600} />)
    expect(screen.getByText('Turns 1–45 earlier')).toBeInTheDocument()
    expect(screen.getByText(`Last ${MAX_CHART_TURNS} turns`)).toBeInTheDocument()
  })

  it('lists the rest of an unsplit turn as one grey row in estimated tokens', () => {
    render(<ContextBreakdownPanel trace={fiveTurns()} chartWidth={600} />)
    const rest = document.querySelector('[data-engine-row="rest"]') as HTMLElement
    expect(rest.textContent).toMatch(/^Rest of the context~\d/)
    expect(screen.getByTestId('engine-caption').textContent).toBe('Figures marked ~ are estimates (characters / 4).')
  })

  it('renders a readable empty state for a session with no recorded turns', () => {
    render(<ContextBreakdownPanel trace={trace({ turns: [] })} />)
    expect(screen.getByText(/No context breakdown recorded/i)).toBeInTheDocument()
  })

  it('does not promise data while recording is switched off', () => {
    render(<ContextBreakdownPanel trace={trace({ turns: [], recording: false, env_file: '/srv/crew/.env' })} />)
    expect(screen.getByText(/Context breakdown is not recorded while the crew log is off\. Remove KIROCREW_CREW_LOG from \/srv\/crew\/\.env/)).toBeInTheDocument()
    expect(screen.queryByText(/yet/)).toBeNull()
  })

  it('shows a loading state before the first payload', () => {
    const { container } = render(<ContextBreakdownPanel trace={undefined} isLoading />)
    expect(within(container).getByText(/Loading context breakdown/i)).toBeInTheDocument()
  })
})

describe('axisLabelIndices — labels that never overprint', () => {
  it('labels every turn when there is room', () => {
    expect(axisLabelIndices(6, 662, 5)).toEqual([0, 1, 2, 3, 4, 5])
  })

  it('strides, keeps the selected and the last turn, and drops colliding neighbours', () => {
    // 176px of plot fits 4 labels; 30 turns -> stride 8 -> 0, 8, 16, 24 (+ last).
    expect(axisLabelIndices(30, 176, 29)).toEqual([0, 8, 16, 29])
    // Selecting turn 9 (index 8) keeps it and drops nothing else; selecting index
    // 10 drops the strided 8 and 16 that would sit on top of it.
    expect(axisLabelIndices(30, 176, 8)).toEqual([0, 8, 16, 29])
    expect(axisLabelIndices(30, 176, 10)).toEqual([0, 10, 29])
    for (let sel = 0; sel < 30; sel++) {
      expect(axisLabelIndices(30, 176, sel).length).toBeLessThanOrEqual(Math.floor(176 / 44) + 2)
    }
  })
})

describe('placement: a per-session tab, not a global page', () => {
  // These two dynamically import `pages/chat/SidePanel`, whose transform is
  // charged to whichever test triggers it first. That exceeded the 15s default on
  // a Windows checkout, so both failed there while passing on CI's Linux runner.
  // The import stays DYNAMIC on purpose: hoisting it to a static top-level import
  // moved the same work into the file's import phase and took the file from 32s to
  // 259s (measured), because every test then waits on that graph. A per-test budget
  // is the cheaper half of that trade -- it is not masking a hang, the work is real
  // and bounded.
  it('is registered as a side-panel view next to Logs', async () => {
    const { PINNED_VIEWS } = await import('../hooks/usePanelTabs')
    const { NEW_MENU_LABEL_KEY, NEW_MENU_DESC_KEY } = await import('../pages/chat/SidePanel')
    // Opened from the + menu like Logs — not auto-pinned, since a session the
    // user never inspects should not carry a permanent extra tab.
    expect(PINNED_VIEWS).not.toContain('context')
    // Both maps are keyed by ViewKind, so a missing entry is a build error; this
    // asserts the pair exists so the menu row can never render label-less.
    expect(NEW_MENU_LABEL_KEY.context).toBeTruthy()
    expect(NEW_MENU_DESC_KEY.context).toBeTruthy()
  }, SIDE_PANEL_IMPORT_TIMEOUT_MS)

  it('is hidden from the + menu unless Developer Mode is on', async () => {
    const { newMenuSections } = await import('../pages/chat/SidePanel')
    const kinds = (o: { devMode: boolean; terminalEnabled: boolean }) =>
      newMenuSections({ ...o, summaryEnabled: true }).flatMap(g => g.items).map(i => i.kind)
    // Dev mode off: Context breakdown is not offered — it is a developer surface.
    expect(kinds({ devMode: false, terminalEnabled: true })).not.toContain('context')
    // Dev mode on: it appears (right after Logs, closing the diagnostics group).
    const on = kinds({ devMode: true, terminalEnabled: true })
    expect(on).toContain('context')
    expect(on.indexOf('context')).toBe(on.indexOf('logs') + 1)
    // The gate is independent of the Terminal gate, and it covers Logs as
    // well: both diagnostics views are Developer-Mode-only, so with dev mode off
    // neither is offered no matter what Terminal is doing.
    expect(kinds({ devMode: false, terminalEnabled: false })).not.toContain('logs')
    expect(kinds({ devMode: true, terminalEnabled: false })).toContain('logs')
    expect(kinds({ devMode: false, terminalEnabled: false })).not.toContain('terminal')
  }, SIDE_PANEL_IMPORT_TIMEOUT_MS)

  it('carries no session picker — the tab IS the session', () => {
    render(
      <ContextBreakdownPanel
        trace={trace({
          slot: 'chat-1',
          turns: [turnOf({ memory: 900, your_message: 100 }, { phase: 'session_start' })],
          context_window: 1_000_000,
        })}
        isLoading={false}
      />,
    )
    expect(screen.queryByRole('combobox')).toBeNull()
  })
})
