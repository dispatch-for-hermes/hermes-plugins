// Hermes HQ theme: a desktop Hermes theme that looks like Hermes HQ on an iPhone.
//
// It adds one theme, "Hermes HQ", to Settings › Appearance (and Cmd-K, /skin), through desktop's own `themes`
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

export default {
  id: 'hermes-hq-theme',
  name: 'Hermes HQ theme',
  description: 'Adds the Hermes HQ theme: the iPhone app’s colours and shapes on desktop.',
  register(ctx) {
    ctx.register({ id: 'hermes-hq', area: 'themes', title: THEME.label, data: THEME })
  },
}
