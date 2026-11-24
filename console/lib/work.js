// A session's work: the files its latest turn left, and addresses a browser can open them at.
//
// An image, a video or a page an agent built is shown by giving the browser an address, and the browser sends no
// key with it. The platform gives a link that is itself the permission, for an hour; what is served through it is
// served sandboxed, so nothing an agent made runs as this page.
import { containerOf, get, post } from "./api.js";

const RENEW_S = 900, PAGES_MAX = 20;             // 20 pages of 100 files are listed
const links = new Map();                         // session → {base, until}

/** Make sure there is a link to this session's files that lasts a while yet. */
export async function ensureView(session) {
  const held = links.get(session);
  if (held && held.until - Date.now() / 1000 > RENEW_S) return;
  const link = await post(`/v1/containers/${containerOf(session)}/view`);
  links.set(session, { base: link.url, until: link.expires_at });
}
/** Where a browser opens one of the session's files, or null before ensureView(session) has answered. */
export const viewUrl = (session, path) => links.has(session) ? links.get(session).base + String(path).split("/").map(encodeURIComponent).join("/") : null;

/** The session's files as its latest turn left them: [{path, bytes, at}] by path, and whether there are more. */
export async function filesOf(session) {
  const files = [];
  let more = true;
  for (let page = 0, after = ""; more && page < PAGES_MAX; page++) {
    const listed = await get(`/v1/containers/${containerOf(session)}/files?limit=100${after && `&after=${after}`}`);
    files.push(...listed.data.map(file => ({ path: file.path.replace(/^\/work\//, ""), bytes: file.bytes || 0, at: file.created_at || 0 })));
    more = listed.has_more;
    after = listed.last_id;
  }
  await ensureView(session);
  return { files, more };
}

/** The harness packages among a session's files: each top-level folder that holds install and run. */
export const packagesIn = files => {
  const paths = new Set(files.map(file => file.path));
  return files.map(file => /^([^./][^/]*)\/run$/.exec(file.path)).filter(Boolean).map(found => found[1])
    .filter(folder => paths.has(`${folder}/install`));
};
