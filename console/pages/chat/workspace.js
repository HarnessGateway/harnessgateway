// How a document's links and images are taken when it is one of a session's files, or a reply about them.
import { slug } from "../../components/markdown.js";
import { resolve } from "../../lib/kinds.js";
import { viewUrl } from "../../lib/work.js";

/** How a document's links and images are taken, for one in the folder `folder` of a session's work: a link to the
 *  web opens there, a path opens the file beside the chat, and an image loads from the work or from the text itself,
 *  never from a host on the web. */
export function workLinks(session, folder = "") {
  const elsewhere = url => (/^[a-z][a-z0-9+.-]*:/i.test(url) && !/^file:/i.test(url)) || url.startsWith("//");
  const inside = url => session && !elsewhere(url) ? resolve(folder, url) : null;
  return {
    link(url) {
      const page = /^(?:\/console\/)?(#\/.+)$/.exec(url);        // a page of the console itself: an agent may point at one
      // "Add this folder of my work as a harness" names no session: the page that shows the reply knows which it
      // is, so an agent never copies an id, and cannot get one wrong.
      if (page) return { page: session ? page[1].replace(/^#\/harnesses\/add\//, `#/harnesses/from/${session}/`) : page[1] };
      if (/^(https?:|mailto:)/i.test(url)) return { href: url };
      if (url.startsWith("#")) return { anchor: slug(decodeURIComponent(url.slice(1))) };
      return inside(url) ? { open: inside(url) } : null;
    },
    image: url => /^data:image\/(png|jpe?g|gif|webp|avif|svg\+xml)[;,]/i.test(url) ? url : inside(url) && viewUrl(session, inside(url)),
  };
}
