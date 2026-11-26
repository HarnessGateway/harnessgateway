// Who a subject is, to a person reading. The platform names whoever made a thing by id (owner, user_…, svc_…), and a
// page shows a name. The console knows three by itself; an extension that has people says who they are (its
// edition.js exports `names`). Whoever nobody names is shown as their id.
import { session } from "./api.js";

const OWN = { owner: "the owner", host: "the host", platform: "the platform" };
const namers = [];                               // each extension's `names`: () → {id: name}

/** Take an extension's way of naming who is here. */
export const register = names => { namers.push(names); };
/** Every name known now, by id. A way of naming that fails adds nothing: the ids are then shown as they are. */
export async function names() {
  const found = await Promise.all(namers.map(each => Promise.resolve().then(each).catch(() => ({}))));
  return Object.assign({}, OWN, ...found);
}
/** A subject as a person reads it: "you", their name, or their id. */
export const named = (known, subject) => subject && subject === session.who ? "you" : known[subject] || subject || "nobody known";
