// What the console keeps in this browser: conveniences only (the chat's own list of conversations, which panel
// was open). Storage may be full or blocked; then the console works until the page closes, and forgets.

export const kept = (key, empty) => { try { return JSON.parse(localStorage.getItem(key)) ?? empty; } catch { return empty; } };
export const keep = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* full or blocked */ } };
