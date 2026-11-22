// A harness package as it travels: a gzip-compressed tar archive of its files. The console unpacks one to edit it
// and packs what was edited to save it, here in the browser, so the platform keeps no half-written packages.
//
// A file is {text, executable} when it is text, or {bytes, executable} when it is not.

const BLOCK = 512;
const strict = new TextDecoder("utf-8", { fatal: true }), loose = new TextDecoder(), encoder = new TextEncoder();
const through = async (data, transform) => new Uint8Array(await new Response(new Blob([data]).stream().pipeThrough(transform)).arrayBuffer());
const text = (block, start, length) => loose.decode(block.subarray(start, start + length)).replace(/\0.*$/s, "");
const octal = (block, start, length) => parseInt(text(block, start, length).trim() || "0", 8);

/** The files in a package archive, by path. */
export async function unpack(archive) {
  const tar = await through(archive, new DecompressionStream("gzip")), files = new Map();
  let long = null;                                 // a path given ahead of its file, when it is too long for the header
  for (let at = 0; at + BLOCK <= tar.length;) {
    const head = tar.subarray(at, at + BLOCK);
    if (head.every(byte => byte === 0)) break;
    const size = octal(head, 124, 12), kind = String.fromCharCode(head[156] || 48);
    const content = tar.subarray(at + BLOCK, at + BLOCK + size);
    at += BLOCK + Math.ceil(size / BLOCK) * BLOCK;
    if (kind === "x") long = (/\d+ path=([^\n]*)\n/.exec(loose.decode(content)) || [])[1] || null;      // a pax header
    else if (kind === "L") long = loose.decode(content).replace(/\0.*$/s, "");                          // a GNU one
    else {
      if (kind === "0") {
        const prefix = text(head, 345, 155), path = (long || (prefix ? `${prefix}/` : "") + text(head, 0, 100)).replace(/^\.\//, "");
        const executable = Boolean(octal(head, 100, 8) & 0o100);
        try { files.set(path, { text: strict.decode(content), executable }); } catch { files.set(path, { bytes: content.slice(), executable }); }
      }
      long = null;
    }
  }
  return files;
}

/** Files as a package archive. */
export async function pack(files) {
  const blocks = [];
  for (const path of [...files.keys()].sort()) {
    const file = files.get(path), content = file.bytes || encoder.encode(file.text), head = new Uint8Array(BLOCK);
    const write = (value, start) => head.set(encoder.encode(value), start);
    let name = path, prefix = "";
    if (encoder.encode(path).length > 100) {       // a long path is split at a folder: the front goes in the prefix
      const cut = path.lastIndexOf("/", 155);
      [prefix, name] = [path.slice(0, cut), path.slice(cut + 1)];
      if (cut < 0 || encoder.encode(name).length > 100) throw new Error(`${path}: the path is too long for a package`);
    }
    write(name, 0); write(file.executable ? "0000755\0" : "0000644\0", 100); write("0000000\0", 108); write("0000000\0", 116);
    write(content.length.toString(8).padStart(11, "0") + "\0", 124); write("00000000000\0", 136); write("        ", 148);
    head[156] = 48; write("ustar\0" + "00", 257); write(prefix, 345);
    write(head.reduce((sum, byte) => sum + byte, 0).toString(8).padStart(6, "0") + "\0 ", 148);         // the checksum, last
    blocks.push(head, content, new Uint8Array((BLOCK - content.length % BLOCK) % BLOCK));
  }
  blocks.push(new Uint8Array(BLOCK * 2));
  return new Blob([await through(new Blob(blocks), new CompressionStream("gzip"))], { type: "application/gzip" });
}
