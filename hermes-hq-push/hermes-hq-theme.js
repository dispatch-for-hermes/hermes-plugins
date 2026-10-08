// Hermes HQ theme: a desktop Hermes theme that looks like Hermes HQ on an iPhone (and Nous High Contrast, below).
//
// It adds the theme "Hermes HQ" to Settings › Appearance (and Cmd-K, /skin), through desktop's own `themes`
// contribution area: no Hermes code changes. The palette is the phone's iOS one (src/ui-kit/ios-theme.ts and
// src/phone-ios-tokens.css): white page and #f2f2f7 sidebar in light, true black and #1c1c1e in dark, iOS system
// blue, the SF system font. The CSS below gives it the phone's shapes: iOS corner radii instead of desktop's nearly
// square ones, your messages as filled blue bubbles on the right, a bot's text in grey bubbles on the left (its tool
// rows outside them), a round composer and rounded sidebar rows.
//
// The CSS keys on desktop's data-slot hooks and a few of its class names, so a Hermes update can move them; the
// palette part keeps working either way. The renderer sets the palette's own tokens inline on <html>, so the CSS
// paints the bubbles from its own --hq-* variables rather than overriding those.
// Installed as ~/.hermes/desktop-plugins/hermes-hq-theme/plugin.js; desktop reloads it when the file changes.

const SYSTEM_SANS = '-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif, "Apple Color Emoji"'
const SYSTEM_MONO = 'ui-monospace, "SF Mono", Menlo, monospace, "Apple Color Emoji"'

export const LIGHT = {
  background: '#ffffff', foreground: '#000000', card: '#f2f2f7', cardForeground: '#000000',
  muted: '#f2f2f7', mutedForeground: '#8a8a8e', popover: '#ffffff', popoverForeground: '#000000',
  primary: '#007aff', primaryForeground: '#ffffff', secondary: '#e5f1ff', secondaryForeground: '#000000',
  accent: '#e5f1ff', accentForeground: '#000000', border: '#c6c6c8', input: '#ffffff', ring: '#007aff',
  midground: '#007aff', midgroundForeground: '#ffffff', composerRing: '#007aff',
  destructive: '#ff3b30', destructiveForeground: '#ffffff',
  sidebarBackground: '#f2f2f7', sidebarBorder: '#c6c6c8',
  // Your bubble is the accent, as on the phone (--ios-bubble-me-fill).
  userBubble: '#007aff', userBubbleBorder: '#007aff',
}

export const DARK = {
  background: '#000000', foreground: '#ffffff', card: '#1c1c1e', cardForeground: '#ffffff',
  muted: '#1c1c1e', mutedForeground: '#8d8d93', popover: '#1c1c1e', popoverForeground: '#ffffff',
  primary: '#0a84ff', primaryForeground: '#ffffff', secondary: '#0b2545', secondaryForeground: '#ffffff',
  accent: '#10233b', accentForeground: '#ffffff', border: '#38383a', input: '#1c1c1e', ring: '#0a84ff',
  midground: '#0a84ff', midgroundForeground: '#ffffff', composerRing: '#0a84ff',
  destructive: '#ff453a', destructiveForeground: '#ffffff',
  sidebarBackground: '#1c1c1e', sidebarBorder: '#38383a',
  // The accent a quarter darker, so white text keeps 4.5:1 (the phone's dark --ios-bubble-me-fill).
  userBubble: '#0863bf', userBubbleBorder: '#0863bf',
}

const USER = "[data-slot='aui_user-message-root'] .composer-human-message:not(.ui-prompt-input__container)"
const BOT = "[data-slot='aui_assistant-message-content'] .aui-md:not(.aui-md .aui-md, details .aui-md)"

export const CSS = `
/* iOS shapes: desktop pins its Tailwind corners at 0.2 of their size; the phone uses them whole. */
html:root {--radius-scalar:1}
/* Chat text at the phone's reading size, a step under iOS's 17 pt body for a desktop window. */
html:root {--conversation-text-font-size:15px}

/* Your messages: a filled bubble on the right, as wide as its text, white type. Painted here, not through the
   palette's userBubble, which desktop mixes toward the page (and the Message Bubble transparency setting). */
html:root {--hq-bubble-me:#007aff}
html:root.dark {--hq-bubble-me:#0863bf}
${USER} {
  width:fit-content;max-width:min(80%,36rem);margin-left:auto;
  padding:7px 13px;border:0;border-radius:18px;
  background:var(--hq-bubble-me);color:#fff;box-shadow:none;backdrop-filter:none}
${USER} :is(*,a,.ref) {color:inherit}
${USER} a {text-decoration:underline;text-underline-offset:.15em}
[data-slot='aui_user-message-root'] .composer-human-message-container {background:transparent;justify-content:flex-end}

/* A bot's text: a grey bubble on the left, as on the phone (--ios-bubble-bot). Its tool rows stay outside, as the
   reply's work trail. Only the reply's own text blocks: markdown nested in a tool's output keeps its plain look. */
html:root {--hq-bubble-bot:#e9e9eb}
html:root.dark {--hq-bubble-bot:#26282c}
${BOT} {
  width:fit-content;max-width:min(88%,44rem);box-sizing:border-box;
  padding:7px 13px;border-radius:18px;background:var(--hq-bubble-bot)}
${BOT} > div > :first-child {margin-top:0}
${BOT} > div > :last-child {margin-bottom:0}

/* The composer: one rounded field. */
[data-slot='composer-root'] .ui-prompt-input__container {border-radius:22px}
[data-slot='composer-rich-input'] {font-size:15px}

/* Code and cards on the phone's card shape. */
:is([data-slot='code-card'],[data-slot='file-diff-panel'],.aui-md pre) {border-radius:14px}
.aui-md :not(pre) > code {border-radius:6px}

/* Sidebar rows: rounded, iOS-style selection. */
[data-slot='row-button'] {border-radius:10px}
`

export const THEME = {
  name: 'hermes-hq',
  label: 'Hermes HQ',
  description: 'The Hermes HQ iPhone look: iOS colours, system blue, round shapes',
  typography: { fontSans: SYSTEM_SANS, fontMono: SYSTEM_MONO },
  colors: LIGHT,
  darkColors: DARK,
  customCSS: CSS,
}

// Nous High Contrast: desktop's own Nous palette (@hermes/shared theme-presets.ts, copied: a plugin can't import it)
// with Hermes HQ's High Contrast option (src/phone-ui-contrast.css) on top. Every value below is mixed from the
// theme's own text, page and accent, as the phone does: lines and secondary text pulled toward the text colour, boxes
// lifted off the page, your messages tinted with the accent and outlined. The palette is set inline on <html>, so these
// go on <body>, where everything inherits them.
export const NOUS_LIGHT = {
  background: '#ffffff', foreground: '#1f2328', card: '#f6f8fa', cardForeground: '#1f2328',
  muted: '#f6f6f6', mutedForeground: '#656d76', popover: '#ffffff', popoverForeground: '#1f2328',
  primary: '#0053fd', primaryForeground: '#ffffff', secondary: '#deeaff', secondaryForeground: '#1f2328',
  accent: '#e3edff', accentForeground: '#1f2328', border: '#d0d7de', input: '#ffffff', ring: '#0053fd',
  midground: '#0053fd', midgroundForeground: '#ffffff', composerRing: '#0053fd',
  destructive: '#cf222e', destructiveForeground: '#ffffff',
  sidebarBackground: '#f6f8fa', sidebarBorder: '#d0d7de', userBubble: '#dae7fd', userBubbleBorder: '#d0d7de',
}
export const NOUS_DARK = {
  background: '#0d1117', foreground: '#e6edf3', card: '#010409', cardForeground: '#e6edf3',
  muted: '#1a1e24', mutedForeground: '#7d8590', popover: '#161b22', popoverForeground: '#e6edf3',
  primary: '#4a84fe', primaryForeground: '#161616', secondary: '#1d2e4f', secondaryForeground: '#e6edf3',
  accent: '#17243a', accentForeground: '#e6edf3', border: '#30363d', input: '#0d1117', ring: '#4a84fe',
  midground: '#4a84fe', midgroundForeground: '#161616', composerRing: '#4a84fe',
  destructive: '#f85149', destructiveForeground: '#ffffff',
  sidebarBackground: '#010409', sidebarBorder: '#30363d', userBubble: '#07162c', userBubbleBorder: '#30363d',
}

export const CONTRAST_CSS = `
body {
  --hc-ink:var(--theme-foreground);--hc-page:var(--theme-background-seed);--hc-accent:var(--theme-midground);
  /* Lines: the text colour at a fixed share, about twice the theme's own (harder ones read as too much). */
  --ui-stroke-primary:color-mix(in srgb,var(--hc-ink) 18%,transparent);
  --ui-stroke-secondary:color-mix(in srgb,var(--hc-ink) 13%,transparent);
  --ui-stroke-tertiary:color-mix(in srgb,var(--hc-ink) 10%,transparent);
  --dt-border:var(--ui-stroke-secondary);--dt-input:var(--ui-stroke-primary);--dt-sidebar-border:var(--ui-stroke-tertiary);
  /* Secondary and tertiary text: a step toward the text colour. */
  --ui-text-secondary:color-mix(in oklab,var(--hc-ink) 82%,var(--hc-page));
  --ui-text-tertiary:color-mix(in oklab,var(--hc-ink) 64%,var(--hc-page));
  /* Boxes (cards, code, the message bar's field) lifted clearly off the page. */
  --ui-bg-editor:color-mix(in oklab,var(--hc-ink) 10%,var(--hc-page));
  --ui-bg-card:var(--ui-bg-editor);
  /* A selected row: the accent, so it never reads as just another surface. */
  --ui-row-active-background:color-mix(in oklab,var(--hc-accent) 22%,var(--hc-page));
  /* Your messages: the accent's tint with an accent outline. */
  --dt-user-bubble:color-mix(in oklab,var(--hc-accent) 28%,var(--hc-page));
  --dt-user-bubble-border:color-mix(in oklab,var(--hc-accent) 75%,var(--hc-page));
}
/* Light pages show a tint far more than dark ones: lighter fills, the same lines. */
html:not(.dark) body {
  --ui-bg-editor:color-mix(in oklab,var(--hc-ink) 5%,var(--hc-page));
  --ui-row-active-background:color-mix(in oklab,var(--hc-accent) 14%,var(--hc-page));
  --dt-user-bubble:color-mix(in oklab,var(--hc-accent) 13%,var(--hc-page));
  --dt-user-bubble-border:color-mix(in oklab,var(--hc-accent) 65%,var(--hc-page));
}
/* The chat keeps the strong treatment: your message and the message bar get a full-strength outline. */
[data-slot='aui_user-message-root'] .composer-human-message {border-color:var(--dt-user-bubble-border)}
[data-slot='composer-root'] .ui-prompt-input__container {border-color:color-mix(in srgb,var(--hc-ink) 50%,transparent)}
`

const NOUS_SANS = '"Segoe WPC", "Segoe UI", -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", system-ui, sans-serif, "Apple Color Emoji"'
const NOUS_MONO = '"JetBrains Mono", "Cascadia Code", "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", "Noto Mono", "SF Mono", ui-monospace, Menlo, Monaco, Consolas, monospace, "Apple Color Emoji"'

export const NOUS_HIGH_CONTRAST = {
  name: 'nous-high-contrast',
  label: 'Nous High Contrast',
  description: 'Nous with Hermes HQ’s High Contrast: clearer lines and text, your messages outlined in blue',
  typography: { fontSans: NOUS_SANS, fontMono: NOUS_MONO },
  colors: NOUS_LIGHT,
  darkColors: NOUS_DARK,
  customCSS: CONTRAST_CSS,
}

export default {
  id: 'hermes-hq-theme',
  name: 'Hermes HQ theme',
  description: 'Adds the Hermes HQ themes: the iPhone app’s look, and Nous with its High Contrast option.',
  register(ctx) {
    ctx.register({ id: 'hermes-hq', area: 'themes', title: THEME.label, data: THEME })
    ctx.register({ id: 'nous-high-contrast', area: 'themes', title: NOUS_HIGH_CONTRAST.label, data: NOUS_HIGH_CONTRAST })
  },
}
