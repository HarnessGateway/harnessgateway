// What another edition adds to the console (spec §19, §20). An extension serves its own file in place of this
// one. The core adds nothing:
//
//   pages     pages added to the navigation: { id, title, module, role }. The module exports
//             `render(root, context)`, like the core's own pages.
//   signIns   other ways to sign in, shown as buttons on the sign-in form: { title, href }. The edition's own
//             routes behind `href` say who the browser is from then on (a cookie), and the console needs no key.
//   signOut   called when someone signs out, to end what such a sign-in started; or null.
export const pages = [];
export const signIns = [];
export const signOut = null;
