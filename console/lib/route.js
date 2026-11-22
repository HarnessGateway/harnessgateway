// The console's address is its hash. A page that changed what it shows asks to be drawn again.

export const again = () => window.dispatchEvent(new Event("hashchange"));
