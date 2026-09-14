// Mirror of pitv/player/input.py: the remote-control actions and their built-in evdev key names.
export const ACTIONS = ['up', 'down', 'left', 'right', 'ok', 'back', 'guide', 'info', 'pause', 'mute',
  'vol_up', 'vol_down', 'ch_up', 'ch_down', 'restart', 'power',
  ...Array.from({ length: 9 }, (_, i) => `channel_${i + 1}`)];

export const ACTION_LABELS = {
  up: 'Up', down: 'Down', left: 'Left', right: 'Right', ok: 'OK / select', back: 'Back', guide: 'Guide',
  info: 'Info', pause: 'Play / pause', mute: 'Mute', vol_up: 'Volume up', vol_down: 'Volume down',
  ch_up: 'Channel up', ch_down: 'Channel down', restart: 'Restart programme', power: 'Power',
};
for (let n = 1; n <= 9; n++) ACTION_LABELS[`channel_${n}`] = `Channel ${n}`;

export const DEFAULT_KEYMAP = {
  up: ['KEY_UP'], down: ['KEY_DOWN'], left: ['KEY_LEFT'], right: ['KEY_RIGHT'],
  ok: ['KEY_ENTER', 'KEY_KPENTER', 'KEY_OK', 'KEY_SELECT'],
  back: ['KEY_BACK', 'KEY_ESC', 'KEY_BACKSPACE', 'KEY_EXIT'],
  guide: ['KEY_CONTEXT_MENU', 'KEY_MENU', 'KEY_EPG', 'KEY_HOMEPAGE', 'KEY_HOME', 'KEY_COMPOSE', 'KEY_G'],
  info: ['KEY_INFO', 'KEY_I'],
  pause: ['KEY_PLAYPAUSE', 'KEY_PLAY', 'KEY_PAUSE', 'KEY_PLAYCD', 'KEY_PAUSECD', 'KEY_SPACE'],
  mute: ['KEY_MUTE', 'KEY_STOPCD', 'KEY_STOP', 'KEY_M'],
  vol_up: ['KEY_VOLUMEUP', 'KEY_EQUAL', 'KEY_KPPLUS'],
  vol_down: ['KEY_VOLUMEDOWN', 'KEY_MINUS', 'KEY_KPMINUS'],
  ch_up: ['KEY_CHANNELUP', 'KEY_PAGEUP', 'KEY_NEXTSONG'],
  ch_down: ['KEY_CHANNELDOWN', 'KEY_PAGEDOWN', 'KEY_PREVIOUSSONG'],
  restart: ['KEY_REWIND', 'KEY_R'],
  power: ['KEY_POWER', 'KEY_SLEEP'],
};
for (let n = 1; n <= 9; n++) DEFAULT_KEYMAP[`channel_${n}`] = [`KEY_${n}`, `KEY_KP${n}`, `KEY_NUMERIC_${n}`];

/** Settings keymap ({} = defaults) merged over the defaults, as a fresh {action: [keys]} object. */
export function mergedKeymap(setting) {
  const out = {};
  for (const a of ACTIONS) {
    const custom = setting && Array.isArray(setting[a]) ? setting[a] : null;
    out[a] = [...(custom ?? DEFAULT_KEYMAP[a] ?? [])].map((k) => String(k).toUpperCase());
  }
  return out;
}
