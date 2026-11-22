// A session's work, for the chat: the files its latest turn left, and addresses a browser can open them at.
//
// An image, a video or a page an agent built is shown by giving the browser an address, and the browser sends no
// key with it. The platform gives a link that is itself the permission, for an hour; what is served through it is
// served sandboxed, so nothing an agent made runs as this page.
import { slug } from "../../components/markdown.js";
import { containerOf, get, post } from "../../lib/api.js";
import { resolve } from "../../lib/kinds.js";

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

/** How a document's links and images are taken, for one in the folder `folder` of a session's work: a link to the
 *  web opens there, a path opens the file beside the chat, and an image loads from the work or from the text itself,
 *  never from a host on the web. */
export function workLinks(session, folder = "") {
  const elsewhere = url => (/^[a-z][a-z0-9+.-]*:/i.test(url) && !/^file:/i.test(url)) || url.startsWith("//");
  const inside = url => session && !elsewhere(url) ? resolve(folder, url) : null;
  return {
    link(url) {
      if (/^(https?:|mailto:)/i.test(url)) return { href: url };
      if (url.startsWith("#")) return { anchor: slug(decodeURIComponent(url.slice(1))) };
      return inside(url) ? { open: inside(url) } : null;
    },
    image: url => /^data:image\/(png|jpe?g|gif|webp|avif|svg\+xml)[;,]/i.test(url) ? url : inside(url) && viewUrl(session, inside(url)),
  };
}
