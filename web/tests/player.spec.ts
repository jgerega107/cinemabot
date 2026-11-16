import { test, expect, Page } from '@playwright/test';

const id = (n: number) => n.toString().padStart(32, '0');
const titles = ['The Last Lighthouse', 'A Place Between', 'Sunday in the City', 'Small Hours', 'The Long Way Home', 'Wild Coast', 'A Quiet Orbit', 'After the Rain'];
const items = titles.map((title, i) => ({ id: id(i + 1), title, type: i === 5 ? 'Series' : 'Movie', year: 2020 + i % 5,
  overview: 'An unexpected journey brings old friends together, finding a little wonder in the places they thought they knew.',
  duration: 5400 + i * 120, audio: [{ index: 1, title: 'English · Stereo' }], subtitles: [{ index: 2, title: 'English' }], artwork: null }));
const basePlayer = { current: null, queue: [], state: 'idle', position: 0, volume: 70, audio: 1, subtitle: -1,
  channel_name: null, can_request: true, can_control: true, available: true, error: '', metrics: {} };

async function setup(page: Page, options: { loggedIn?: boolean; control?: boolean; playing?: boolean; readOnly?: boolean } = {}) {
  const state: any = { ...basePlayer, can_request: !options.readOnly, can_control: !options.readOnly && (options.control ?? true) };
  if (options.playing) Object.assign(state, { current: { ...items[0], key: 'current', requested_by: 'Alex', requester: { id: '123', name: 'Alex', avatar: '/test-avatar.svg' } }, state: 'playing', position: 1340, channel_name: 'Movie night', queue: [{ ...items[2], key: 'queued', requested_by: 'Sam', requester: { id: '456', name: 'Sam', avatar: '/missing-avatar.png' } }] });
  const actions: any[] = [];
  // Deterministic SSE source: production uses the real same-origin EventSource endpoint.
  await page.addInitScript(({ state }) => {
    class MockEvents extends EventTarget {
      onerror: any;
      timer: ReturnType<typeof setInterval>;
      constructor() {
        super();
        this.timer = setInterval(() => this.dispatchEvent(new MessageEvent('player', { data: JSON.stringify((window as any).__player || state) })), 100);
      }
      close() { clearInterval(this.timer); }
    }
    (window as any).EventSource = MockEvents;
  }, { state });
  await page.route('**/test-avatar.svg', route => route.fulfill({ contentType: 'image/svg+xml',
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="#ec4899"/></svg>' }));
  await page.route('**/missing-avatar.png', route => route.fulfill({ status: 404, body: '' }));
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    let data: any;
    if (url.pathname === '/api/meta') data = { instance: 'https://gammy.land', branding: { name: 'Gammy Land', color: '#ec4899', icon: '/test-avatar.svg', favicon: '/test-avatar.svg' } };
    else if (url.pathname === '/api/me') {
      if (options.loggedIn === false) return route.fulfill({ status: 401, json: { error: 'Sign in with Gammy Land.' } });
      data = { id: '123', name: 'Alex' };
    } else if (url.pathname === '/api/libraries') data = [{ id: id(90), title: 'Movies' }, { id: id(91), title: 'TV Shows' }];
    else if (url.pathname === '/api/items') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const results = items.filter(i => i.title.toLowerCase().includes(q));
      data = { items: results, total: results.length };
    } else if (url.pathname.startsWith('/api/items/')) data = items.find(i => i.id === url.pathname.split('/').at(-1));
    else if (url.pathname === '/api/action') {
      const action = route.request().postDataJSON();
      actions.push(action);
      if (action.action === 'play') { state.current = { ...items.find(i => i.id === action.item_id), key: 'current', requested_by: 'Alex', requester: { id: '123', name: 'Alex', avatar: '/test-avatar.svg' } }; state.state = 'playing'; state.channel_name = 'Movie night'; }
      if (action.action === 'pause') state.state = 'paused';
      if (action.action === 'resume') state.state = 'playing';
      if (action.action === 'tracks') Object.assign(state, { audio: action.audio, subtitle: action.subtitle });
      await page.evaluate(s => { (window as any).__player = s; }, state);
      data = state;
    } else data = state;
    await route.fulfill({ json: data });
  });
  return actions;
}

test('sign-in screen and responsive layout', async ({ page }, testInfo) => {
  await setup(page, { loggedIn: false });
  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Sign in with Gammy Land' })).toHaveAttribute('href', '/auth/login');
  await expect(page.getByRole('heading', { name: /A little cinema/ })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('welcome.png'), fullPage: true });
});

test('browse, search, details and play', async ({ page }, testInfo) => {
  const actions = await setup(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'What’s on tonight?' })).toBeVisible();
  await expect(page.locator('.title-card')).toHaveCount(8);
  await page.screenshot({ path: testInfo.outputPath('library.png'), fullPage: true });
  await page.getByRole('textbox', { name: 'Search library' }).fill('Lighthouse');
  await expect(page.locator('.title-card')).toHaveCount(1);
  await page.locator('.title-card').click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: 'Start watching' }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.getByRole('button', { name: 'Pause', exact: true })).toBeVisible();
  expect(actions[0]).toMatchObject({ action: 'play', item_id: items[0].id });
});

test('viewers outside voice cannot use controls', async ({ page }) => {
  await setup(page, { control: false, playing: true });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Pause', exact: true })).toBeDisabled();
  await expect(page.getByLabel('Playback position')).toBeDisabled();
  await expect(page.getByLabel('Subtitle track')).toBeDisabled();
  await expect(page.getByText('Join Movie night to use the controls.')).toBeVisible();
});

test('read-only members can browse but cannot request or change playback', async ({ page }) => {
  const actions = await setup(page, { readOnly: true, playing: true });
  await page.goto('/');
  await expect(page.getByText('You have read-only access. Ask an administrator for permission to request titles and control playback.')).toBeVisible();
  for (const name of ['Pause', 'Stop and disconnect', 'Skip title', 'Remove Sunday in the City']) {
    await expect(page.getByRole('button', { name, exact: true })).toBeDisabled();
  }
  for (const label of ['Playback position', 'Shared volume', 'Audio track', 'Subtitle track']) {
    await expect(page.getByLabel(label)).toBeDisabled();
  }
  await page.getByRole('textbox', { name: 'Search library' }).fill('Lighthouse');
  await expect(page.locator('.title-card')).toHaveCount(1);
  await page.locator('.title-card').click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Play now', exact: true })).toBeDisabled();
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Add to queue' })).toBeDisabled();
  await expect(page.getByRole('dialog')).toContainText('You have read-only access.');
  await expect(page.getByRole('dialog')).not.toContainText('Join the active voice channel');
  expect(actions).toEqual([]);
});

test('shared controls and track selection', async ({ page }, testInfo) => {
  const actions = await setup(page, { playing: true });
  await page.goto('/');
  await page.getByRole('button', { name: 'Pause', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeVisible();
  await page.getByLabel('Subtitle track').selectOption('2');
  expect(actions.some(a => a.action === 'tracks' && a.subtitle === 2)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('playing.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});


test('instance theme and requester identities survive image failures', async ({ page }) => {
  await setup(page, { playing: true });
  await page.goto('/');
  await expect(page).toHaveTitle('Gammy Land Cinema');
  await expect(page.locator('.credit')).toContainText('Requested by Alex');
  await expect(page.locator('.credit .avatar img')).toBeVisible();
  await expect(page.locator('.queue .requester')).toContainText('Requested by Sam');
  await expect(page.locator('.queue .avatar')).toHaveText('S');
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim())).toBe('#ec4899');
  await expect(page.locator('body')).not.toContainText('Fluxer');
});
