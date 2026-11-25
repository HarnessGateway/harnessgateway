// What the installation's extensions add to the console: pages, and ways to sign in. Each extension's files are
// served under x/<name>/, and its edition.js names what it adds. Nothing of an extension's is loaded in place of
// anything of the console's, or of another extension's.

/** What every extension adds, in the order the extensions were named: {pages, signIns, signOuts, refused}. `taken`
 *  holds the ids the console's own pages have. A page whose id is taken, by the console or by an earlier extension,
 *  is refused, and so is an extension whose edition.js does not load: `refused` says why, for a person to read. */
export async function editions(taken) {
  const pages = [], signIns = [], signOuts = [], refused = [];
  const answer = await fetch("extensions.json");
  for (const name of answer.ok ? await answer.json() : []) {
    let edition;
    try { edition = await import(`../x/${encodeURIComponent(name)}/edition.js`); }
    catch (error) { refused.push(`${name}: its pages could not be loaded: ${error.message}`); continue; }
    for (const page of edition.pages || []) {
      if (taken.has(page.id)) { refused.push(`${name}: there is already a page named ${page.id}`); continue; }
      taken.add(page.id);
      pages.push(page);
    }
    signIns.push(...(edition.signIns || []));
    if (edition.signOut) signOuts.push(edition.signOut);
  }
  refused.forEach(why => console.error(why));
  return { pages, signIns, signOuts, refused };
}
