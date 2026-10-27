/**
 * Capture harness for the MCP Management share buttons (PR #19661).
 *
 * Shoots a RUNNING pod. The page, its components and its styles are the pod's
 * build; the three MCP reads (`status`, `servers`, `servers/launch`) are
 * answered by route stubs with a fixed set of servers so every state the
 * reviewer asked for is on screen at once. The page shows no launch command, and
 * every frame must confirm that. Nothing is written: the stub and enable POSTs
 * are aborted.
 *
 * Set MCP_CAPTURE_BEFORE=1 against a pod built from origin/main to shoot the
 * page as it was before this change (the STUB switch table); it takes the
 * default and narrow frames only.
 *
 * Usage:
 *   POD_URL=http://127.0.0.1:7845 POD_TOKEN=<token> \
 *     node scripts/capture-mcp-share-buttons.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/mcp-share-buttons'
const BASE = process.env.POD_URL
const TOKEN = process.env.POD_TOKEN
if (!BASE || !TOKEN) {
  console.error('POD_URL and POD_TOKEN are required')
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })

const declared = {
  strength: 'declared',
  recommendStub: true,
  recommendShare: true,
  reasons: [{ code: 'preflight_passed', detail: '' }],
}
const row = over => ({
  stub: false,
  can_stub: true,
  in_allowlist: false,
  entry_poolable: false,
  agents: ['kirocrew'],
  transport: 'stdio',
  denylisted: false,
  recommendation: declared,
  ...over,
})
const SERVERS = [
  row({ name: 'docs-mcp' }),
  row({ name: 'slack-mcp', stub: true, in_allowlist: true }),
  row({ name: 'search-mcp', stub: true, in_allowlist: true }),
  row({ name: 'tickets-mcp' }),
  // No LAUNCH entry: its read answers an incomplete display.
  row({ name: 'notion-mcp' }),
]
const LAUNCH = {
  'docs-mcp': { commands: [['/opt/mcp/docs/bin/docs-mcp', '--stdio']], envs: [['LOG_LEVEL=warn']] },
  'slack-mcp': { commands: [['/opt/mcp/slack/bin/slack-mcp']], envs: [['SLACK_WORKSPACE=example']] },
  'search-mcp': {
    commands: [['/opt/mcp/search/bin/search-mcp', '--profile', 'work', '--stdio']],
    envs: [['SEARCH_REGION=us-west-2']],
  },
  'tickets-mcp': { commands: [['/opt/mcp/tickets/bin/tickets-mcp']], envs: [[]] },
}

const pending = { delay: false, off: false }

// The sharing-off frames: the switch off, one marked server the check argues
// against, one never approved here, and one refused with no approvable identity.
const OFF_SERVERS = [
  ...SERVERS.filter(s => s.name !== 'slack-mcp'),
  row({
    name: 'slack-mcp',
    stub: true,
    in_allowlist: true,
    recommendation: { strength: 'refuted', recommendStub: false, recommendShare: false, reasons: [] },
  }),
  row({ name: 'jira-mcp', stub: true, in_allowlist: true }),
  row({ name: 'linear-mcp', stub: true, in_allowlist: true }),
]
const OFF_REFUSED = {
  'jira-mcp': { reason: 'added_outside_dashboard', complete: true, expected_launch: 'jira-mcp-identity' },
  'linear-mcp': { reason: 'changed_needs_reapproval', complete: false },
}

async function stub(page) {
  await page.route('**/api/mcp-gateway/status', r =>
    r.fulfill({
      json: {
        enabled: !pending.off,
        stub: ['slack-mcp', 'search-mcp', ...(pending.off ? ['jira-mcp', 'linear-mcp'] : [])],
        stub_count: pending.off ? 4 : 2,
        running: true,
        ping_ok: true,
        supported: true,
        launch_refused: {
          'search-mcp': {
            reason: 'changed_needs_reapproval',
            commands: [['/opt/mcp/search/bin/search-mcp', '--profile', 'work', '--stdio']],
            envs: [['SEARCH_REGION=us-west-2']],
            approved_commands: [['/opt/mcp/search/bin/search-mcp', '--stdio']],
            approved_envs: [['SEARCH_REGION=us-east-1']],
            complete: true,
            expected_launch: 'search-mcp-identity',
          },
          ...(pending.off ? OFF_REFUSED : {}),
        },
      },
    }),
  )
  await page.route('**/api/mcp-gateway/servers', r =>
    r.fulfill({ json: { servers: pending.off ? OFF_SERVERS : SERVERS } }),
  )
  await page.route('**/api/mcp-gateway/servers/launch?**', async r => {
    const name = new URL(r.request().url()).searchParams.get('name')
    if (pending.delay && name === 'tickets-mcp') await new Promise(res => setTimeout(res, 60_000))
    const l = LAUNCH[name]
    if (!l) return r.fulfill({ json: { name, commands: [], envs: [], complete: false } })
    return r.fulfill({ json: { name, ...l, complete: true, expected_launch: `${name}-identity` } })
  })
  await page.route('**/api/mcp/measure', r => r.fulfill({ json: { running: false, done: 0, total: 0, error: '' } }))
  // Never write anything: these are the only mutating calls the page makes. A
  // stub POST is answered here (nothing reaches the gateway): docs-mcp as a
  // success, every other name as a refused launch, so both notices can be shot.
  await page.route('**/api/mcp-gateway/servers/stub', r => {
    const name = JSON.parse(r.request().postData() || '{}').name
    if (name === 'docs-mcp') return r.fulfill({ json: { ok: true, name, stub: true, stubbed: [name] } })
    return r.fulfill({ status: 409, json: { error: 'moved', code: 'launch_changed_since_display' } })
  })
  await page.route('**/api/mcp-gateway/enable', r => r.abort())
}

const BEFORE = process.env.MCP_CAPTURE_BEFORE === '1'

async function open(page) {
  await page.evaluate(() => {
    window.history.pushState({}, '', '/developer?tab=mcp-pool')
    window.dispatchEvent(new PopStateEvent('popstate'))
  })
  await page.getByText('docs-mcp').first().waitFor({ timeout: 30_000 })
  await page.waitForTimeout(1200)
}

// The servers card: the one that lists docs-mcp.
const section = page => page.locator('section').filter({ hasText: 'docs-mcp' }).first()

// No frame may carry a launch command.
async function assertNoCommand(page) {
  const text = await page.locator('body').innerText()
  if (!BEFORE && text.includes('/opt/mcp/')) throw new Error('a launch command is on the page')
}

const browser = await chromium.launch()
try {
  const port = new URL(BASE).port
  for (const [label, viewport] of [['desktop', { width: 1280, height: 2200 }], ['narrow', { width: 360, height: 1600 }]]) {
    const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
    await context.addCookies(
      [BASE, `http://localhost:${port}`].map(u => ({ name: `mc_token_${port}`, value: TOKEN, url: u })),
    )
    const page = await context.newPage()
    await stub(page)
    await page.addInitScript(() => {
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-crewmates-onboarded', '1')
      localStorage.setItem('mc-theme', 'light')
    })
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1500)
    await open(page)

    await assertNoCommand(page)
    if (label === 'narrow') {
      await section(page).screenshot({ path: `${OUT}/06-narrow-360.png` })
      await context.close()
      continue
    }
    await section(page).screenshot({ path: `${OUT}/01-default.png` })
    if (BEFORE) {
      await context.close()
      continue
    }
    // The whole tab: the page-wide Sharing switch and Pre-install sections below the rows.
    await page.locator('[role="tabpanel"][data-state="active"]').screenshot({ path: `${OUT}/08-whole-tab.png` })

    const share = page.getByRole('switch', { name: 'Enable MCP sharing docs-mcp' })
    await share.hover()
    await page.waitForTimeout(300)
    await page.locator('li').filter({ has: share }).screenshot({ path: `${OUT}/02-hover.png` })
    await page.mouse.move(0, 0)
    await share.focus()
    await page.keyboard.press('Shift+Tab')
    await page.keyboard.press('Tab')
    await page.waitForTimeout(300)
    await page.locator('li').filter({ has: share }).screenshot({ path: `${OUT}/03-focus.png` })

    const confirm = page.getByRole('button', { name: 'Approve update search-mcp' })
    await page.locator('li').filter({ has: confirm }).screenshot({ path: `${OUT}/04-command-changed.png` })

    await page.getByRole('button', { name: 'Enable all shareable' }).click()
    await page.getByRole('region', { name: 'Servers to share' }).waitFor({ timeout: 15_000 })
    await page.waitForTimeout(500)
    await assertNoCommand(page)
    await page.getByRole('region', { name: 'Servers to share' }).screenshot({ path: `${OUT}/05-share-all-list.png` })
    await page.getByRole('button', { name: 'Cancel' }).click()

    // The checking state: the press holds one launch read open.
    pending.delay = true
    const tickets = page.getByRole('switch', { name: 'Enable MCP sharing tickets-mcp' })
    await tickets.click()
    await page.getByText('Checking what it runs…').waitFor({ timeout: 10_000 })
    await page.locator('li').filter({ has: tickets }).screenshot({ path: `${OUT}/07-checking.png` })
    pending.delay = false

    // A refused write and an accepted one, each with its notice.
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1500)
    await open(page)
    await page.getByRole('switch', { name: 'Enable MCP sharing notion-mcp' }).click()
    await page.waitForTimeout(1200)
    await page.locator('[role="tabpanel"][data-state="active"]').screenshot({ path: `${OUT}/13-share-unapprovable.png` })
    await page.getByRole('switch', { name: 'Enable MCP sharing tickets-mcp' }).click()
    await page.getByRole('alert').first().waitFor({ timeout: 10_000 })
    await page.waitForTimeout(500)
    await page.locator('[role="tabpanel"][data-state="active"]').screenshot({ path: `${OUT}/14-share-refused.png` })

    // Sharing off: the switch, its warning, the paused rows, and its dialog.
    pending.off = true
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1500)
    await open(page)
    await assertNoCommand(page)
    await page.locator('[role="tabpanel"][data-state="active"]').screenshot({ path: `${OUT}/09-sharing-off.png` })
    const jira = page.locator('li').filter({ hasText: 'jira-mcp' })
    await jira.locator('summary').click()
    await page.waitForTimeout(300)
    await jira.screenshot({ path: `${OUT}/10-not-approved-details-open.png` })
    await page.locator('li').filter({ hasText: 'linear-mcp' }).screenshot({ path: `${OUT}/11-no-approvable-identity.png` })
    await page.getByRole('button', { name: 'Enable all shareable' }).click()
    await page.getByRole('region', { name: 'Servers to share' }).waitFor({ timeout: 15_000 })
    await page.waitForTimeout(500)
    await page.getByRole('region', { name: 'Servers to share' }).screenshot({ path: `${OUT}/12-share-all-skipped.png` })
    await page.getByRole('button', { name: 'Cancel' }).click()
    await page.getByRole('switch', { name: 'MCP sharing' }).click()
    await page.getByRole('dialog').waitFor({ timeout: 10_000 })
    await page.waitForTimeout(500)
    await page.getByRole('dialog').screenshot({ path: `${OUT}/15-sharing-confirm.png` })
    pending.off = false
    pending.delay = false
    await context.close()
  }
  console.log(`wrote ${OUT}`)
} finally {
  await browser.close()
}
